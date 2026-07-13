"""Vision-only PDF render, upscale selection, and coordinate provenance."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from math import log2
from pathlib import Path
from typing import Protocol
from uuid import UUID

from PIL import Image, ImageStat

from idp.domain.models import ArtifactReference, StoredArtifact
from idp.domain.states import ArtifactRetention
from idp.ports.artifact_store import ArtifactStore
from idp.persistence.repository import SqlAlchemyBatchRepository


class VisionPreparationError(RuntimeError):
    """Raised for corrupt PDFs, unsupported rendering, or configured resource limits."""


@dataclass(frozen=True)
class RenderLimits:
    """Hard limits applied before pixel buffers are allocated."""

    dpi: int
    max_pages: int
    max_pixels_per_page: int
    max_total_pixels: int


@dataclass(frozen=True)
class PageTransform:
    """Explicit mapping between PDF point coordinates and page-image pixels."""

    page_number: int
    pdf_width_points: float
    pdf_height_points: float
    render_width_pixels: int
    render_height_pixels: int
    dpi: int
    image_scale_x: float
    image_scale_y: float

    def pdf_to_image(self, x: float, y: float) -> tuple[float, float]:
        """Map PDF page points to the rendered image coordinate system."""
        return x * self.image_scale_x, y * self.image_scale_y

    def image_to_pdf(self, x: float, y: float) -> tuple[float, float]:
        """Map image pixels back to original PDF point coordinates."""
        return x / self.image_scale_x, y / self.image_scale_y


@dataclass(frozen=True)
class RenderedPage:
    """One immutable RGB render and its coordinate mapping."""

    transform: PageTransform
    png: bytes


@dataclass(frozen=True)
class ImageSignals:
    """Image-only signals used to decide whether generated upscale is safe to use."""

    entropy: float
    clipping_fraction: float
    luminance_stddev: float
    width: int
    height: int


@dataclass(frozen=True)
class UpscaleDecision:
    """A reproducible selection of original or enhanced page image."""

    selected: str
    reason: str
    original: ImageSignals
    enhanced: ImageSignals


@dataclass(frozen=True)
class PreparedPage:
    """Artifacts and transform selected for downstream layout/OCR stages."""

    page_number: int
    render: StoredArtifact
    enhanced: StoredArtifact | None
    selected: StoredArtifact
    render_transform: PageTransform
    selected_transform: PageTransform
    decision: UpscaleDecision


@dataclass(frozen=True)
class VisionManifest:
    """Versioned manifest consumed by later layout and OCR stages."""

    source_sha256: str
    dpi: int
    pages: tuple[PreparedPage, ...]


class Upscaler(Protocol):
    """Pinned local SwinIR adapter contract; implementations must never use network I/O."""

    def upscale(self, image_png: bytes) -> bytes:
        """Return one locally generated enhanced PNG for the supplied render."""


class CommandSwinIRUpscaler:
    """Pinned local SwinIR wrapper using files, never a remote inference endpoint."""

    def __init__(self, command: tuple[str, ...], working_directory: Path | None = None) -> None:
        if not command:
            raise ValueError("SwinIR command must not be empty")
        self._command = command
        self._working_directory = working_directory

    def upscale(self, image_png: bytes) -> bytes:
        """Run the configured local command with input/output placeholders exactly once."""
        with tempfile.TemporaryDirectory(prefix="idp-swinir-") as temporary:
            root = Path(temporary)
            source = root / "input.png"
            target = root / "output.png"
            source.write_bytes(image_png)
            command = [
                argument.format(input=str(source), output=str(target)) for argument in self._command
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
                raise VisionPreparationError(f"local SwinIR command failed: {error}") from error
            if not target.is_file():
                raise VisionPreparationError("local SwinIR command did not create output image")
            return target.read_bytes()


class PyMuPdfRenderer:
    """Deterministic RGB renderer that intentionally never asks MuPDF for text content."""

    def render(self, pdf_path: Path, limits: RenderLimits) -> tuple[RenderedPage, ...]:
        """Render each PDF page directly into RGB PNG bytes with resource limits."""
        try:
            import fitz
        except ImportError as error:
            raise VisionPreparationError("PyMuPDF is required for PDF rendering") from error
        try:
            document = fitz.open(pdf_path)
        except Exception as error:
            raise VisionPreparationError(f"cannot open PDF for image rendering: {error}") from error
        try:
            if document.page_count == 0:
                raise VisionPreparationError("pdf_has_no_pages")
            if document.page_count > limits.max_pages:
                raise VisionPreparationError("pdf_page_limit_exceeded")
            scale = limits.dpi / 72.0
            total_pixels = 0
            pages: list[RenderedPage] = []
            for index in range(document.page_count):
                page = document.load_page(index)
                rectangle = page.rect
                width = round(rectangle.width * scale)
                height = round(rectangle.height * scale)
                pixels = width * height
                if width <= 0 or height <= 0 or pixels > limits.max_pixels_per_page:
                    raise VisionPreparationError(f"pdf_page_pixel_limit_exceeded:{index + 1}")
                total_pixels += pixels
                if total_pixels > limits.max_total_pixels:
                    raise VisionPreparationError("pdf_total_pixel_limit_exceeded")
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(scale, scale),
                    colorspace=fitz.csRGB,
                    alpha=False,
                    annots=True,
                )
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                payload = _encode_png(image)
                pages.append(
                    RenderedPage(
                        transform=PageTransform(
                            page_number=index + 1,
                            pdf_width_points=rectangle.width,
                            pdf_height_points=rectangle.height,
                            render_width_pixels=pixmap.width,
                            render_height_pixels=pixmap.height,
                            dpi=limits.dpi,
                            image_scale_x=pixmap.width / rectangle.width,
                            image_scale_y=pixmap.height / rectangle.height,
                        ),
                        png=payload,
                    )
                )
            return tuple(pages)
        finally:
            document.close()


class ImageQualityGate:
    """Conservative image-only fallback policy for generated upscale output."""

    def __init__(self, entropy_tolerance: float, clipping_tolerance: float) -> None:
        self._entropy_tolerance = entropy_tolerance
        self._clipping_tolerance = clipping_tolerance

    def select(self, original_png: bytes, enhanced_png: bytes) -> UpscaleDecision:
        """Choose enhanced image only if its pixel signals do not indicate degradation."""
        original = image_signals(original_png)
        enhanced = image_signals(enhanced_png)
        if enhanced.width < original.width or enhanced.height < original.height:
            return UpscaleDecision("render", "upscale_dimensions_smaller", original, enhanced)
        if enhanced.entropy + self._entropy_tolerance < original.entropy:
            return UpscaleDecision("render", "upscale_entropy_degraded", original, enhanced)
        if enhanced.clipping_fraction > original.clipping_fraction + self._clipping_tolerance:
            return UpscaleDecision("render", "upscale_clipping_increased", original, enhanced)
        return UpscaleDecision("enhanced", "upscale_accepted", original, enhanced)


class VisionPreparation:
    """Render source PDF, optionally upscale each page, and write provenance artifacts."""

    def __init__(
        self,
        renderer: PyMuPdfRenderer,
        artifacts: ArtifactStore,
        quality_gate: ImageQualityGate,
        upscaler: Upscaler | None = None,
    ) -> None:
        self._renderer = renderer
        self._artifacts = artifacts
        self._quality_gate = quality_gate
        self._upscaler = upscaler

    def prepare(
        self,
        *,
        pdf_path: Path,
        source_sha256: str,
        artifact_prefix: str,
        limits: RenderLimits,
    ) -> tuple[VisionManifest, StoredArtifact]:
        """Write page artifacts and an immutable manifest without reading PDF text layers."""
        pages: list[PreparedPage] = []
        selected_total_pixels = 0
        for page in self._renderer.render(pdf_path, limits):
            base_key = f"{artifact_prefix.rstrip('/')}/pages/{page.transform.page_number:05d}"
            render = self._artifacts.put_bytes(
                object_key=f"{base_key}/render.png",
                payload=page.png,
                media_type="image/png",
                retention=ArtifactRetention.TEMPORARY,
            )
            enhanced: StoredArtifact | None = None
            decision = UpscaleDecision(
                "render",
                "upscale_not_configured",
                image_signals(page.png),
                image_signals(page.png),
            )
            selected = render
            selected_transform = page.transform
            if self._upscaler is not None:
                enhanced_png = self._upscaler.upscale(page.png)
                enhanced_signals = image_signals(enhanced_png)
                original_signals = image_signals(page.png)
                if enhanced_signals.width * enhanced_signals.height > limits.max_pixels_per_page:
                    decision = UpscaleDecision(
                        "render", "upscale_pixel_limit_exceeded", original_signals, enhanced_signals
                    )
                elif selected_total_pixels + enhanced_signals.width * enhanced_signals.height > limits.max_total_pixels:
                    decision = UpscaleDecision(
                        "render", "upscale_total_pixel_limit_exceeded", original_signals, enhanced_signals
                    )
                else:
                    enhanced = self._artifacts.put_bytes(
                        object_key=f"{base_key}/upscaled.png",
                        payload=enhanced_png,
                        media_type="image/png",
                        retention=ArtifactRetention.TEMPORARY,
                    )
                    decision = self._quality_gate.select(page.png, enhanced_png)
                if decision.selected == "enhanced":
                    assert enhanced is not None
                    selected = enhanced
                    selected_transform = _transform_for_selected_image(
                        page.transform, enhanced_png
                    )
            selected_total_pixels += selected_transform.render_width_pixels * selected_transform.render_height_pixels
            pages.append(
                PreparedPage(
                    page_number=page.transform.page_number,
                    render=render,
                    enhanced=enhanced,
                    selected=selected,
                    render_transform=page.transform,
                    selected_transform=selected_transform,
                    decision=decision,
                )
            )
        manifest = VisionManifest(source_sha256=source_sha256, dpi=limits.dpi, pages=tuple(pages))
        manifest_artifact = self._artifacts.put_bytes(
            object_key=f"{artifact_prefix.rstrip('/')}/render_manifest.json",
            payload=json.dumps(_manifest_payload(manifest), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            media_type="application/json",
            retention=ArtifactRetention.TEMPORARY,
        )
        return manifest, manifest_artifact

    def prepare_source_artifact(
        self,
        *,
        source: ArtifactReference,
        source_sha256: str,
        artifact_prefix: str,
        limits: RenderLimits,
    ) -> tuple[VisionManifest, StoredArtifact]:
        """Render only a verified immutable source object, never its original filesystem path."""
        if source.sha256 != source_sha256:
            raise VisionPreparationError("source artifact hash does not match submitted source SHA-256")
        with tempfile.TemporaryDirectory(prefix="idp-render-") as temporary:
            local_pdf = Path(temporary) / "source.pdf"
            self._artifacts.get_file(source, local_pdf)
            return self.prepare(
                pdf_path=local_pdf,
                source_sha256=source_sha256,
                artifact_prefix=artifact_prefix,
                limits=limits,
            )


class VisionStageHandler:
    """Handler for immutable source jobs before the layout stage is introduced."""

    def __init__(
        self,
        preparation: VisionPreparation,
        repository: SqlAlchemyBatchRepository,
        limits: RenderLimits,
    ) -> None:
        self._preparation = preparation
        self._repository = repository
        self._limits = limits

    def handle(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        source_object_key: str,
        source_sha256: str,
        artifact_prefix: str,
    ) -> VisionManifest:
        """Prepare immutable page images and safely close the owned source stage."""
        source = ArtifactReference(
            object_key=source_object_key,
            sha256=source_sha256,
            media_type="application/pdf",
        )
        manifest, manifest_artifact = self._preparation.prepare_source_artifact(
            source=source,
            source_sha256=source_sha256,
            artifact_prefix=artifact_prefix,
            limits=self._limits,
        )
        artifacts: list[StoredArtifact] = []
        for page in manifest.pages:
            artifacts.append(page.render)
            if page.enhanced is not None:
                artifacts.append(page.enhanced)
        self._repository.record_vision_output(
            job_id=job_id,
            worker_id=worker_id,
            manifest=manifest_artifact,
            artifacts=tuple(artifacts),
        )
        from idp.domain.states import JobState

        self._repository.complete_job(job_id=job_id, worker_id=worker_id, state=JobState.SUCCEEDED)
        return manifest


def image_signals(png: bytes) -> ImageSignals:
    """Calculate deterministic pixel-only quality signals without OCR or text extraction."""
    with Image.open(io.BytesIO(png)) as image:
        grayscale = image.convert("L")
        histogram = grayscale.histogram()
        total = sum(histogram)
        entropy = -sum((count / total) * log2(count / total) for count in histogram if count)
        clipping = (histogram[0] + histogram[255]) / total
        stat = ImageStat.Stat(grayscale)
        return ImageSignals(
            entropy=entropy,
            clipping_fraction=clipping,
            luminance_stddev=stat.stddev[0],
            width=image.width,
            height=image.height,
        )


def _encode_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=6)
    return output.getvalue()


def _manifest_payload(manifest: VisionManifest) -> dict[str, object]:
    return {
        "source_sha256": manifest.source_sha256,
        "dpi": manifest.dpi,
        "pages": [
            {
                "page_number": page.page_number,
                "render": page.render.reference.model_dump(mode="json"),
                "enhanced": None
                if page.enhanced is None
                else page.enhanced.reference.model_dump(mode="json"),
                "selected": page.selected.reference.model_dump(mode="json"),
                "render_transform": asdict(page.render_transform),
                "selected_transform": asdict(page.selected_transform),
                "decision": {
                    "selected": page.decision.selected,
                    "reason": page.decision.reason,
                    "original": asdict(page.decision.original),
                    "enhanced": asdict(page.decision.enhanced),
                },
            }
            for page in manifest.pages
        ],
    }


def _transform_for_selected_image(render_transform: PageTransform, png: bytes) -> PageTransform:
    """Produce an explicit PDF-point mapping for a selected upscaled page image."""
    with Image.open(io.BytesIO(png)) as image:
        return PageTransform(
            page_number=render_transform.page_number,
            pdf_width_points=render_transform.pdf_width_points,
            pdf_height_points=render_transform.pdf_height_points,
            render_width_pixels=image.width,
            render_height_pixels=image.height,
            dpi=render_transform.dpi,
            image_scale_x=image.width / render_transform.pdf_width_points,
            image_scale_y=image.height / render_transform.pdf_height_points,
        )
