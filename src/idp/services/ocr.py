"""Offline PaddleOCR line recognition inside MinerU text-bearing blocks only."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from PIL import Image

from idp.domain.models import ArtifactReference, StoredArtifact
from idp.domain.states import ArtifactRetention, JobState
from idp.persistence.repository import SqlAlchemyBatchRepository
from idp.ports.artifact_store import ArtifactStore
from idp.services.mineru import LayoutBlock, LayoutManifest


class OcrError(RuntimeError):
    """Raised for invalid local OCR output or unavailable configured OCR components."""


class OcrRoute(StrEnum):
    """Only recognition paths approved for the first production profile."""

    EAST_SLAVIC = "east_slavic"
    CYRILLIC = "cyrillic"
    LATIN_CJK = "latin_cjk"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class DetectorLine:
    """One line bbox in local block-crop coordinates."""

    bbox: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class RecognizedToken:
    """Recognizer token geometry in local line-crop coordinates."""

    raw_text: str
    bbox: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class ScriptDecision:
    """Script/language decision made before applying a recognizer."""

    route: OcrRoute
    script: str
    language: str
    confidence: float
    reason: str | None = None


@dataclass(frozen=True)
class RecognizerProfile:
    """Pinned local model identity reported for every OCR token."""

    route: OcrRoute
    model_id: str
    revision: str


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


class LineDetector(Protocol):
    """Local PP-OCRv5 detector operating only on a supplied text-block crop."""

    def detect(self, block_crop: Path) -> tuple[DetectorLine, ...]:
        """Return local block-crop line bboxes in visual reading order."""


class ScriptRouter(Protocol):
    """Local script router that chooses one recognition path for a line crop."""

    def route(self, line_crop: Path) -> ScriptDecision:
        """Return an explicit route without contacting external language services."""


class LineRecognizer(Protocol):
    """Local Paddle recognizer operating only on a detector-provided line crop."""

    def recognize(self, line_crop: Path) -> tuple[RecognizedToken, ...]:
        """Return token text, token geometry, and confidence in line-crop coordinates."""


class CommandJsonComponent:
    """Pinned local command bridge with explicit input/output placeholders and JSON output."""

    def __init__(self, command: tuple[str, ...], working_directory: Path | None = None) -> None:
        if not command:
            raise ValueError("local OCR command must not be empty")
        self._command = command
        self._working_directory = working_directory

    def _run(self, input_path: Path) -> Any:
        with tempfile.TemporaryDirectory(prefix="idp-paddle-") as temporary:
            output_path = Path(temporary) / "output.json"
            command = [
                argument.format(input=str(input_path), output=str(output_path))
                for argument in self._command
            ]
            try:
                subprocess.run(
                    command,
                    cwd=self._working_directory,
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=600,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise OcrError(f"local PaddleOCR command failed: {error}") from error
            if not output_path.is_file():
                raise OcrError("local PaddleOCR command did not create JSON output")
            try:
                return json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise OcrError(f"local PaddleOCR output is invalid JSON: {error}") from error


class CommandPaddleLineDetector(CommandJsonComponent):
    """Command adapter for PP-OCRv5 server detector JSON output."""

    def detect(self, block_crop: Path) -> tuple[DetectorLine, ...]:
        output = self._run(block_crop)
        entries = _entries(output, "lines")
        return tuple(
            DetectorLine(bbox=_bbox(entry), confidence=_confidence(entry)) for entry in entries
        )


class CommandScriptRouter(CommandJsonComponent):
    """Command adapter for a pinned local script/language classifier."""

    def route(self, line_crop: Path) -> ScriptDecision:
        output = self._run(line_crop)
        if not isinstance(output, Mapping):
            raise OcrError("script router output must be an object")
        try:
            route = OcrRoute(str(output["route"]))
            script = str(output["script"])
            language = str(output["language"])
        except (KeyError, ValueError) as error:
            raise OcrError("script router output lacks route/script/language") from error
        return ScriptDecision(
            route=route,
            script=script,
            language=language,
            confidence=_confidence(output),
            reason=None if output.get("reason") is None else str(output["reason"]),
        )


class CommandPaddleRecognizer(CommandJsonComponent):
    """Command adapter for a route-specific local Paddle recognition model."""

    def recognize(self, line_crop: Path) -> tuple[RecognizedToken, ...]:
        output = self._run(line_crop)
        entries = _entries(output, "tokens")
        tokens: list[RecognizedToken] = []
        for entry in entries:
            raw_text = entry.get("text")
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise OcrError("recognizer token lacks text")
            tokens.append(
                RecognizedToken(
                    raw_text=raw_text,
                    bbox=_bbox(entry),
                    confidence=_confidence(entry),
                )
            )
        return tuple(tokens)


class OcrProcessor:
    """Detect lines and recognize text only inside text-bearing MinerU blocks."""

    TEXT_BEARING_TYPES = frozenset(
        {
            "text",
            "title",
            "list",
            "header",
            "footer",
            "footnote",
            "caption",
            "table_cell",
        }
    )

    def __init__(
        self,
        *,
        detector: LineDetector,
        router: ScriptRouter,
        recognizers: Mapping[OcrRoute, tuple[LineRecognizer, RecognizerProfile]],
        artifacts: ArtifactStore,
        max_lines_per_block: int,
        min_token_confidence: float,
    ) -> None:
        if OcrRoute.UNSUPPORTED in recognizers:
            raise ValueError("unsupported script route must not have a recognizer")
        self._detector = detector
        self._router = router
        self._recognizers = recognizers
        self._artifacts = artifacts
        self._max_lines = max_lines_per_block
        self._min_token_confidence = min_token_confidence

    def process(
        self, *, layout: LayoutManifest, layout_reference: ArtifactReference, artifact_prefix: str
    ) -> tuple[OcrManifest, tuple[StoredArtifact, ...]]:
        """Produce page-grounded OCR without changing MinerU block order or geometry."""
        tokens: list[OcrToken] = []
        findings: list[OcrFinding] = []
        line_artifacts: list[StoredArtifact] = []
        for block in layout.blocks:
            if block.kind not in self.TEXT_BEARING_TYPES:
                continue
            block_tokens, block_findings, artifacts = self._process_block(
                block, f"{artifact_prefix.rstrip('/')}/ocr/{block.block_id}"
            )
            tokens.extend(block_tokens)
            findings.extend(block_findings)
            line_artifacts.extend(artifacts)
        manifest = OcrManifest(
            schema_version="ocr-manifest-v1",
            source_sha256=layout.source_sha256,
            layout_manifest=layout_reference,
            tokens=tuple(tokens),
            findings=tuple(findings),
        )
        return manifest, tuple(line_artifacts)

    def _process_block(
        self, block: LayoutBlock, artifact_prefix: str
    ) -> tuple[list[OcrToken], list[OcrFinding], list[StoredArtifact]]:
        tokens: list[OcrToken] = []
        findings: list[OcrFinding] = []
        line_artifacts: list[StoredArtifact] = []
        with tempfile.TemporaryDirectory(prefix="idp-ocr-") as temporary:
            root = Path(temporary)
            block_image = root / "block.png"
            self._artifacts.get_file(block.crop, block_image)
            with Image.open(block_image) as image:
                lines = self._detector.detect(block_image)
                if len(lines) > self._max_lines:
                    raise OcrError(f"OCR line limit exceeded for block {block.block_id}")
                for line_index, line in enumerate(lines):
                    local_bbox = _bounded_bbox(line.bbox, image.width, image.height)
                    line_image = image.crop(local_bbox)
                    local_path = root / f"line-{line_index:04d}.png"
                    line_image.save(local_path, format="PNG", optimize=False)
                    line_artifact = self._artifacts.put_bytes(
                        object_key=f"{artifact_prefix}/lines/{line_index:04d}.png",
                        payload=local_path.read_bytes(),
                        media_type="image/png",
                        retention=ArtifactRetention.TEMPORARY,
                    )
                    line_artifacts.append(line_artifact)
                    decision = self._router.route(local_path)
                    if decision.route == OcrRoute.UNSUPPORTED:
                        findings.append(
                            OcrFinding(
                                block_id=block.block_id,
                                page_number=block.page_number,
                                bbox=_to_page_bbox(block.bbox, local_bbox),
                                code="unsupported_script",
                                detail=decision.reason or f"unsupported script: {decision.script}",
                                crop=line_artifact.reference,
                            )
                        )
                        continue
                    recognizer_data = self._recognizers.get(decision.route)
                    if recognizer_data is None:
                        raise OcrError(f"no recognizer configured for route {decision.route}")
                    recognizer, profile = recognizer_data
                    for token_index, token in enumerate(recognizer.recognize(local_path)):
                        if token.confidence < self._min_token_confidence:
                            continue
                        token_bbox = _bounded_bbox(token.bbox, line_image.width, line_image.height)
                        page_bbox = _to_page_bbox(
                            block.bbox,
                            (
                                local_bbox[0] + token_bbox[0],
                                local_bbox[1] + token_bbox[1],
                                local_bbox[0] + token_bbox[2],
                                local_bbox[1] + token_bbox[3],
                            ),
                        )
                        raw = token.raw_text
                        tokens.append(
                            OcrToken(
                                token_id=f"{block.block_id}-l{line_index:04d}-t{token_index:04d}",
                                block_id=block.block_id,
                                page_number=block.page_number,
                                bbox=page_bbox,
                                raw_text=raw,
                                normalized_text=_normalize_text(raw),
                                confidence=token.confidence,
                                detector_confidence=line.confidence,
                                script=decision.script,
                                language=decision.language,
                                model_id=profile.model_id,
                                model_revision=profile.revision,
                                line_crop=line_artifact.reference,
                            )
                        )
        return tokens, findings, line_artifacts


class OcrStageHandler:
    """Persist OCR manifest and line crops while owning the layout-stage job lease."""

    def __init__(
        self,
        processor: OcrProcessor,
        artifacts: ArtifactStore,
        repository: SqlAlchemyBatchRepository,
    ) -> None:
        self._processor = processor
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
        """Run OCR once per text-bearing block and close job only after artifact persistence."""
        manifest, line_artifacts = self._processor.process(
            layout=layout,
            layout_reference=layout_reference,
            artifact_prefix=artifact_prefix,
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
            line_crops=line_artifacts,
        )
        self._repository.complete_job(job_id=job_id, worker_id=worker_id, state=JobState.SUCCEEDED)
        return manifest


def _entries(output: Any, key: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(output, Mapping) or not isinstance(output.get(key), list):
        raise OcrError(f"local OCR output must contain {key} list")
    entries = output[key]
    assert isinstance(entries, list)
    if not all(isinstance(entry, Mapping) for entry in entries):
        raise OcrError(f"local OCR {key} entries must be objects")
    return tuple(entries)


def _bbox(value: Mapping[str, Any]) -> tuple[float, float, float, float]:
    raw = value.get("bbox")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 4:
        raise OcrError("local OCR entry must contain four-number bbox")
    try:
        x0, y0, x1, y1 = (float(coordinate) for coordinate in raw)
    except (TypeError, ValueError) as error:
        raise OcrError("local OCR bbox is invalid") from error
    if x1 <= x0 or y1 <= y0:
        raise OcrError("local OCR bbox has no area")
    return x0, y0, x1, y1


def _confidence(value: Mapping[str, Any]) -> float:
    raw = value.get("confidence", value.get("score", 1.0))
    try:
        confidence = float(raw)
    except (TypeError, ValueError) as error:
        raise OcrError("local OCR confidence is invalid") from error
    if not 0 <= confidence <= 1:
        raise OcrError("local OCR confidence must be between zero and one")
    return confidence


def _bounded_bbox(
    bbox: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    left = max(0, min(width, round(x0)))
    top = max(0, min(height, round(y0)))
    right = min(width, max(left + 1, round(x1)))
    bottom = min(height, max(top + 1, round(y1)))
    if right <= left or bottom <= top:
        raise OcrError(f"OCR bbox is outside crop bounds: {bbox}")
    return left, top, right, bottom


def _to_page_bbox(
    block_bbox: tuple[float, float, float, float],
    local_bbox: tuple[int, int, int, int],
) -> tuple[float, float, float, float]:
    return (
        block_bbox[0] + local_bbox[0],
        block_bbox[1] + local_bbox[1],
        block_bbox[0] + local_bbox[2],
        block_bbox[1] + local_bbox[3],
    )


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


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
