"""Render PDF pages to PNG images without reading the text layer."""

from __future__ import annotations

import concurrent.futures
import logging
import tempfile
import threading
from pathlib import Path

import fitz
from PIL import Image, ImageEnhance

from idp.config import settings

logger = logging.getLogger(__name__)

_local = threading.local()


def _open_document(pdf_path: Path):
    """One PyMuPDF document per worker thread (the API is not thread-safe)."""
    doc = getattr(_local, "doc", None)
    if doc is None or getattr(_local, "doc_path", None) != str(pdf_path):
        if doc is not None:
            doc.close()
        doc = fitz.open(pdf_path)
        _local.doc = doc
        _local.doc_path = str(pdf_path)
    return doc


def _close_thread_document() -> None:
    doc = getattr(_local, "doc", None)
    if doc is not None:
        doc.close()
    _local.doc = None
    _local.doc_path = None


def _render_single_page(
    page_index: int,
    pdf_path: Path,
    output_dir: Path,
    dpi: int,
    upscale_factor: int,
    max_image_dimension: int,
) -> tuple[Path, int]:
    doc = _open_document(pdf_path)
    page = doc.load_page(page_index)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    logger.debug("Renderer: page %d raw size: %dx%d", page_index + 1, pix.width, pix.height)
    img = _upscale_image(img, scale=upscale_factor)
    img = _resize_to_max_dimension(img, max_image_dimension)
    logger.debug("Renderer: page %d final size: %dx%d", page_index + 1, img.width, img.height)
    out_path = output_dir / f"page_{page_index + 1:05d}.png"
    img.save(out_path, "PNG")
    return out_path, page_index


def _render_all_pages(pdf_path: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        doc = fitz.open(pdf_path)
        num_pages = len(doc)
        doc.close()
        futures = [
            executor.submit(
                _render_single_page,
                i, pdf_path, output_dir,
                settings.render_dpi,
                settings.upscale_factor,
                settings.max_image_dimension,
            )
            for i in range(num_pages)
        ]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    results.sort(key=lambda x: x[1])
    return [path for path, _ in results]


def render_pdf_to_pngs(pdf_path: Path, output_dir: Path) -> list[Path]:
    pngs = _render_all_pages(pdf_path, output_dir)
    logger.debug("Renderer: produced %d images", len(pngs))
    return pngs


def _upscale_image(img: Image.Image, scale: int = 2) -> Image.Image:
    if scale <= 1:
        return img
    new_size = (img.width * scale, img.height * scale)
    upscaled = img.resize(new_size, Image.Resampling.LANCZOS)
    enhancer = ImageEnhance.Sharpness(upscaled)
    upscaled = enhancer.enhance(1.15)
    enhancer = ImageEnhance.Contrast(upscaled)
    upscaled = enhancer.enhance(1.05)
    return upscaled


def _resize_to_max_dimension(img: Image.Image, max_dim: int) -> Image.Image:
    current_max = max(img.width, img.height)
    if current_max <= max_dim:
        return img
    scale = max_dim / current_max
    new_size = (int(img.width * scale), int(img.height * scale))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def extract_pdf_text_and_visual_pages(
    pdf_path: Path,
    output_dir: Path | None = None,
) -> tuple[str, list[Path], list[int]]:
    """Render the PDF to page PNGs. The text layer is intentionally never read.

    When ``output_dir`` is given, renders live there (the pipeline's
    ``rendered/`` area); otherwise a per-stem temp directory is used and the
    caller is responsible for cleanup.
    """
    doc = fitz.open(pdf_path)
    num_pages = len(doc)
    doc.close()

    if output_dir is not None:
        target_dir = output_dir
    else:
        target_dir = Path(tempfile.gettempdir()) / "idp_pdf_pages" / pdf_path.stem

    pngs = _render_all_pages(pdf_path, target_dir)

    visual_page_indices = list(range(num_pages))
    logger.debug("Renderer: produced %d images", len(pngs))
    return "", pngs, visual_page_indices
