"""Convert PPTX files to Markdown with slides, tables, and noise removal."""

from __future__ import annotations

import re
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu


_HEADING_PATTERN = re.compile(r"^(?:Глава\s+\d+|[0-9]+(?:\.[0-9]+)*\s+.+)$", re.UNICODE)
_NOISE_PATTERNS = [
    re.compile(r"^\s*(?:Стр\.|Page\s+\d+|Page\s*:.*|Слайд\s+\d+|Slide\s+\d+)\s*$", re.IGNORECASE),
]


def _is_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _HEADING_PATTERN.match(stripped):
        return True
    if len(stripped.split()) <= 12 and stripped.endswith((":", "：")):
        return True
    return False


def _format_heading(text: str) -> str:
    stripped = text.strip()
    if re.match(r"^Глава\s+\d+", stripped, re.IGNORECASE):
        return f"# {stripped}"
    parts = stripped.split(None, 1)
    if re.match(r"^[0-9]+(?:\.[0-9]+)*$", parts[0]):
        depth = parts[0].count(".") + 1
        return f"{'#' * min(depth, 6)} {stripped}"
    return f"### {stripped}"


def _table_to_markdown(table) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            text = " ".join(
                p.text.strip() for p in cell.text_frame.paragraphs if p.text.strip()
            )
            cells.append(text)
        rows.append(cells)
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    normalized = [r + [""] * (cols - len(r)) for r in rows]
    md_lines = []
    for i, row in enumerate(normalized):
        md_lines.append("| " + " | ".join(row) + " |")
        if i == 0:
            md_lines.append("| " + " | ".join(["---"] * cols) + " |")
    return "\n".join(md_lines)


def _clean_noise(text: str) -> str:
    lines = text.splitlines()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append("")
            continue
        if any(p.match(stripped) for p in _NOISE_PATTERNS):
            continue
        cleaned.append(line)
    text = "\n".join(cleaned)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def convert_pptx_to_markdown(pptx_path: Path) -> str:
    prs = Presentation(str(pptx_path))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        texts: list[str] = []
        table_blocks: list[str] = []
        for shape in slide.shapes:
            try:
                if not hasattr(shape, "text"):
                    continue
                text = shape.text.strip()
                if not text:
                    continue
                if getattr(shape, "has_table", False):
                    table_blocks.append(_table_to_markdown(shape.table))
                    continue
                if any(p.match(text.strip()) for p in _NOISE_PATTERNS):
                    continue
                if _is_heading(text):
                    texts.append(_format_heading(text))
                else:
                    texts.append(text)
            except Exception:
                continue
        block_parts: list[str] = []
        if texts:
            block_parts.append("\n".join(texts))
        if table_blocks:
            block_parts.append("\n".join(table_blocks))
        if block_parts:
            parts.append(f"<!-- slide {i} -->\n" + "\n\n".join(block_parts))
    text = "\n\n".join(parts)
    return _clean_noise(text)
