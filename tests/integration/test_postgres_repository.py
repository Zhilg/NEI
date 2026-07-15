"""Optional target-like PostgreSQL checks; no database is required on developer laptops."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from idp.domain.models import BatchItemSnapshot, BatchSnapshot, ResourceRequest
from idp.domain.models import ArtifactReference, Entity, FinalManifest, StoredArtifact
from idp.domain.states import ArtifactRetention, BatchItemState, JobState, QualityState, ReservationKind
from idp.persistence.base import Base
from idp.persistence.models import BatchItemModel, JobModel
from idp.persistence.repository import ResourceCapacityError, SqlAlchemyBatchRepository

POSTGRES_URL = os.getenv("IDP_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="requires IDP_TEST_POSTGRES_URL")


@pytest.fixture
def repository() -> SqlAlchemyBatchRepository:
    assert POSTGRES_URL is not None
    engine = create_engine(POSTGRES_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    repository = SqlAlchemyBatchRepository(factory)
    repository.register_profile(name="test", profile_hash="a" * 64)
    yield repository
    Base.metadata.drop_all(engine)
    engine.dispose()


def _snapshot(path_suffix: str) -> BatchSnapshot:
    return BatchSnapshot(
        profile_name="test",
        roots=(Path("/data/incoming"),),
        items=(
            BatchItemSnapshot(
                root=Path("/data/incoming"),
                path=Path(f"/data/incoming/{path_suffix}.pdf"),
                source_sha256="b" * 64,
                state=BatchItemState.QUEUED,
                observed_at=datetime.now(UTC),
            ),
        ),
    )


def test_expired_final_attempt_quarantines_item(repository: SqlAlchemyBatchRepository) -> None:
    snapshot = _snapshot("source")
    repository.create_batch(snapshot, "a" * 64)
    job_id = repository.enqueue_job(
        batch_item_id=snapshot.items[0].item_id,
        stage="render",
        payload={},
        max_attempts=1,
    )
    now = datetime.now(UTC)
    assert repository.claim_next_job(
        worker_id="worker-a", lease_duration=timedelta(seconds=1), now=now
    )

    assert repository.requeue_expired_jobs(now=now + timedelta(seconds=2)) == 1

    factory = repository._session_factory  # type: ignore[attr-defined]
    with factory() as session:
        item = session.get(BatchItemModel, snapshot.items[0].item_id)
        job = session.get(JobModel, job_id)
        assert item is not None and item.state == BatchItemState.QUARANTINED
        assert job is not None and job.state == JobState.FAILED


def test_resource_pool_prevents_gpu_oversubscription(repository: SqlAlchemyBatchRepository) -> None:
    first = _snapshot("first")
    second = _snapshot("second")
    repository.create_batch(first, "a" * 64)
    repository.create_batch(second, "a" * 64)
    first_job = repository.enqueue_job(
        batch_item_id=first.items[0].item_id, stage="layout", payload={}, max_attempts=1
    )
    second_job = repository.enqueue_job(
        batch_item_id=second.items[0].item_id, stage="layout", payload={}, max_attempts=1
    )
    now = datetime.now(UTC)
    first_claim = repository.claim_next_job(
        worker_id="worker-a", lease_duration=timedelta(minutes=1), now=now
    )
    assert first_claim is not None and first_claim.job_id == first_job
    repository.configure_resource_pool(kind=ReservationKind.GPU1, capacity=40, unit="gib")
    repository.reserve_resources(
        job_id=first_job,
        owner="worker-a",
        requests=(ResourceRequest(kind=ReservationKind.GPU1, amount=40, unit="gib"),),
        lease_duration=timedelta(minutes=1),
        now=now,
    )
    second_claim = repository.claim_next_job(
        worker_id="worker-b", lease_duration=timedelta(minutes=1), now=now
    )
    assert second_claim is not None and second_claim.job_id == second_job
    with pytest.raises(ResourceCapacityError):
        repository.reserve_resources(
            job_id=second_job,
            owner="worker-b",
            requests=(ResourceRequest(kind=ReservationKind.GPU1, amount=1, unit="gib"),),
            lease_duration=timedelta(minutes=1),
            now=now,
        )


def test_compatible_published_output_is_reused_in_next_batch(
    repository: SqlAlchemyBatchRepository,
) -> None:
    first = _snapshot("first")
    repository.create_batch(first, "a" * 64)
    item_id = first.items[0].item_id
    repository.set_item_state(item_id=item_id, state=BatchItemState.RUNNING)
    source_sha = "b" * 64
    markdown = ArtifactReference(
        object_key="final/one/final.md", sha256="c" * 64, media_type="text/markdown"
    )
    entities = ArtifactReference(
        object_key="final/one/entities.json", sha256="d" * 64, media_type="application/json"
    )
    manifest = FinalManifest(
        source_sha256=source_sha,
        pipeline_profile_hash="a" * 64,
        quality=QualityState.PASS,
        final_markdown=markdown,
        entities=entities,
    )
    repository.commit_publication(
        item_id=item_id,
        bundle_prefix="final/one",
        manifest=manifest,
        artifacts=(
            StoredArtifact(reference=markdown, size_bytes=1, retention=ArtifactRetention.FINAL),
            StoredArtifact(reference=entities, size_bytes=1, retention=ArtifactRetention.FINAL),
            StoredArtifact(
                reference=ArtifactReference(
                    object_key="final/one/manifest.json",
                    sha256="e" * 64,
                    media_type="application/json",
                ),
                size_bytes=1,
                retention=ArtifactRetention.FINAL,
            ),
        ),
        entities=(),
        schema_version="v1",
    )
    second = _snapshot("second")
    repository.create_batch(second, "a" * 64)

    report = repository.get_batch_report(second.batch_id)

    assert report[0]["state"] == BatchItemState.REUSED.value
    assert report[0]["final_manifest_key"] == "final/one/manifest.json"


def test_cancel_includes_every_path_in_report(repository: SqlAlchemyBatchRepository) -> None:
    snapshot = _snapshot("cancelled")
    repository.create_batch(snapshot, "a" * 64)

    repository.cancel_batch(snapshot.batch_id)

    assert repository.get_batch_status(snapshot.batch_id)["state"] == "cancelled"
    assert repository.get_batch_report(snapshot.batch_id)[0]["state"] == BatchItemState.CANCELLED.value
