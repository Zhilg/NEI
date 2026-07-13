"""Transactional PostgreSQL controller interface."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from idp.domain.models import (
    ArtifactReference,
    ArtifactReference,
    BatchSnapshot,
    Entity,
    FinalManifest,
    JobClaim,
    ResourceRequest,
    StoredArtifact,
)
from idp.domain.states import BatchItemState, BatchState, JobState, ReservationKind


class BatchRepository(Protocol):
    """The controller's durable state and queue operations."""

    def create_batch(
        self,
        snapshot: BatchSnapshot,
        profile_hash: str,
        source_artifacts: dict[UUID, ArtifactReference],
    ) -> None:
        """Persist a snapshot and every discovered item atomically."""

    def register_profile(self, *, name: str, profile_hash: str) -> UUID:
        """Register an immutable pipeline profile and return its identifier."""

    def resolve_profile_hash(self, identifier: str) -> str:
        """Resolve a profile name or immutable profile hash for batch submission."""

    def configure_resource_pool(
        self, *, kind: ReservationKind, capacity: int, unit: str
    ) -> UUID:
        """Create or update a serializable capacity pool."""

    def enqueue_job(
        self,
        *,
        batch_item_id: UUID,
        stage: str,
        payload: dict[str, object],
        max_attempts: int,
        depends_on: UUID | None = None,
    ) -> UUID:
        """Create a deduplicated stage job."""

    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> JobClaim | None:
        """Atomically claim one runnable job with a durable lease."""

    def renew_lease(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> None:
        """Extend the active worker lease and reservations."""

    def complete_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        state: JobState,
        now: datetime | None = None,
    ) -> None:
        """Complete a successful or cancelled job and release its resources."""

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
        """Retry a job or quarantine its item at the terminal failure boundary."""

    def requeue_expired_jobs(self, *, now: datetime | None = None) -> int:
        """Recover work abandoned by a crashed worker after its lease expires."""

    def reserve_resources(
        self,
        *,
        job_id: UUID,
        owner: str,
        requests: tuple[ResourceRequest, ...],
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> None:
        """Reserve all requested bounded resources atomically."""

    def defer_job_for_capacity(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_detail: str,
        now: datetime | None = None,
    ) -> None:
        """Atomically requeue and pause a batch after capacity admission fails."""

    def resume_batch_after_capacity(self, *, batch_id: UUID) -> None:
        """Resume a paused batch only after capacity recovery is confirmed."""

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
        """Catalog artifacts and atomically expose the final output pointer."""

    def set_batch_state(self, *, batch_id: UUID, state: BatchState) -> None:
        """Apply a validated controller lifecycle transition."""

    def set_item_state(self, *, item_id: UUID, state: BatchItemState) -> None:
        """Apply a validated batch-item lifecycle transition."""

    def get_batch_status(self, batch_id: UUID) -> dict[str, object]:
        """Return current batch aggregate without relying on in-memory worker state."""

    def get_batch_report(self, batch_id: UUID) -> list[dict[str, object]]:
        """Return every snapshot item in deterministic report order."""

    def cancel_batch(self, batch_id: UUID, now: datetime | None = None) -> None:
        """Cancel pending jobs and request cooperative cancellation of running work."""

    def retry_quarantined_item(self, item_id: UUID) -> UUID:
        """Requeue one quarantined item through a fresh source snapshot stage."""

    def attach_source_artifacts(self, sources: dict[UUID, ArtifactReference]) -> None:
        """Attach immutable source object references to queued source snapshot jobs."""

    def record_vision_output(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        manifest: StoredArtifact,
        artifacts: tuple[StoredArtifact, ...],
    ) -> None:
        """Catalog render artifacts before an owned vision stage is completed."""
