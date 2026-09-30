"""Render PDF pages to PNG images without reading the text layer."""

from __future__ import annotations

import concurrent.futures
import sys
import tempfile
from pathlib import Path

import fitz
from PIL import Image, ImageEnhance

from idp.config import settings


def _render_single_page(
    page_index: int,
    pdf_path: Path,
    output_dir: Path,
    dpi: int,
    upscale_factor: int,
    max_image_dimension: int,
) -> tuple[Path, int]:
    doc = fitz.open(pdf_path)
    page = doc.load_page(page_index)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    print(f"Renderer: page {page_index + 1} raw size: {pix.width}x{pix.height}", file=sys.stderr)
    img = _upscale_image(img, scale=upscale_factor)
    img = _resize_to_max_dimension(img, max_image_dimension)
    print(f"Renderer: page {page_index + 1} final size: {img.width}x{img.height}", file=sys.stderr)
    out_path = output_dir / f"page_{page_index + 1:05d}.png"
    img.save(out_path, "PNG")
    doc.close()
    return out_path, page_index


def render_pdf_to_pngs(pdf_path: Path, output_dir: Path) -> list[Path]:
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


def extract_pdf_text_and_visual_pages(pdf_path: Path) -> tuple[str, list[Path], list[int]]:
    doc = fitz.open(pdf_path)
    num_pages = len(doc)
    doc.close()

    output_dir = Path(tempfile.gettempdir()) / "idp_pdf_pages" / pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    pngs: list[Path] = []
    visual_page_indices: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
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
    for path, page_index in results:
        pngs.append(path)
        visual_page_indices.append(page_index)

    print(f"Renderer: produced {len(pngs)} images", file=sys.stderr)
    return "", pngs, visual_page_indices
