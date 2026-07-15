from pathlib import Path
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from idp.domain.models import ArtifactReference, Entity, FinalManifest, StoredArtifact
from idp.domain.states import ArtifactRetention, QualityState
from idp.services.publication import FinalBundlePublisher
from idp.storage import LocalArtifactStore


class RecordingRepository:
    def __init__(self) -> None:
        self.publication: dict[str, object] | None = None

    def commit_publication(
        self,
        *,
        item_id: UUID,
        bundle_prefix: str,
        manifest: FinalManifest,
        artifacts: tuple[StoredArtifact, ...],
        entities: tuple[Entity, ...],
        schema_version: str,
        job_id: UUID | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.publication = {
            "item_id": item_id,
            "bundle_prefix": bundle_prefix,
            "manifest": manifest,
            "artifacts": artifacts,
            "entities": entities,
            "schema_version": schema_version,
            "job_id": job_id,
            "worker_id": worker_id,
        }


def test_publisher_writes_all_bundle_objects_before_database_commit(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    repository = RecordingRepository()
    publisher = FinalBundlePublisher(store, repository)  # type: ignore[arg-type]
    entity = Entity(
        type="organization",
        value="ООО Пример",
        page=1,
        block_id="block-1",
        bbox=(0, 0, 1, 1),
        evidence="ООО Пример",
        confidence=0.9,
    )

    manifest = publisher.publish(
        item_id=uuid4(),
        bundle_prefix="results/document/version-1",
        source_sha256="a" * 64,
        pipeline_profile_hash="b" * 64,
        quality=QualityState.WARNING,
        markdown="# Результат\n",
        entities=(entity,),
        schema_version="entity-v1",
    )

    assert store.exists(manifest.final_markdown)
    assert store.exists(manifest.entities)
    assert repository.publication is not None
    artifacts = repository.publication["artifacts"]
    assert isinstance(artifacts, tuple)
    assert {artifact.reference.object_key.rsplit("/", 1)[-1] for artifact in artifacts} == {
        "final.md",
        "entities.json",
        "manifest.json",
    }


def test_publisher_keeps_manifest_bytes_stable_for_a_retry(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    repository = RecordingRepository()
    publisher = FinalBundlePublisher(store, repository)  # type: ignore[arg-type]
    item_id = uuid4()
    created_at = datetime(2026, 7, 15, tzinfo=UTC)
    arguments = {
        "item_id": item_id,
        "bundle_prefix": "results/document/stable",
        "source_sha256": "a" * 64,
        "pipeline_profile_hash": "b" * 64,
        "quality": QualityState.PASS,
        "markdown": "Evidence",
        "entities": (),
        "schema_version": "entity-v1",
        "created_at": created_at,
    }

    first = publisher.publish(**arguments)
    second = publisher.publish(**arguments)

    assert first.created_at == second.created_at == created_at


def test_publisher_copies_reconstruction_provenance_into_final_bundle(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    source = store.put_bytes(
        object_key="temporary/reconstruction.json",
        payload=b'{"blocks":[]}',
        media_type="application/json",
        retention=ArtifactRetention.TEMPORARY,
    )
    repository = RecordingRepository()
    publisher = FinalBundlePublisher(store, repository)  # type: ignore[arg-type]

    manifest = publisher.publish(
        item_id=uuid4(),
        bundle_prefix="results/document/provenance",
        source_sha256="a" * 64,
        pipeline_profile_hash="b" * 64,
        quality=QualityState.PASS,
        markdown="Evidence",
        entities=(),
        schema_version="entity-v1",
        reconstruction=source.reference,
    )

    assert manifest.reconstruction == ArtifactReference(
        object_key="results/document/provenance/reconstruction_manifest.json",
        sha256=source.reference.sha256,
        media_type="application/json",
    )
    assert store.exists(manifest.reconstruction)


def test_publisher_rejects_partial_publish_job_ownership(tmp_path: Path) -> None:
    publisher = FinalBundlePublisher(
        LocalArtifactStore(tmp_path / "artifacts"), RecordingRepository()  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="job_id and worker_id"):
        publisher.publish(
            item_id=uuid4(),
            bundle_prefix="results/document/partial-job",
            source_sha256="a" * 64,
            pipeline_profile_hash="b" * 64,
            quality=QualityState.PASS,
            markdown="Evidence",
            entities=(),
            schema_version="entity-v1",
            job_id=uuid4(),
        )
