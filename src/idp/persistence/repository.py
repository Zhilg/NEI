"""Transactional PostgreSQL control-plane repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from idp.domain.models import (
    ArtifactReference,
    BatchSnapshot,
    Entity,
    FinalManifest,
    JobClaim,
    ResourceRequest,
    StoredArtifact,
)
from idp.domain.states import (
    ArtifactRetention,
    BatchItemState,
    BatchState,
    JobState,
    QualityState,
    ReservationKind,
)
from idp.persistence.models import (
    ArtifactModel,
    BatchItemModel,
    BatchModel,
    BatchRootModel,
    DocumentModel,
    EntityResultModel,
    JobModel,
    PipelineProfileModel,
    ResourcePoolModel,
    ResourceReservationModel,
    StageRunModel,
)
from idp.services.state_machine import (
    require_batch_transition,
    require_item_transition,
    require_terminal_job_state,
)


class RepositoryError(RuntimeError):
    """Base error for durable controller operations."""


class UnknownProfileError(RepositoryError):
    """Raised when a submitted snapshot references no registered profile."""


class LeaseOwnershipError(RepositoryError):
    """Raised when a worker mutates work without a current owned lease."""


class ResourceCapacityError(RepositoryError):
    """Raised when a serialized pool cannot admit the requested resources."""


class ResourceReservationError(RepositoryError):
    """Raised for duplicate or malformed active reservations."""


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Produce timezone-aware controller timestamps."""
    return datetime.now(UTC)


