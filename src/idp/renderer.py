"""Render PDF pages to PNG images without reading the text layer."""

from __future__ import annotations

from pathlib import Path

import fitz
from PIL import Image

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
