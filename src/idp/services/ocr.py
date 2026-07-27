"""OCR-manifest compatibility stage for the MinerU-only pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from idp.domain.models import ArtifactReference
from idp.domain.states import ArtifactRetention, JobState
from idp.persistence.repository import SqlAlchemyBatchRepository
from idp.ports.artifact_store import ArtifactStore
from idp.services.mineru import LayoutManifest


class OcrError(RuntimeError):
    """Raised for invalid local OCR output or unavailable configured OCR components."""


@dataclass(frozen=True)
class OcrToken:
    """A page-grounded OCR token with block and local model provenance."""

    token_id: str
    block_id: str
    page_number: int
    bbox: tuple[float, float, float, float]
    raw_text: str
    normalized_text: str
    confidence: float
    detector_confidence: float
    script: str
    language: str
    model_id: str
    model_revision: str
    line_crop: ArtifactReference


@dataclass(frozen=True)
class OcrFinding:
    """A non-fabricated recognition limitation handed to the later VLM stage."""

    block_id: str
    page_number: int
    bbox: tuple[float, float, float, float]
    code: str
    detail: str
    crop: ArtifactReference


@dataclass(frozen=True)
class OcrManifest:
    """Versioned OCR result with layout reference and full token provenance."""

    schema_version: str
    source_sha256: str
    layout_manifest: ArtifactReference
    tokens: tuple[OcrToken, ...]
    findings: tuple[OcrFinding, ...]


class OcrStageHandler:
    """Persist an empty OCR manifest so the durable stage graph remains compatible."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        repository: SqlAlchemyBatchRepository,
    ) -> None:
        self._artifacts = artifacts
        self._repository = repository

    def handle(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        layout: LayoutManifest,
        layout_reference: ArtifactReference,
        artifact_prefix: str,
    ) -> OcrManifest:
        """Record that Qwen-VL must reconstruct text directly from local page and block images."""
        manifest = OcrManifest(
            schema_version="ocr-manifest-v1",
            source_sha256=layout.source_sha256,
            layout_manifest=layout_reference,
            tokens=(),
            findings=(),
        )
        manifest_artifact = self._artifacts.put_bytes(
            object_key=f"{artifact_prefix.rstrip('/')}/ocr_manifest.json",
            payload=json.dumps(_manifest_payload(manifest), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            media_type="application/json",
            retention=ArtifactRetention.TEMPORARY,
        )
        self._repository.record_ocr_output(
            job_id=job_id,
            worker_id=worker_id,
            manifest=manifest_artifact,
            line_crops=(),
        )
        self._repository.complete_job(job_id=job_id, worker_id=worker_id, state=JobState.SUCCEEDED)
        return manifest


def _manifest_payload(manifest: OcrManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "source_sha256": manifest.source_sha256,
        "layout_manifest": manifest.layout_manifest.model_dump(mode="json"),
        "tokens": [
            {
                "token_id": token.token_id,
                "block_id": token.block_id,
                "page_number": token.page_number,
                "bbox": token.bbox,
                "raw_text": token.raw_text,
                "normalized_text": token.normalized_text,
                "confidence": token.confidence,
                "detector_confidence": token.detector_confidence,
                "script": token.script,
                "language": token.language,
                "model_id": token.model_id,
                "model_revision": token.model_revision,
                "line_crop": token.line_crop.model_dump(mode="json"),
            }
            for token in manifest.tokens
        ],
        "findings": [
            {
                "block_id": finding.block_id,
                "page_number": finding.page_number,
                "bbox": finding.bbox,
                "code": finding.code,
                "detail": finding.detail,
                "crop": finding.crop.model_dump(mode="json"),
            }
            for finding in manifest.findings
        ],
    }


def ocr_manifest_from_payload(payload: Mapping[str, Any]) -> OcrManifest:
    """Load a persisted OCR provenance manifest for the grounded VLM stage."""
    try:
        tokens = tuple(
            OcrToken(
                token_id=str(value["token_id"]),
                block_id=str(value["block_id"]),
                page_number=int(value["page_number"]),
                bbox=tuple(float(coordinate) for coordinate in value["bbox"]),
                raw_text=str(value["raw_text"]),
                normalized_text=str(value["normalized_text"]),
                confidence=float(value["confidence"]),
                detector_confidence=float(value["detector_confidence"]),
                script=str(value["script"]),
                language=str(value["language"]),
                model_id=str(value["model_id"]),
                model_revision=str(value["model_revision"]),
                line_crop=ArtifactReference.model_validate(value["line_crop"]),
            )
            for value in payload["tokens"]
        )
        findings = tuple(
            OcrFinding(
                block_id=str(value["block_id"]),
                page_number=int(value["page_number"]),
                bbox=tuple(float(coordinate) for coordinate in value["bbox"]),
                code=str(value["code"]),
                detail=str(value["detail"]),
                crop=ArtifactReference.model_validate(value["crop"]),
            )
            for value in payload.get("findings", [])
        )
        return OcrManifest(
            schema_version=str(payload["schema_version"]),
            source_sha256=str(payload["source_sha256"]),
            layout_manifest=ArtifactReference.model_validate(payload["layout_manifest"]),
            tokens=tokens,
            findings=findings,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OcrError(f"persisted OCR manifest is invalid: {error}") from error