class SqlAlchemyBatchRepository:
    """PostgreSQL implementation of snapshot, queue, lease and capacity operations."""

    def __init__(self, session_factory: sessionmaker[Session], clock: Clock = utc_now) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def register_profile(self, *, name: str, profile_hash: str) -> UUID:
        """Insert an immutable profile once, safely under concurrent setup calls."""
        with self._session_factory.begin() as session:
            identifier = session.scalar(
                insert(PipelineProfileModel)
                .values(name=name, profile_hash=profile_hash, configuration={}, active=False)
                .on_conflict_do_nothing(index_elements=[PipelineProfileModel.profile_hash])
                .returning(PipelineProfileModel.id)
            )
            if identifier is not None:
                return identifier
            existing = session.scalar(
                select(PipelineProfileModel.id).where(
                    PipelineProfileModel.profile_hash == profile_hash
                )
            )
            if existing is None:
                msg = f"profile insert conflicted but profile {profile_hash} is unavailable"
                raise RepositoryError(msg)
            return existing

    def resolve_profile_hash(self, identifier: str) -> str:
        """Resolve an explicit profile name or immutable hash before submission."""
        with self._session_factory() as session:
            profile_hash = session.scalar(
                select(PipelineProfileModel.profile_hash).where(
                    or_(
                        PipelineProfileModel.name == identifier,
                        PipelineProfileModel.profile_hash == identifier,
                    )
                )
            )
            if profile_hash is None:
                raise UnknownProfileError(f"unknown pipeline profile: {identifier}")
            return profile_hash

    def configure_resource_pool(
        self, *, kind: ReservationKind, capacity: int, unit: str
    ) -> UUID:
        """Create or update one capacity budget before controller admission begins."""
        if capacity <= 0:
            msg = "resource pool capacity must be positive"
            raise ValueError(msg)
        with self._session_factory.begin() as session:
            pool = session.scalar(
                select(ResourcePoolModel)
                .where(ResourcePoolModel.kind == kind, ResourcePoolModel.unit == unit)
                .with_for_update()
            )
            if pool is None:
                pool = ResourcePoolModel(kind=kind, capacity=capacity, unit=unit)
                session.add(pool)
                session.flush()
            else:
                pool.capacity = capacity
            return pool.id

    def create_batch(
        self,
        snapshot: BatchSnapshot,
        profile_hash: str,
        source_artifacts: dict[UUID, ArtifactReference] | None = None,
    ) -> None:
        """Store the whole scan snapshot atomically, including content deduplication."""
        with self._session_factory.begin() as session:
            profile_id = session.scalar(
                select(PipelineProfileModel.id).where(PipelineProfileModel.profile_hash == profile_hash)
            )
            if profile_id is None:
                msg = f"unknown pipeline profile hash: {profile_hash}"
                raise UnknownProfileError(msg)
            session.add(
                BatchModel(
                    id=snapshot.batch_id,
                    profile_id=profile_id,
                    state=snapshot.state,
                    created_at=snapshot.created_at,
                )
            )
            session.add_all(
                [BatchRootModel(batch_id=snapshot.batch_id, path=str(root)) for root in snapshot.roots]
            )
            reusable = self._reusable_items(session, profile_hash)
            for item in snapshot.items:
                document_id = (
                    self._get_or_create_document(session, item.source_sha256)
                    if item.source_sha256 is not None
                    else None
                )
                model = BatchItemModel(
                        id=item.item_id,
                        batch_id=snapshot.batch_id,
                        document_id=document_id,
                        root_path=str(item.root),
                        source_path=str(item.path),
                        state=item.state,
                        scan_reason=item.reason,
                        source_size_bytes=item.size_bytes,
                        source_mtime_ns=item.mtime_ns,
                        source_device=item.device,
                        source_inode=item.inode,
                        observed_at=item.observed_at,
                    )
                if item.state == BatchItemState.QUEUED and item.source_sha256 in reusable:
                    source = reusable[item.source_sha256]
                    model.state = BatchItemState.REUSED
                    model.quality = source.quality
                    model.final_bundle_prefix = source.final_bundle_prefix
                    model.final_manifest_key = source.final_manifest_key
                    model.reused_from_item_id = source.id
                    model.scan_reason = "strict_compatible_final_bundle_reuse"
                session.add(model)
                session.flush()
                if model.state == BatchItemState.QUEUED:
                    source_artifact = (source_artifacts or {}).get(model.id)
                    if source_artifact is None:
                        # Low-level fixtures may build a batch before explicitly
                        # enqueuing jobs. Production submit always provides source artifacts.
                        continue
                    model.source_object_key = source_artifact.object_key
                    job_payload = {
                        "source_object_key": source_artifact.object_key,
                        "source_object_sha256": source_artifact.sha256,
                    }
                    job_id = self._enqueue_in_session(
                        session,
                        model.id,
                        "source_snapshot",
                        job_payload,
                        3,
                        None,
                    )
                    existing_artifact = session.scalar(
                        select(ArtifactModel)
                        .where(ArtifactModel.object_key == source_artifact.object_key)
                        .with_for_update()
                    )
                    if existing_artifact is None:
                        session.add(
                            ArtifactModel(
                                producing_job_id=job_id,
                                object_key=source_artifact.object_key,
                                sha256=source_artifact.sha256,
                                media_type=source_artifact.media_type,
                                size_bytes=model.source_size_bytes or 0,
                                retention=ArtifactRetention.TEMPORARY,
                            )
                        )
                    elif existing_artifact.sha256 != source_artifact.sha256:
                        raise RepositoryError(
                            f"source artifact key collision: {source_artifact.object_key}"
                        )

    def enqueue_job(
        self,
        *,
        batch_item_id: UUID,
        stage: str,
        payload: dict[str, object],
        max_attempts: int,
        depends_on: UUID | None = None,
    ) -> UUID:
        """Create a deterministic idempotent stage job exactly once."""
        if max_attempts <= 0:
            msg = "max_attempts must be positive"
            raise ValueError(msg)
        key = f"{batch_item_id}:{stage}:{depends_on or 'root'}"
        with self._session_factory.begin() as session:
            identifier = session.scalar(
                insert(JobModel)
                .values(
                    batch_item_id=batch_item_id,
                    depends_on_id=depends_on,
                    stage=stage,
                    idempotency_key=key,
                    payload=payload,
                    state=JobState.PENDING,
                    priority=0,
                    attempt_count=0,
                    max_attempts=max_attempts,
                )
                .on_conflict_do_nothing(index_elements=[JobModel.idempotency_key])
                .returning(JobModel.id)
            )
            if identifier is not None:
                return identifier
            existing = session.scalar(select(JobModel.id).where(JobModel.idempotency_key == key))
            if existing is None:
                msg = f"job insert conflicted but idempotency key {key} is unavailable"
                raise RepositoryError(msg)
            return existing

    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> JobClaim | None:
        """Claim one dependency-ready job with PostgreSQL SKIP LOCKED semantics."""
        current_time = now or self._clock()
        dependency = aliased(JobModel)
        dependency_succeeded = exists(
            select(dependency.id).where(
                dependency.id == JobModel.depends_on_id,
                dependency.state == JobState.SUCCEEDED,
            )
        )
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(JobModel)
                .join(BatchItemModel, JobModel.batch_item_id == BatchItemModel.id)
                .join(BatchModel, BatchItemModel.batch_id == BatchModel.id)
                .where(
                    JobModel.state == JobState.PENDING,
                    JobModel.attempt_count < JobModel.max_attempts,
                    BatchItemModel.state.in_((BatchItemState.QUEUED, BatchItemState.RUNNING)),
                    BatchModel.state.in_((BatchState.QUEUED, BatchState.RUNNING)),
                    or_(JobModel.depends_on_id.is_(None), dependency_succeeded),
                )
                .order_by(JobModel.priority.desc(), JobModel.created_at, JobModel.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            item = self._locked_item(session, job.batch_item_id)
            batch = self._locked_batch(session, item.batch_id)
            if batch.state == BatchState.QUEUED:
                require_batch_transition(batch.state, BatchState.RUNNING)
                batch.state = BatchState.RUNNING
            if item.state == BatchItemState.QUEUED:
                require_item_transition(item.state, BatchItemState.RUNNING)
                item.state = BatchItemState.RUNNING
            job.state = JobState.RUNNING
            job.attempt_count += 1
            job.lease_owner = worker_id
            job.lease_expires_at = current_time + lease_duration
            job.started_at = current_time
            session.add(
                StageRunModel(
                    job_id=job.id,
                    attempt=job.attempt_count,
                    worker_id=worker_id,
                    started_at=current_time,
                )
            )
            return JobClaim(
                job_id=job.id,
                batch_item_id=job.batch_item_id,
                stage=job.stage,
                attempt=job.attempt_count,
                payload=job.payload,
                lease_owner=worker_id,
                lease_expires_at=job.lease_expires_at,
            )

    def renew_lease(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> None:
        """Extend one job lease and its active resource reservations together."""
        current_time = now or self._clock()
        expiry = current_time + lease_duration
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, worker_id, current_time)
            assert job is not None
            job.lease_expires_at = expiry
            session.execute(
                update(ResourceReservationModel)
                .where(
                    ResourceReservationModel.job_id == job_id,
                    ResourceReservationModel.owner == worker_id,
                    ResourceReservationModel.released_at.is_(None),
                )
                .values(lease_expires_at=expiry)
            )

    def complete_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        state: JobState,
        now: datetime | None = None,
    ) -> None:
        """Complete successful/cancelled work and free its leased resources."""
        require_terminal_job_state(state)
        if state == JobState.FAILED:
            msg = "use fail_job to apply retry and quarantine policy"
            raise ValueError(msg)
        current_time = now or self._clock()
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, worker_id, current_time)
            assert job is not None
            job.state = state
            job.completed_at = current_time
            job.lease_owner = None
            job.lease_expires_at = None
            self._finish_stage_run(session, job, current_time)
            self._release_resources(session, job_id, worker_id, current_time)
            item = self._locked_item(session, job.batch_item_id)
            batch = self._locked_batch(session, item.batch_id)
            if batch.state == BatchState.CANCELLED or item.cancellation_requested_at is not None:
                job.state = JobState.CANCELLED
                item.state = BatchItemState.CANCELLED

    def fail_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_code: str,
        error_detail: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> JobState:
        """Requeue retryable failures or terminally quarantine the document item."""
        current_time = now or self._clock()
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, worker_id, current_time)
            assert job is not None
            retry = retryable and job.attempt_count < job.max_attempts
            job.state = JobState.PENDING if retry else JobState.FAILED
            job.completed_at = None if retry else current_time
            job.lease_owner = None
            job.lease_expires_at = None
            self._finish_stage_run(session, job, current_time, error_code, error_detail)
            self._release_resources(session, job_id, worker_id, current_time)
            if retry:
                item = self._locked_item(session, job.batch_item_id)
                if item.state == BatchItemState.RUNNING:
                    require_item_transition(item.state, BatchItemState.QUEUED)
                    item.state = BatchItemState.QUEUED
            else:
                self._quarantine_item(session, job.batch_item_id, error_code, current_time)
            return job.state

    def requeue_expired_jobs(self, *, now: datetime | None = None) -> int:
        """Release abandoned work; final expired attempts quarantine their item."""
        current_time = now or self._clock()
        with self._session_factory.begin() as session:
            jobs = list(
                session.scalars(
                    select(JobModel)
                    .where(
                        JobModel.state == JobState.RUNNING,
                        JobModel.lease_expires_at.is_not(None),
                        JobModel.lease_expires_at < current_time,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for job in jobs:
                owner = job.lease_owner
                item = self._locked_item(session, job.batch_item_id)
                batch = self._locked_batch(session, item.batch_id)
                self._finish_stage_run(
                    session,
                    job,
                    current_time,
                    "lease_expired",
                    "worker heartbeat stopped before job completion",
                )
                if owner is not None:
                    self._release_resources(session, job.id, owner, current_time)
                job.lease_owner = None
                job.lease_expires_at = None
                if batch.state == BatchState.CANCELLED or item.cancellation_requested_at is not None:
                    job.state = JobState.CANCELLED
                    job.completed_at = current_time
                    if item.state == BatchItemState.RUNNING:
                        item.state = BatchItemState.CANCELLED
                    continue
                if job.attempt_count < job.max_attempts:
                    job.state = JobState.PENDING
                    if item.state == BatchItemState.RUNNING:
                        require_item_transition(item.state, BatchItemState.QUEUED)
                        item.state = BatchItemState.QUEUED
                else:
                    job.state = JobState.FAILED
                    job.completed_at = current_time
                    self._quarantine_item(
                        session, job.batch_item_id, "lease_expired_attempts_exhausted", current_time
                    )
            return len(jobs)

    def reserve_resources(
        self,
        *,
        job_id: UUID,
        owner: str,
        requests: tuple[ResourceRequest, ...],
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> None:
        """Lock capacity pools in deterministic order before recording reservations."""
        if not requests:
            return
        pool_keys = {(request.kind, request.unit) for request in requests}
        if len(pool_keys) != len(requests):
            msg = "one request per resource pool is allowed for a job"
            raise ResourceReservationError(msg)
        current_time = now or self._clock()
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, owner, current_time)
            active = session.scalar(
                select(ResourceReservationModel.id).where(
                    ResourceReservationModel.job_id == job_id,
                    ResourceReservationModel.released_at.is_(None),
                )
            )
            if active is not None:
                msg = f"job {job_id} already holds active resource reservations"
                raise ResourceReservationError(msg)
            expiry = current_time + lease_duration
            for request in sorted(requests, key=lambda value: (value.kind.value, value.unit)):
                pool = session.scalar(
                    select(ResourcePoolModel)
                    .where(ResourcePoolModel.kind == request.kind, ResourcePoolModel.unit == request.unit)
                    .with_for_update()
                )
                if pool is None:
                    msg = f"resource pool not configured: {request.kind}/{request.unit}"
                    raise ResourceCapacityError(msg)
                used = session.scalar(
                    select(func.coalesce(func.sum(ResourceReservationModel.amount), 0)).where(
                        ResourceReservationModel.pool_id == pool.id,
                        ResourceReservationModel.released_at.is_(None),
                        ResourceReservationModel.lease_expires_at > current_time,
                    )
                )
                if int(used or 0) + request.amount > pool.capacity:
                    msg = f"insufficient {request.kind} capacity in {request.unit}"
                    raise ResourceCapacityError(msg)
                session.add(
                    ResourceReservationModel(
                        job_id=job_id,
                        pool_id=pool.id,
                        kind=request.kind,
                        amount=request.amount,
                        unit=request.unit,
                        owner=owner,
                        lease_expires_at=expiry,
                    )
                )

    def defer_job_for_capacity(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_detail: str,
        now: datetime | None = None,
    ) -> None:
        """Release, requeue, and pause atomically without consuming a retry attempt."""
        current_time = now or self._clock()
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, worker_id, current_time)
            assert job is not None
            item = self._locked_item(session, job.batch_item_id)
            batch = self._locked_batch(session, item.batch_id)
            if batch.state == BatchState.RUNNING:
                require_batch_transition(batch.state, BatchState.PAUSED_CAPACITY)
                batch.state = BatchState.PAUSED_CAPACITY
            run = session.scalar(
                select(StageRunModel)
                .where(StageRunModel.job_id == job.id, StageRunModel.attempt == job.attempt_count)
                .with_for_update()
            )
            if run is not None:
                session.delete(run)
            job.attempt_count -= 1
            job.state = JobState.PENDING
            job.lease_owner = None
            job.lease_expires_at = None
            self._release_resources(session, job.id, worker_id, current_time)
            if item.state == BatchItemState.RUNNING:
                require_item_transition(item.state, BatchItemState.QUEUED)
                item.state = BatchItemState.QUEUED

    def resume_batch_after_capacity(self, *, batch_id: UUID) -> None:
        """Resume a batch only from the explicit capacity-paused state."""
        with self._session_factory.begin() as session:
            batch = self._locked_batch(session, batch_id)
            if batch.state == BatchState.PAUSED_CAPACITY:
                require_batch_transition(batch.state, BatchState.RUNNING)
                batch.state = BatchState.RUNNING

    def commit_publication(
        self,
        *,
        item_id: UUID,
        bundle_prefix: str,
        manifest: FinalManifest,
        artifacts: tuple[StoredArtifact, ...],
        entities: tuple[Entity, ...],
        schema_version: str,
    ) -> None:
        """Expose a bundle exactly once after all immutable object writes complete."""
        prefix = bundle_prefix.rstrip("/")
        manifest_key = f"{prefix}/manifest.json"
        required = {
            manifest.final_markdown.object_key: manifest.final_markdown.sha256,
            manifest.entities.object_key: manifest.entities.sha256,
            manifest_key: None,
        }
        supplied = {artifact.reference.object_key: artifact for artifact in artifacts}
        for object_key, expected_hash in required.items():
            artifact = supplied.get(object_key)
            if artifact is None or (
                expected_hash is not None and artifact.reference.sha256 != expected_hash
            ):
                msg = f"publication is missing required final artifact: {object_key}"
                raise RepositoryError(msg)
        target_state = (
            BatchItemState.COMPLETED
            if manifest.quality == QualityState.PASS
            else BatchItemState.COMPLETED_WITH_WARNINGS
        )
        with self._session_factory.begin() as session:
            item = self._locked_item(session, item_id)
            batch = self._locked_batch(session, item.batch_id)
            if batch.state == BatchState.CANCELLED or item.cancellation_requested_at is not None:
                raise RepositoryError("cannot publish output for a cancelled batch item")
            if item.final_manifest_key is not None:
                if item.final_manifest_key == manifest_key:
                    return
                msg = f"item {item_id} already points to a different final bundle"
                raise RepositoryError(msg)
            for artifact in artifacts:
                existing = session.scalar(
                    select(ArtifactModel)
                    .where(ArtifactModel.object_key == artifact.reference.object_key)
                    .with_for_update()
                )
                if existing is None:
                    session.add(
                        ArtifactModel(
                            object_key=artifact.reference.object_key,
                            sha256=artifact.reference.sha256,
                            media_type=artifact.reference.media_type,
                            size_bytes=artifact.size_bytes,
                            retention=artifact.retention,
                        )
                    )
                elif existing.sha256 != artifact.reference.sha256:
                    msg = f"immutable artifact key collision: {artifact.reference.object_key}"
                    raise RepositoryError(msg)
            session.add(
                EntityResultModel(
                    batch_item_id=item_id,
                    schema_version=schema_version,
                    payload={"entities": [entity.model_dump(mode="json") for entity in entities]},
                )
            )
            require_item_transition(item.state, target_state)
            item.state = target_state
            item.quality = manifest.quality
            item.final_bundle_prefix = prefix
            item.final_manifest_key = manifest_key

    def set_batch_state(self, *, batch_id: UUID, state: BatchState) -> None:
        """Apply a checked batch lifecycle transition."""
        with self._session_factory.begin() as session:
            batch = self._locked_batch(session, batch_id)
            if batch.state != state:
                require_batch_transition(batch.state, state)
                batch.state = state

    def set_item_state(self, *, item_id: UUID, state: BatchItemState) -> None:
        """Apply a checked item lifecycle transition."""
        with self._session_factory.begin() as session:
            item = self._locked_item(session, item_id)
            if item.state != state:
                require_item_transition(item.state, state)
                item.state = state

    def get_batch_status(self, batch_id: UUID) -> dict[str, object]:
        """Return PostgreSQL-only aggregate state for a submitted batch."""
        with self._session_factory() as session:
            batch = session.get(BatchModel, batch_id)
            if batch is None:
                raise RepositoryError(f"unknown batch: {batch_id}")
            items = list(session.scalars(select(BatchItemModel).where(BatchItemModel.batch_id == batch_id)))
            counts: dict[str, int] = {}
            for item in items:
                counts[item.state.value] = counts.get(item.state.value, 0) + 1
            active_jobs = session.scalar(
                select(func.count()).select_from(JobModel).join(BatchItemModel).where(
                    BatchItemModel.batch_id == batch_id, JobModel.state == JobState.RUNNING
                )
            )
            return {"batch_id": str(batch.id), "state": batch.state.value, "item_counts": counts, "active_jobs": int(active_jobs or 0)}

    def get_batch_report(self, batch_id: UUID) -> list[dict[str, object]]:
        """Return every scanned item deterministically, including skipped/reused paths."""
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(BatchItemModel)
                    .where(BatchItemModel.batch_id == batch_id)
                    .order_by(BatchItemModel.root_path, BatchItemModel.source_path)
                )
            )
            if not rows and session.get(BatchModel, batch_id) is None:
                raise RepositoryError(f"unknown batch: {batch_id}")
            report: list[dict[str, object]] = []
            for row in rows:
                attempts = session.scalar(
                    select(func.coalesce(func.max(JobModel.attempt_count), 0)).where(
                        JobModel.batch_item_id == row.id
                    )
                )
                report.append(
                    {
                        "item_id": str(row.id),
                        "root": row.root_path,
                        "path": row.source_path,
                        "state": row.state.value,
                        "reason": row.scan_reason or row.quarantine_reason,
                        "quality": None if row.quality is None else row.quality.value,
                        "source_sha256": None if row.document is None else row.document.source_sha256,
                        "attempts": int(attempts or 0),
                        "final_manifest_key": row.final_manifest_key,
                    }
                )
            return report

    def cancel_batch(self, batch_id: UUID, now: datetime | None = None) -> None:
        """Prevent future claims and request cooperative cancellation for running work."""
        current_time = now or self._clock()
        with self._session_factory.begin() as session:
            batch = self._locked_batch(session, batch_id)
            if batch.state != BatchState.CANCELLED:
                batch.state = BatchState.CANCELLED
            items = list(session.scalars(select(BatchItemModel).where(BatchItemModel.batch_id == batch_id).with_for_update()))
            for item in items:
                if item.state in {BatchItemState.QUEUED, BatchItemState.RUNNING}:
                    item.cancellation_requested_at = current_time
                    if item.state == BatchItemState.QUEUED:
                        item.state = BatchItemState.CANCELLED
            session.execute(update(JobModel).where(JobModel.batch_item_id.in_([item.id for item in items]), JobModel.state == JobState.PENDING).values(state=JobState.CANCELLED, completed_at=current_time))

    def retry_quarantined_item(self, item_id: UUID) -> UUID:
        """Create a new root snapshot job for one quarantined item without altering final output."""
        with self._session_factory.begin() as session:
            item = self._locked_item(session, item_id)
            if item.state != BatchItemState.QUARANTINED:
                raise RepositoryError("only quarantined items can be retried")
            item.state = BatchItemState.QUEUED
            item.quarantine_reason = None
            item.cancellation_requested_at = None
            if item.source_object_key is None or item.document is None:
                raise RepositoryError("quarantined item has no immutable source object for retry")
            prior_retries = session.scalar(
                select(func.count())
                .select_from(JobModel)
                .where(JobModel.batch_item_id == item.id, JobModel.stage == "source_snapshot")
            )
            return self._enqueue_in_session(
                session,
                item.id,
                "source_snapshot",
                {
                    "source_object_key": item.source_object_key,
                    "source_object_sha256": item.document.source_sha256,
                },
                3,
                None,
                suffix=f"retry-{int(prior_retries or 0)}",
            )

    def record_vision_output(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        manifest: StoredArtifact,
        artifacts: tuple[StoredArtifact, ...],
    ) -> None:
        """Persist page artifacts and manifest while the source job lease is still owned."""
        current_time = self._clock()
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, worker_id, current_time)
            assert job is not None
            for artifact in (*artifacts, manifest):
                existing = session.scalar(
                    select(ArtifactModel)
                    .where(ArtifactModel.object_key == artifact.reference.object_key)
                    .with_for_update()
                )
                if existing is None:
                    session.add(
                        ArtifactModel(
                            producing_job_id=job_id,
                            object_key=artifact.reference.object_key,
                            sha256=artifact.reference.sha256,
                            media_type=artifact.reference.media_type,
                            size_bytes=artifact.size_bytes,
                            retention=artifact.retention,
                        )
                    )
                elif existing.sha256 != artifact.reference.sha256:
                    raise RepositoryError(
                        f"vision artifact key collision: {artifact.reference.object_key}"
                    )
            job.payload = {
                **job.payload,
                "render_manifest_key": manifest.reference.object_key,
                "render_manifest_sha256": manifest.reference.sha256,
            }

    def record_layout_output(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        raw_mineru: StoredArtifact,
        manifest: StoredArtifact,
        crops: tuple[StoredArtifact, ...],
    ) -> None:
        """Persist raw vendor data and content-free normalized layout under the owned lease."""
        current_time = self._clock()
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, worker_id, current_time)
            assert job is not None
            for artifact in (raw_mineru, *crops, manifest):
                existing = session.scalar(
                    select(ArtifactModel)
                    .where(ArtifactModel.object_key == artifact.reference.object_key)
                    .with_for_update()
                )
                if existing is None:
                    session.add(
                        ArtifactModel(
                            producing_job_id=job_id,
                            object_key=artifact.reference.object_key,
                            sha256=artifact.reference.sha256,
                            media_type=artifact.reference.media_type,
                            size_bytes=artifact.size_bytes,
                            retention=artifact.retention,
                        )
                    )
                elif existing.sha256 != artifact.reference.sha256:
                    raise RepositoryError(
                        f"layout artifact key collision: {artifact.reference.object_key}"
                    )
            job.payload = {
                **job.payload,
                "raw_mineru_key": raw_mineru.reference.object_key,
                "raw_mineru_sha256": raw_mineru.reference.sha256,
                "layout_manifest_key": manifest.reference.object_key,
                "layout_manifest_sha256": manifest.reference.sha256,
            }

    def record_ocr_output(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        manifest: StoredArtifact,
        line_crops: tuple[StoredArtifact, ...],
    ) -> None:
        """Persist OCR artifacts under the job lease before allowing dependency progression."""
        current_time = self._clock()
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, worker_id, current_time)
            assert job is not None
            for artifact in (*line_crops, manifest):
                existing = session.scalar(
                    select(ArtifactModel)
                    .where(ArtifactModel.object_key == artifact.reference.object_key)
                    .with_for_update()
                )
                if existing is None:
                    session.add(
                        ArtifactModel(
                            producing_job_id=job_id,
                            object_key=artifact.reference.object_key,
                            sha256=artifact.reference.sha256,
                            media_type=artifact.reference.media_type,
                            size_bytes=artifact.size_bytes,
                            retention=artifact.retention,
                        )
                    )
                elif existing.sha256 != artifact.reference.sha256:
                    raise RepositoryError(
                        f"OCR artifact key collision: {artifact.reference.object_key}"
                    )
            job.payload = {
                **job.payload,
                "ocr_manifest_key": manifest.reference.object_key,
                "ocr_manifest_sha256": manifest.reference.sha256,
            }

    def record_reconstruction_output(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        markdown: StoredArtifact,
        manifest: StoredArtifact,
    ) -> None:
        """Persist one grounded document reconstruction under the active GPU0 job lease."""
        current_time = self._clock()
        with self._session_factory.begin() as session:
            job = session.get(JobModel, job_id, with_for_update=True)
            self._require_active_lease(job, worker_id, current_time)
            assert job is not None
            for artifact in (markdown, manifest):
                existing = session.scalar(
                    select(ArtifactModel)
                    .where(ArtifactModel.object_key == artifact.reference.object_key)
                    .with_for_update()
                )
                if existing is None:
                    session.add(
                        ArtifactModel(
                            producing_job_id=job_id,
                            object_key=artifact.reference.object_key,
                            sha256=artifact.reference.sha256,
                            media_type=artifact.reference.media_type,
                            size_bytes=artifact.size_bytes,
                            retention=artifact.retention,
                        )
                    )
                elif existing.sha256 != artifact.reference.sha256:
                    raise RepositoryError(
                        f"reconstruction artifact key collision: {artifact.reference.object_key}"
                    )
            job.payload = {
                **job.payload,
                "reconstructed_markdown_key": markdown.reference.object_key,
                "reconstructed_markdown_sha256": markdown.reference.sha256,
                "reconstruction_manifest_key": manifest.reference.object_key,
                "reconstruction_manifest_sha256": manifest.reference.sha256,
            }

    @staticmethod
    def _get_or_create_document(session: Session, source_sha256: str) -> UUID:
        identifier = session.scalar(
            insert(DocumentModel)
            .values(source_sha256=source_sha256)
            .on_conflict_do_nothing(index_elements=[DocumentModel.source_sha256])
            .returning(DocumentModel.id)
        )
        if identifier is not None:
            return identifier
        existing = session.scalar(
            select(DocumentModel.id).where(DocumentModel.source_sha256 == source_sha256)
        )
        if existing is None:
            msg = f"document insert conflicted but {source_sha256} is unavailable"
            raise RepositoryError(msg)
        return existing

    @staticmethod
    def _reusable_items(session: Session, profile_hash: str) -> dict[str, BatchItemModel]:
        rows = session.scalars(select(BatchItemModel).join(BatchModel).join(PipelineProfileModel).join(DocumentModel).where(PipelineProfileModel.profile_hash == profile_hash, BatchItemModel.state.in_((BatchItemState.COMPLETED, BatchItemState.COMPLETED_WITH_WARNINGS)), BatchItemModel.final_manifest_key.is_not(None))).all()
        return {row.document.source_sha256: row for row in rows if row.document is not None}

    @staticmethod
    def _enqueue_in_session(session: Session, batch_item_id: UUID, stage: str, payload: dict[str, object], max_attempts: int, depends_on: UUID | None, suffix: str = "root") -> UUID:
        key = f"{batch_item_id}:{stage}:{depends_on or suffix}"
        job = JobModel(batch_item_id=batch_item_id, depends_on_id=depends_on, stage=stage, idempotency_key=key, payload=payload, state=JobState.PENDING, priority=0, attempt_count=0, max_attempts=max_attempts)
        session.add(job)
        session.flush()
        return job.id

    @staticmethod
    def _locked_item(session: Session, item_id: UUID) -> BatchItemModel:
        item = session.get(BatchItemModel, item_id, with_for_update=True)
        if item is None:
            msg = f"unknown batch item: {item_id}"
            raise RepositoryError(msg)
        return item

    @staticmethod
    def _locked_batch(session: Session, batch_id: UUID) -> BatchModel:
        batch = session.get(BatchModel, batch_id, with_for_update=True)
        if batch is None:
            msg = f"unknown batch: {batch_id}"
            raise RepositoryError(msg)
        return batch

    @staticmethod
    def _require_active_lease(job: JobModel | None, worker_id: str, now: datetime) -> None:
        if (
            job is None
            or job.state != JobState.RUNNING
            or job.lease_owner != worker_id
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            msg = "job lease is absent, expired, or owned by another worker"
            raise LeaseOwnershipError(msg)

    @staticmethod
    def _finish_stage_run(
        session: Session,
        job: JobModel,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        run = session.scalar(
            select(StageRunModel)
            .where(StageRunModel.job_id == job.id, StageRunModel.attempt == job.attempt_count)
            .with_for_update()
        )
        if run is not None:
            run.finished_at = now
            run.error_code = error_code
            run.error_detail = error_detail

    @staticmethod
    def _release_resources(session: Session, job_id: UUID, owner: str, now: datetime) -> None:
        session.execute(
            update(ResourceReservationModel)
            .where(
                ResourceReservationModel.job_id == job_id,
                ResourceReservationModel.owner == owner,
                ResourceReservationModel.released_at.is_(None),
            )
            .values(released_at=now)
        )

    def _quarantine_item(
        self, session: Session, item_id: UUID, reason: str, now: datetime
    ) -> None:
        item = self._locked_item(session, item_id)
        if item.state in {BatchItemState.QUEUED, BatchItemState.RUNNING}:
            require_item_transition(item.state, BatchItemState.QUARANTINED)
            item.state = BatchItemState.QUARANTINED
        item.quarantine_reason = reason
        session.execute(
            update(JobModel)
            .where(JobModel.batch_item_id == item_id, JobModel.state == JobState.PENDING)
            .values(state=JobState.CANCELLED, completed_at=now)
        )
