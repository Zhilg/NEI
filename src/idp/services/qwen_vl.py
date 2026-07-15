"""Single logical Qwen-VL reconstruction and validation run over grounded document inputs."""

from __future__ import annotations

import base64
import json
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse
from uuid import UUID

from idp.domain.models import ArtifactReference, ResourceRequest, StoredArtifact
from idp.domain.states import ArtifactRetention, JobState, ReservationKind
from idp.persistence.repository import ResourceCapacityError, SqlAlchemyBatchRepository
from idp.ports.artifact_store import ArtifactStore
from idp.services.mineru import LayoutBlock, LayoutManifest
from idp.services.ocr import OcrFinding, OcrManifest, OcrToken


class ReconstructionError(RuntimeError):
    """Raised when the local VLM response is unavailable or violates grounded output rules."""


@dataclass(frozen=True)
class GroundedBlock:
    """One VLM-produced Markdown fragment tied exactly to a MinerU layout block."""

    block_id: str
    page_number: int
    bbox: tuple[float, float, float, float]
    markdown: str
    corrections: tuple["OcrCorrection", ...]


@dataclass(frozen=True)
class OcrCorrection:
    """An image-evidenced correction to OCR text, not an ungrounded replacement."""

    token_id: str
    original_text: str
    corrected_text: str
    evidence: str


@dataclass(frozen=True)
class ValidationFinding:
    """One evidence-bound visual, OCR, structural, or lightweight logic finding."""

    code: str
    severity: str
    detail: str
    block_ids: tuple[str, ...]
    evidence: str


@dataclass(frozen=True)
class ReconstructionChunk:
    """Structured response for a single deterministic page/block chunk."""

    page_numbers: tuple[int, ...]
    blocks: tuple[GroundedBlock, ...]
    findings: tuple[ValidationFinding, ...]


@dataclass(frozen=True)
class ReconstructionManifest:
    """Document-level Markdown assembly with complete provenance and quality findings."""

    schema_version: str
    source_sha256: str
    layout_manifest: ArtifactReference
    ocr_manifest: ArtifactReference
    model_id: str
    model_revision: str
    prompt_version: str
    markdown: str
    blocks: tuple[GroundedBlock, ...]
    findings: tuple[ValidationFinding, ...]


