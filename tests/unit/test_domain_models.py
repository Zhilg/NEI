from datetime import UTC, datetime

from idp.domain.models import ArtifactReference, BatchItemSnapshot, FinalManifest
from idp.domain.states import BatchItemState, QualityState


SHA256 = "a" * 64


def test_final_manifest_is_serializable_with_immutable_artifacts() -> None:
    artifact = ArtifactReference(
        object_key="results/document/final.md",
        sha256=SHA256,
        media_type="text/markdown",
    )
    manifest = FinalManifest(
        source_sha256=SHA256,
        pipeline_profile_hash="b" * 64,
        quality=QualityState.PASS,
        final_markdown=artifact,
        entities=ArtifactReference(
            object_key="results/document/entities.json",
            sha256="c" * 64,
            media_type="application/json",
        ),
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
    )

    assert manifest.model_dump(mode="json")["quality"] == "pass"


def test_skipped_item_does_not_require_source_hash() -> None:
    item = BatchItemSnapshot(
        root="/data/incoming",
        path="/data/incoming/link.pdf",
        state=BatchItemState.SKIPPED_SYMLINK,
    )

    assert item.source_sha256 is None
