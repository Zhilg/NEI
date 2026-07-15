from pathlib import Path
from uuid import UUID, uuid4

from idp.domain.models import Entity, FinalManifest, StoredArtifact
from idp.domain.states import QualityState
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
    ) -> None:
        self.publication = {
            "item_id": item_id,
            "bundle_prefix": bundle_prefix,
            "manifest": manifest,
            "artifacts": artifacts,
            "entities": entities,
            "schema_version": schema_version,
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
