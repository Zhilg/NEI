"""Render PDF pages to PNG images without reading the text layer."""

from __future__ import annotations

import tempfile
from pathlib import Path

import fitz
from PIL import Image, ImageEnhance

from idp.config import settings


def render_pdf_to_pngs(pdf_path: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    pngs: list[Path] = []
    for page_index in range(len(doc)):
        page = doc.load_page(page_index)
        mat = fitz.Matrix(settings.render_dpi / 72, settings.render_dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        out_path = output_dir / f"page_{page_index + 1:05d}.png"
        img.save(out_path, "PNG")
        pngs.append(out_path)
    doc.close()
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


def extract_pdf_text_and_visual_pages(pdf_path: Path) -> tuple[str, list[Path], list[int]]:
    doc = fitz.open(pdf_path)
    num_pages = len(doc)
    doc.close()

    output_dir = Path(tempfile.gettempdir()) / "idp_pdf_pages" / pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    pngs: list[Path] = []
    visual_page_indices: list[int] = []
    doc = fitz.open(pdf_path)
    for page_index in range(num_pages):
        page = doc.load_page(page_index)
        mat = fitz.Matrix(settings.render_dpi / 72, settings.render_dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        img = _upscale_image(img, scale=settings.upscale_factor)
        img = _resize_to_max_dimension(img, settings.max_image_dimension)
        out_path = output_dir / f"page_{page_index + 1:05d}.png"
        img.save(out_path, "PNG")
        pngs.append(out_path)
        visual_page_indices.append(page_index)
    doc.close()

    return "", pngs, visual_page_indices