class QwenVLClient(Protocol):
    """Internal vLLM/OpenAI-compatible Qwen-VL inference boundary."""

    def reconstruct(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Run one local structured VLM inference for the supplied grounded context."""


class LocalOpenAIQwenVLClient:
    """OpenAI-compatible client restricted to the local/internal Qwen-VL service."""

    def __init__(self, *, endpoint: str, model_id: str, timeout_seconds: float) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {
            "qwen-vl",
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("Qwen-VL client endpoint must be a local/internal HTTP service")
        self._endpoint = endpoint.rstrip("/")
        self._model_id = model_id
        self._timeout = timeout_seconds

    def reconstruct(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Call only the internal vLLM chat endpoint and require JSON object response."""
        content: list[dict[str, Any]] = [
            {"type": "text", "text": _prompt(context)},
        ]
        for image in context["images"]:
            assert isinstance(image, Mapping)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image['base64']}"},
                }
            )
        payload = {
            "model": self._model_id,
            "temperature": 0,
            "top_p": 1,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a grounded document reconstruction engine. Return only valid JSON. "
                        "Never introduce facts without image/block evidence."
                    ),
                },
                {"role": "user", "content": content},
            ],
        }
        request = urllib.request.Request(
            f"{self._endpoint}/chat/completions",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ReconstructionError(f"local Qwen-VL request failed: {error}") from error
        try:
            content_value = payload["choices"][0]["message"]["content"]
            if not isinstance(content_value, str):
                raise TypeError("content is not a string")
            return _decode_json_object(content_value)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ReconstructionError("local Qwen-VL response has no valid JSON content") from error


class ReconstructionAssembler:
    """Build grounded contexts, validate each chunk, and deterministically assemble one document."""

    def __init__(
        self,
        *,
        client: QwenVLClient,
        artifacts: ArtifactStore,
        model_id: str,
        model_revision: str,
        prompt_version: str,
        max_blocks_per_request: int,
        max_images_per_request: int,
    ) -> None:
        self._client = client
        self._artifacts = artifacts
        self._model_id = model_id
        self._model_revision = model_revision
        self._prompt_version = prompt_version
        self._max_blocks = max_blocks_per_request
        self._max_images = max_images_per_request

    def reconstruct(
        self,
        *,
        layout: LayoutManifest,
        layout_reference: ArtifactReference,
        ocr: OcrManifest,
        ocr_reference: ArtifactReference,
    ) -> ReconstructionManifest:
        """Run page chunks as one logical reconstruction and join only validated block order."""
        if layout.source_sha256 != ocr.source_sha256:
            raise ReconstructionError("layout and OCR source SHA-256 differ")
        chunks = self._chunks(layout)
        assembled_blocks: list[GroundedBlock] = []
        findings: list[ValidationFinding] = []
        for page_numbers, expected_blocks in chunks:
            context = self._context(layout, ocr, page_numbers, expected_blocks)
            chunk = self._validate_chunk(
                self._client.reconstruct(context), page_numbers, expected_blocks, ocr
            )
            assembled_blocks.extend(chunk.blocks)
            findings.extend(chunk.findings)
        markdown = "\n\n".join(block.markdown for block in assembled_blocks if block.markdown.strip())
        return ReconstructionManifest(
            schema_version="reconstruction-manifest-v1",
            source_sha256=layout.source_sha256,
            layout_manifest=layout_reference,
            ocr_manifest=ocr_reference,
            model_id=self._model_id,
            model_revision=self._model_revision,
            prompt_version=self._prompt_version,
            markdown=markdown,
            blocks=tuple(assembled_blocks),
            findings=tuple(findings),
        )

    def _chunks(self, layout: LayoutManifest) -> tuple[tuple[tuple[int, ...], tuple[LayoutBlock, ...]], ...]:
        blocks_per_chunk = min(self._max_blocks, self._max_images - 1)
        if blocks_per_chunk <= 0:
            raise ReconstructionError("Qwen-VL image budget must allow one page image and one block crop")
        by_page: dict[int, list[LayoutBlock]] = {}
        for block in sorted(layout.blocks, key=lambda value: value.reading_order):
            by_page.setdefault(block.page_number, []).append(block)
        chunks: list[tuple[tuple[int, ...], tuple[LayoutBlock, ...]]] = []
        for page_number in sorted(by_page):
            page_blocks = by_page[page_number]
            for offset in range(0, len(page_blocks), blocks_per_chunk):
                chunk = tuple(page_blocks[offset : offset + blocks_per_chunk])
                chunks.append(((page_number,), chunk))
        return tuple(chunks)

    def _context(
        self,
        layout: LayoutManifest,
        ocr: OcrManifest,
        page_numbers: tuple[int, ...],
        blocks: tuple[LayoutBlock, ...],
    ) -> dict[str, Any]:
        layout_pages = {page.page_number: page for page in layout.pages}
        images: list[dict[str, str | int]] = []
        for page_number in page_numbers:
            page = layout_pages[page_number]
            images.append(self._image_payload(page.image, f"page:{page_number}"))
        for block in blocks:
            images.append(self._image_payload(block.crop, f"block:{block.block_id}"))
        block_ids = {block.block_id for block in blocks}
        return {
            "prompt_version": self._prompt_version,
            "page_numbers": page_numbers,
            "layout_blocks": [
                {
                    "block_id": block.block_id,
                    "page_number": block.page_number,
                    "kind": block.kind,
                    "bbox": block.bbox,
                    "reading_order": block.reading_order,
                    "parent_block_id": block.parent_block_id,
                    "relations": block.relations,
                    "attributes": block.attributes,
                    "image_ref": f"block:{block.block_id}",
                }
                for block in blocks
            ],
            "ocr_tokens": [_token_payload(token) for token in ocr.tokens if token.block_id in block_ids],
            "ocr_findings": [_ocr_finding_payload(finding) for finding in ocr.findings if finding.block_id in block_ids],
            "images": images,
        }

    def _image_payload(self, reference: ArtifactReference, label: str) -> dict[str, str | int]:
        with tempfile.TemporaryDirectory(prefix="idp-qwen-vl-") as temporary:
            image_path = Path(temporary) / "image.png"
            self._artifacts.get_file(reference, image_path)
            data = image_path.read_bytes()
        return {"label": label, "sha256": reference.sha256, "base64": base64.b64encode(data).decode("ascii")}

    def _validate_chunk(
        self,
        response: Mapping[str, Any],
        page_numbers: tuple[int, ...],
        expected_blocks: tuple[LayoutBlock, ...],
        ocr: OcrManifest,
    ) -> ReconstructionChunk:
        raw_blocks = response.get("blocks")
        raw_findings = response.get("findings", [])
        if not isinstance(raw_blocks, list) or not isinstance(raw_findings, list):
            raise ReconstructionError("Qwen-VL JSON requires blocks and findings arrays")
        expected_ids = [block.block_id for block in expected_blocks]
        if [entry.get("block_id") if isinstance(entry, Mapping) else None for entry in raw_blocks] != expected_ids:
            raise ReconstructionError("Qwen-VL response does not preserve complete MinerU block order")
        block_map = {block.block_id: block for block in expected_blocks}
        tokens = {token.token_id: token for token in ocr.tokens}
        blocks = tuple(self._grounded_block(entry, block_map, tokens) for entry in raw_blocks)
        findings = tuple(self._finding(entry, block_map) for entry in raw_findings)
        return ReconstructionChunk(page_numbers=page_numbers, blocks=blocks, findings=findings)

    @staticmethod
    def _grounded_block(
        value: Any,
        expected: Mapping[str, LayoutBlock],
        tokens: Mapping[str, OcrToken],
    ) -> GroundedBlock:
        if not isinstance(value, Mapping):
            raise ReconstructionError("Qwen-VL block response must be an object")
        block_id = value.get("block_id")
        block = expected.get(block_id) if isinstance(block_id, str) else None
        markdown = value.get("markdown")
        if block is None or not isinstance(markdown, str):
            raise ReconstructionError("Qwen-VL block lacks valid block_id or markdown")
        if value.get("page_number") != block.page_number or tuple(value.get("bbox", ())) != block.bbox:
            raise ReconstructionError(f"Qwen-VL block geometry mismatch: {block.block_id}")
        corrections: list[OcrCorrection] = []
        raw_corrections = value.get("corrections", [])
        if not isinstance(raw_corrections, list):
            raise ReconstructionError("Qwen-VL corrections must be an array")
        for correction in raw_corrections:
            if not isinstance(correction, Mapping):
                raise ReconstructionError("Qwen-VL correction must be an object")
            token_id = correction.get("token_id")
            token = tokens.get(token_id) if isinstance(token_id, str) else None
            original = correction.get("original_text")
            corrected = correction.get("corrected_text")
            evidence = correction.get("evidence")
            if (
                token is None
                or token.block_id != block.block_id
                or original != token.raw_text
                or not isinstance(corrected, str)
                or not isinstance(evidence, str)
                or not evidence.strip()
            ):
                raise ReconstructionError(f"invalid OCR correction for block {block.block_id}")
            corrections.append(OcrCorrection(token_id, original, corrected, evidence))
        return GroundedBlock(block.block_id, block.page_number, block.bbox, markdown, tuple(corrections))

    @staticmethod
    def _finding(value: Any, expected: Mapping[str, LayoutBlock]) -> ValidationFinding:
        if not isinstance(value, Mapping):
            raise ReconstructionError("Qwen-VL finding must be an object")
        code = value.get("code")
        severity = value.get("severity")
        detail = value.get("detail")
        evidence = value.get("evidence")
        block_ids = value.get("block_ids")
        if (
            not all(isinstance(item, str) and item in expected for item in block_ids)
            if isinstance(block_ids, list)
            else True
        ):
            raise ReconstructionError("Qwen-VL finding references unknown block IDs")
        if not all(isinstance(item, str) and item for item in (code, severity, detail, evidence)):
            raise ReconstructionError("Qwen-VL finding lacks code/severity/detail/evidence")
        return ValidationFinding(code, severity, detail, tuple(block_ids), evidence)


class QwenVLStageHandler:
    """Reserve GPU0, reconstruct one document, persist result, then release the stage lease."""

    def __init__(
        self,
        assembler: ReconstructionAssembler,
        artifacts: ArtifactStore,
        repository: SqlAlchemyBatchRepository,
        gpu0_slot_unit: str,
    ) -> None:
        self._assembler = assembler
        self._artifacts = artifacts
        self._repository = repository
        self._gpu0_slot_unit = gpu0_slot_unit

    def handle(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        layout: LayoutManifest,
        layout_reference: ArtifactReference,
        ocr: OcrManifest,
        ocr_reference: ArtifactReference,
        artifact_prefix: str,
    ) -> ReconstructionManifest:
        """Use exclusive GPU0 role capacity for the whole logical reconstruction run."""
        lease_duration = self._lease_duration()
        try:
            self._repository.renew_lease(
                job_id=job_id,
                worker_id=worker_id,
                lease_duration=lease_duration,
            )
            self._repository.reserve_resources(
                job_id=job_id,
                owner=worker_id,
                requests=(ResourceRequest(ReservationKind.GPU0, 1, self._gpu0_slot_unit),),
                lease_duration=lease_duration,
            )
        except ResourceCapacityError:
            self._repository.defer_job_for_capacity(
                job_id=job_id,
                worker_id=worker_id,
                error_detail="GPU0 Qwen-VL role slot is unavailable",
            )
            raise
        result = self._assembler.reconstruct(
            layout=layout,
            layout_reference=layout_reference,
            ocr=ocr,
            ocr_reference=ocr_reference,
        )
        markdown_artifact = self._artifacts.put_bytes(
            object_key=f"{artifact_prefix.rstrip('/')}/reconstructed.md",
            payload=result.markdown.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            retention=ArtifactRetention.TEMPORARY,
        )
        manifest_artifact = self._artifacts.put_bytes(
            object_key=f"{artifact_prefix.rstrip('/')}/reconstruction_manifest.json",
            payload=json.dumps(_manifest_payload(result), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            media_type="application/json",
            retention=ArtifactRetention.TEMPORARY,
        )
        self._repository.record_reconstruction_output(
            job_id=job_id,
            worker_id=worker_id,
            markdown=markdown_artifact,
            manifest=manifest_artifact,
        )
        self._repository.complete_job(job_id=job_id, worker_id=worker_id, state=JobState.SUCCEEDED)
        return result

    @staticmethod
    def _lease_duration():
        from datetime import timedelta

        return timedelta(minutes=20)


def _prompt(context: Mapping[str, Any]) -> str:
    """Versioned strict prompt; all visual context travels in the user payload, not hidden state."""
    return (
        "Reconstruct this document chunk into grounded Markdown. Return JSON exactly with "
        "`blocks` and `findings`. Return every provided layout block exactly once, in the input "
        "reading order. Each block must contain block_id, page_number, bbox, markdown, corrections. "
        "Use page and block images to validate OCR; correct an OCR token only with visual evidence. "
        "Interpret tables, images, charts, diagrams, formulas, stamps, signatures, headers, footers, "
        "and footnotes when meaningful. Findings must cover OCR disagreement, unreadable regions, "
        "missing/contradictory content, and obvious sums/dates/numbering/reference inconsistencies. "
        "Every finding must cite one or more supplied block IDs and an evidence string. "
        "GROUND_TRUTH_CONTEXT="
        f"{json.dumps(_prompt_context(context), ensure_ascii=False, separators=(',', ':'))}"
    )


def _prompt_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Keep binary image payloads out of the text prompt; they are sent once as image parts."""
    return {
        **{key: value for key, value in context.items() if key != "images"},
        "images": [
            {"label": image["label"], "sha256": image["sha256"]}
            for image in context["images"]
            if isinstance(image, Mapping)
        ],
    }


def _decode_json_object(value: str) -> Mapping[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    decoded = json.loads(text)
    if not isinstance(decoded, Mapping):
        raise ReconstructionError("Qwen-VL JSON response root must be an object")
    return decoded


def _token_payload(token: OcrToken) -> dict[str, Any]:
    return {
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
    }


def _ocr_finding_payload(finding: OcrFinding) -> dict[str, Any]:
    return {
        "block_id": finding.block_id,
        "page_number": finding.page_number,
        "bbox": finding.bbox,
        "code": finding.code,
        "detail": finding.detail,
    }


def _manifest_payload(manifest: ReconstructionManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "source_sha256": manifest.source_sha256,
        "layout_manifest": manifest.layout_manifest.model_dump(mode="json"),
        "ocr_manifest": manifest.ocr_manifest.model_dump(mode="json"),
        "model_id": manifest.model_id,
        "model_revision": manifest.model_revision,
        "prompt_version": manifest.prompt_version,
        "blocks": [
            {
                "block_id": block.block_id,
                "page_number": block.page_number,
                "bbox": block.bbox,
                "markdown": block.markdown,
                "corrections": [asdict(correction) for correction in block.corrections],
            }
            for block in manifest.blocks
        ],
        "findings": [asdict(finding) for finding in manifest.findings],
    }
