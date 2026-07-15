"""Offline MinerU layout adapter that preserves structure without trusting vendor text."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from PIL import Image

from idp.domain.models import ArtifactReference, StoredArtifact
from idp.domain.states import ArtifactRetention, JobState
from idp.persistence.repository import SqlAlchemyBatchRepository
from idp.ports.artifact_store import ArtifactStore
from idp.services.vision import PageTransform, PreparedPage, VisionManifest


class MinerUError(RuntimeError):
    """Raised when local MinerU output is missing or violates the layout contract."""


@dataclass(frozen=True)
class LayoutBlock:
    """One lossless structural block; text content deliberately remains only in raw output."""

    block_id: str
    page_number: int
    kind: str
    bbox: tuple[float, float, float, float]
    reading_order: int
    parent_block_id: str | None
    relations: tuple[str, ...]
    crop: ArtifactReference
    vendor_path: str
    attributes: dict[str, str | float | int | bool | None]


@dataclass(frozen=True)
class LayoutPage:
    """Selected image and transform used by MinerU for one document page."""

    page_number: int
    image: ArtifactReference
    transform: PageTransform


@dataclass(frozen=True)
class LayoutManifest:
    """Internal versioned layout contract consumed by OCR and Qwen-VL stages."""

    schema_version: str
    source_sha256: str
    raw_mineru: ArtifactReference
    pages: tuple[LayoutPage, ...]
    blocks: tuple[LayoutBlock, ...]


class MinerURunner(Protocol):
    """Local MinerU adapter; output must be a `middle.json` file produced offline."""

    def run(self, page_images: Mapping[int, Path], output_directory: Path) -> Path:
        """Parse selected page images and return the local `middle.json` path."""


class CommandMinerURunner:
    """Run a pinned local MinerU command with explicit image/output directory placeholders."""

    def __init__(self, command: tuple[str, ...], working_directory: Path | None = None) -> None:
        if not command:
            raise ValueError("MinerU command must not be empty")
        self._command = command
        self._working_directory = working_directory

    def run(self, page_images: Mapping[int, Path], output_directory: Path) -> Path:
        """Invoke only a local process and require it to create `middle.json`."""
        images_directory = output_directory / "pages"
        images_directory.mkdir(parents=True, exist_ok=True)
        for page_number, image_path in page_images.items():
            target = images_directory / f"{page_number:05d}.png"
            target.write_bytes(image_path.read_bytes())
        command = [
            argument.format(images=str(images_directory), output=str(output_directory))
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
                timeout=1800,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise MinerUError(f"local MinerU command failed: {error}") from error
        output = output_directory / "middle.json"
        if not output.is_file():
            raise MinerUError("local MinerU command did not create middle.json")
        return output


class LayoutAdapter:
    """Normalize flexible MinerU `middle.json` schema without letting vendor text enter content path."""

    _PAGE_KEYS = ("pdf_info", "pages", "page_info")
    _CHILD_KEYS = ("para_blocks", "blocks", "layout_blocks", "layout_dets", "children", "lines")
    _BBOX_KEYS = ("bbox", "box", "rect", "coordinates")
    _TYPE_KEYS = ("type", "block_type", "category", "label")
    _TEXT_KEYS = frozenset(
        {
            "text",
            "content",
            "markdown",
            "md",
            "spans",
            "lines",
            "words",
            "ocr",
            "latex",
            "html",
        }
    )
    _ATTRIBUTE_KEYS = frozenset(
        {
            "score",
            "confidence",
            "rotation",
            "index",
            "level",
            "is_header",
            "is_footer",
            "is_vertical",
        }
    )

    def normalize(
        self,
        *,
        middle_json: Mapping[str, Any],
        vision: VisionManifest,
        raw_reference: ArtifactReference,
        artifacts: ArtifactStore,
        artifact_prefix: str,
    ) -> tuple[LayoutManifest, tuple[StoredArtifact, ...]]:
        """Create a content-free structural manifest and crops for every bbox block."""
        vision_pages = {page.page_number: page for page in vision.pages}
        pending_blocks: list[
            tuple[
                int,
                str,
                str,
                tuple[float, float, float, float],
                int,
                str,
                str | None,
                tuple[str, ...],
                StoredArtifact,
                dict[str, str | float | int | bool | None],
            ]
        ] = []
        crops: list[StoredArtifact] = []
        reading_order = 0
        for page_number, page_payload, page_path in self._pages(middle_json):
            prepared = vision_pages.get(page_number)
            if prepared is None:
                raise MinerUError(f"MinerU output references unavailable page {page_number}")
            for payload, path, parent_path in self._walk_blocks(page_payload, page_path, None):
                bbox = self._bbox(payload)
                if bbox is None:
                    continue
                kind = self._kind(payload)
                block_id = f"p{page_number}-{reading_order:06d}"
                crop = self._write_crop(
                    prepared,
                    bbox,
                    artifacts,
                    f"{artifact_prefix.rstrip('/')}/blocks/{block_id}/crop.png",
                )
                crops.append(crop)
                pending_blocks.append(
                    (
                        page_number,
                        block_id,
                        kind,
                        bbox,
                        reading_order,
                        path,
                        parent_path,
                        self._relations(payload),
                        crop,
                        self._attributes(payload),
                    )
                )
                reading_order += 1
        if not pending_blocks:
            raise MinerUError("MinerU middle.json contains no bbox layout blocks")
        vendor_paths = [block[5] for block in pending_blocks]
        path_to_id = {
            block_path: block_id
            for block_path, (_, block_id, _, _, _, _, _, _, _, _) in zip(vendor_paths, pending_blocks)
        }
        blocks = tuple(
            LayoutBlock(
                block_id=block_id,
                page_number=page_number,
                kind=kind,
                bbox=bbox,
                reading_order=order,
                parent_block_id=path_to_id.get(parent_path),
                relations=relations,
                crop=crop.reference,
                vendor_path=vendor_path,
                attributes=attributes,
            )
            for vendor_path, (
                page_number,
                block_id,
                kind,
                bbox,
                order,
                _block_path,
                parent_path,
                relations,
                crop,
                attributes,
            ) in pending_blocks
        )
        manifest = LayoutManifest(
            schema_version="layout-manifest-v1",
            source_sha256=vision.source_sha256,
            raw_mineru=raw_reference,
            pages=tuple(
                LayoutPage(
                    page_number=page.page_number,
                    image=page.selected.reference,
                    transform=page.selected_transform,
                )
                for page in vision.pages
            ),
            blocks=blocks,
        )
        return manifest, tuple(crops)

    def _pages(self, payload: Mapping[str, Any]) -> tuple[tuple[int, Mapping[str, Any], str], ...]:
        for key in self._PAGE_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                pages: list[tuple[int, Mapping[str, Any], str]] = []
                for index, page in enumerate(value):
                    if isinstance(page, Mapping):
                        pages.append((self._page_number(page, index), page, f"$.{key}[{index}]"))
                if pages:
                    return tuple(pages)
        if isinstance(payload.get("page"), Mapping):
            page = payload["page"]
            assert isinstance(page, Mapping)
            return ((self._page_number(page, 0), page, "$.page"),)
        raise MinerUError("MinerU middle.json has no supported page list")

    @staticmethod
    def _page_number(page: Mapping[str, Any], index: int) -> int:
        for key in ("page_number", "page_no", "page_id", "page_idx", "index"):
            value = page.get(key)
            if isinstance(value, int):
                return value + 1 if key in {"page_id", "page_idx", "index"} else value
        return index + 1

    def _walk_blocks(
        self,
        payload: Mapping[str, Any],
        path: str,
        parent: str | None,
    ) -> tuple[tuple[Mapping[str, Any], str, str | None], ...]:
        results: list[tuple[Mapping[str, Any], str, str | None]] = []
        parent_id = parent
        if self._bbox(payload) is not None:
            results.append((payload, path, parent))
            parent_id = path
        for key in self._CHILD_KEYS:
            children = payload.get(key)
            if not isinstance(children, list):
                continue
            for index, child in enumerate(children):
                if isinstance(child, Mapping):
                    results.extend(self._walk_blocks(child, f"{path}.{key}[{index}]", parent_id))
        return tuple(results)

    def _bbox(self, payload: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
        for key in self._BBOX_KEYS:
            value = payload.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 4:
                try:
                    x0, y0, x1, y1 = (float(coordinate) for coordinate in value)
                except (TypeError, ValueError):
                    continue
                if x1 > x0 and y1 > y0 and x0 >= 0 and y0 >= 0:
                    return x0, y0, x1, y1
        return None

    def _kind(self, payload: Mapping[str, Any]) -> str:
        for key in self._TYPE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value.lower()
        return "unknown"

    @staticmethod
    def _relations(payload: Mapping[str, Any]) -> tuple[str, ...]:
        values = payload.get("relations", payload.get("relation", ()))
        if isinstance(values, list):
            return tuple(str(value) for value in values if isinstance(value, (str, int)))
        if isinstance(values, (str, int)):
            return (str(values),)
        return ()

    def _attributes(self, payload: Mapping[str, Any]) -> dict[str, str | float | int | bool | None]:
        result: dict[str, str | float | int | bool | None] = {}
        for key in self._ATTRIBUTE_KEYS:
            value = payload.get(key)
            if value is None or isinstance(value, (str, float, int, bool)):
                if key in payload:
                    result[key] = value
        return result

    @staticmethod
    def _write_crop(
        page: PreparedPage,
        bbox: tuple[float, float, float, float],
        artifacts: ArtifactStore,
        object_key: str,
    ) -> StoredArtifact:
        with tempfile.TemporaryDirectory(prefix="idp-layout-crop-") as temporary:
            image_path = Path(temporary) / "page.png"
            artifacts.get_file(page.selected.reference, image_path)
            with Image.open(image_path) as image:
                x0, y0, x1, y1 = bbox
                left = max(0, min(image.width, round(x0)))
                top = max(0, min(image.height, round(y0)))
                right = max(left + 1, min(image.width, round(x1)))
                bottom = max(top + 1, min(image.height, round(y1)))
                if right <= left or bottom <= top:
                    raise MinerUError(f"MinerU bbox outside selected page image: {bbox}")
                output = io.BytesIO()
                image.crop((left, top, right, bottom)).save(output, format="PNG", optimize=False)
        return artifacts.put_bytes(
            object_key=object_key,
            payload=output.getvalue(),
            media_type="image/png",
            retention=ArtifactRetention.TEMPORARY,
        )


class MinerUStageHandler:
    """Prepare selected page inputs, persist raw `middle.json`, and emit normalized layout."""

    def __init__(
        self,
        runner: MinerURunner,
        adapter: LayoutAdapter,
        artifacts: ArtifactStore,
        repository: SqlAlchemyBatchRepository,
    ) -> None:
        self._runner = runner
        self._adapter = adapter
        self._artifacts = artifacts
        self._repository = repository

    def handle(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        vision: VisionManifest,
        artifact_prefix: str,
    ) -> LayoutManifest:
        """Run only local MinerU and persist raw + normalized layout before job completion."""
        with tempfile.TemporaryDirectory(prefix="idp-mineru-") as temporary:
            root = Path(temporary)
            pages: dict[int, Path] = {}
            for page in vision.pages:
                target = root / f"{page.page_number:05d}.png"
                self._artifacts.get_file(page.selected.reference, target)
                pages[page.page_number] = target
            raw_path = self._runner.run(pages, root / "output")
            raw_bytes = raw_path.read_bytes()
        try:
            raw_payload = json.loads(raw_bytes)
        except json.JSONDecodeError as error:
            raise MinerUError(f"MinerU middle.json is invalid JSON: {error}") from error
        if not isinstance(raw_payload, Mapping):
            raise MinerUError("MinerU middle.json root must be an object")
        raw_artifact = self._artifacts.put_bytes(
            object_key=f"{artifact_prefix.rstrip('/')}/mineru/middle.json",
            payload=raw_bytes,
            media_type="application/json",
            retention=ArtifactRetention.TEMPORARY,
        )
        layout, crops = self._adapter.normalize(
            middle_json=raw_payload,
            vision=vision,
            raw_reference=raw_artifact.reference,
            artifacts=self._artifacts,
            artifact_prefix=artifact_prefix,
        )
        layout_artifact = self._artifacts.put_bytes(
            object_key=f"{artifact_prefix.rstrip('/')}/layout_manifest.json",
            payload=json.dumps(_layout_payload(layout), sort_keys=True, separators=(",", ":")).encode("utf-8"),
            media_type="application/json",
            retention=ArtifactRetention.TEMPORARY,
        )
        self._repository.record_layout_output(
            job_id=job_id,
            worker_id=worker_id,
            raw_mineru=raw_artifact,
            manifest=layout_artifact,
            crops=crops,
        )
        self._repository.complete_job(job_id=job_id, worker_id=worker_id, state=JobState.SUCCEEDED)
        return layout


def _layout_payload(manifest: LayoutManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "source_sha256": manifest.source_sha256,
        "raw_mineru": manifest.raw_mineru.model_dump(mode="json"),
        "pages": [
            {
                "page_number": page.page_number,
                "image": page.image.model_dump(mode="json"),
                "transform": asdict(page.transform),
            }
            for page in manifest.pages
        ],
        "blocks": [
            {
                "block_id": block.block_id,
                "page_number": block.page_number,
                "kind": block.kind,
                "bbox": block.bbox,
                "reading_order": block.reading_order,
                "parent_block_id": block.parent_block_id,
                "relations": block.relations,
                "crop": block.crop.model_dump(mode="json"),
                "vendor_path": block.vendor_path,
                "attributes": block.attributes,
            }
            for block in manifest.blocks
        ],
    }


def layout_manifest_from_payload(payload: Mapping[str, Any]) -> LayoutManifest:
    """Load a normalized layout manifest produced by the local MinerU adapter."""
    try:
        pages = tuple(
            LayoutPage(
                page_number=int(value["page_number"]),
                image=ArtifactReference.model_validate(value["image"]),
                transform=PageTransform(**value["transform"]),
            )
            for value in payload["pages"]
        )
        blocks = tuple(
            LayoutBlock(
                block_id=str(value["block_id"]),
                page_number=int(value["page_number"]),
                kind=str(value["kind"]),
                bbox=tuple(float(coordinate) for coordinate in value["bbox"]),
                reading_order=int(value["reading_order"]),
                parent_block_id=(
                    None if value.get("parent_block_id") is None else str(value["parent_block_id"])
                ),
                relations=tuple(str(relation) for relation in value.get("relations", [])),
                crop=ArtifactReference.model_validate(value["crop"]),
                vendor_path=str(value["vendor_path"]),
                attributes=dict(value.get("attributes", {})),
            )
            for value in payload["blocks"]
        )
        return LayoutManifest(
            schema_version=str(payload["schema_version"]),
            source_sha256=str(payload["source_sha256"]),
            raw_mineru=ArtifactReference.model_validate(payload["raw_mineru"]),
            pages=pages,
            blocks=blocks,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MinerUError(f"persisted layout manifest is invalid: {error}") from error
