from pathlib import Path
from unittest.mock import Mock

from idp.domain.models import ArtifactReference
from idp.domain.states import ArtifactRetention, JobState
from idp.services.mineru import LayoutManifest
from idp.services.ocr import OcrStageHandler, ocr_manifest_from_payload
from idp.storage import LocalArtifactStore


def _layout(reference: ArtifactReference) -> LayoutManifest:
    return LayoutManifest(
        schema_version="layout-manifest-v1",
        source_sha256="b" * 64,
        raw_mineru=ArtifactReference(
            object_key="layout/raw.json", sha256="c" * 64, media_type="application/json"
        ),
        pages=(),
        blocks=(),
    )


def test_ocr_stage_persists_empty_compatibility_manifest(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    repository = Mock()
    layout_reference = ArtifactReference(
        object_key="layout/layout_manifest.json", sha256="a" * 64, media_type="application/json"
    )
    handler = OcrStageHandler(store, repository)

    manifest = handler.handle(
        job_id=Mock(),
        worker_id="worker",
        layout=_layout(layout_reference),
        layout_reference=layout_reference,
        artifact_prefix="run",
    )

    assert manifest.layout_manifest == layout_reference
    assert manifest.tokens == ()
    assert manifest.findings == ()
    assert repository.record_ocr_output.call_args.kwargs["line_crops"] == ()
    assert repository.complete_job.call_args.kwargs["state"] == JobState.SUCCEEDED


def test_empty_ocr_manifest_round_trips() -> None:
    reference = ArtifactReference(
        object_key="layout/layout_manifest.json", sha256="a" * 64, media_type="application/json"
    )

    manifest = ocr_manifest_from_payload(
        {
            "schema_version": "ocr-manifest-v1",
            "source_sha256": "b" * 64,
            "layout_manifest": reference.model_dump(mode="json"),
            "tokens": [],
            "findings": [],
        }
    )

    assert manifest.layout_manifest == reference
    assert manifest.tokens == ()
    assert manifest.findings == ()
