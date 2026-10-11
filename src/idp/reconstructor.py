"""Rebuild DOCX and PDF documents from txts bundles.

Each ``txts/<file name>/`` bundle contains ``text.txt`` (document text with
hyperlinks to images), ``images/`` (page renders and embedded pictures),
``tables.json`` and ``meta.json``. This module parses the text back into
blocks (headings, paragraphs with inline images, tables, lists, page breaks)
and renders it as a real DOCX and/or PDF document with the images in place.

Run standalone:  idp-reconstruct [--bundle DIR] [--root DIR] [--format docx,pdf]
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from idp.config import settings

_PAGE_MARKER_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_TABLE_SEPARATOR_RE = re.compile(r"^[:\-\s]+$")
_INLINE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)|\[HANDWRITTEN:\s*([^\]]*)\]\(([^)\s]+)\)")
_LINK_ANY_RE = re.compile(r"(!?\[[^\]]*?\])\(([^)]*?)\)", re.DOTALL)
_ESCAPE_RE = re.compile(r"\\([.\-*+\\`()\[\]{}#!|>~])")

_PAGE_WIDTH = 595.28
_PAGE_HEIGHT = 841.89
_MARGIN = 56.7  # 2 cm
_GAP = 6.0
_FONT_SIZE = 12.0
_TABLE_FONT_SIZE = 11.0
_HEADING_FONT_SIZE = 14.0
_LINE_HEIGHT = 1.5
_MAX_IMAGE_WIDTH_CM = 17.0
_MAX_IMAGE_HEIGHT_CM = 24.0
_DOCX_FONT = "Times New Roman"
_DOCX_FONT_SIZE = 12
_DOCX_HEADING_FONT_SIZE = 14
_FONT_DIR = Path(__file__).parent / "fonts"
_FONT_CANDIDATES = (
    str(_FONT_DIR / "LiberationSerif-Regular.ttf"),
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
)


@dataclass
class Segment:
    kind: str
    text: str = ""
    path: str = ""
    alt: str = ""


@dataclass
class Block:
    kind: str
    level: int = 0
    segments: list[Segment] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


def _unescape(text: str) -> str:
    return _ESCAPE_RE.sub(r"\1", text)


def _split_segments(line: str) -> list[Segment]:
    segments: list[Segment] = []
    position = 0
    for match in _INLINE_RE.finditer(line):
        if match.start() > position:
            segments.append(Segment("text", text=line[position:match.start()]))
        if match.group(2) is not None:
            segments.append(Segment("image", path=match.group(2), alt=match.group(1)))
        else:
            segments.append(Segment("handwritten", text=match.group(3) or "", path=match.group(4) or ""))
        position = match.end()
    if position < len(line):
        segments.append(Segment("text", text=line[position:]))
    return segments


def _join_wrapped_links(text: str) -> str:
    """Repair image links that were wrapped across lines.

    A soft wrap inside a markdown link (alt text or path) breaks the link
    entirely, so the newline is removed and the link reassembled.
    """

    def _fix(match: re.Match) -> str:
        label = " ".join(match.group(1).split())
        path = "".join(match.group(2).split())
        return f"{label}({path})"

    return _LINK_ANY_RE.sub(_fix, text)


def _is_plain_line(line: str) -> bool:
    """A markdown paragraph line (soft wraps are joined into one paragraph)."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("<!--") or stripped.startswith("#") or stripped.startswith("```"):
        return False
    if _TABLE_ROW_RE.match(stripped) and stripped.count("|") >= 2:
        return False
    return _BULLET_RE.match(stripped) is None


def parse_bundle(text: str) -> list[Block]:
    blocks: list[Block] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("<!--"):
            marker = _PAGE_MARKER_RE.search(stripped)
            if marker is not None:
                blocks.append(Block("page_break", level=int(marker.group(1))))
            index += 1
            continue
        if _TABLE_ROW_RE.match(stripped) and stripped.count("|") >= 2:
            rows: list[list[str]] = []
            while index < len(lines):
                row_line = lines[index].strip()
                if not (_TABLE_ROW_RE.match(row_line) and row_line.count("|") >= 2):
                    break
                cells = [cell.strip() for cell in row_line.strip("|").split("|")]
                if not all(_TABLE_SEPARATOR_RE.fullmatch(cell) for cell in cells if cell):
                    rows.append(cells)
                index += 1
            if rows:
                blocks.append(Block("table", rows=rows))
            continue
        heading = _HEADING_RE.match(stripped)
        if heading is not None:
            blocks.append(Block("heading", level=len(heading.group(1)), segments=[Segment("text", text=heading.group(2).strip())]))
            index += 1
            continue
        if _BULLET_RE.match(stripped):
            items: list[str] = []
            while index < len(lines):
                item = _BULLET_RE.match(lines[index].strip())
                if item is None:
                    break
                items.append(item.group(1).strip())
                index += 1
            blocks.append(Block("list", items=items))
            continue
        paragraph_lines = [stripped]
        index += 1
        while index < len(lines) and _is_plain_line(lines[index]):
            paragraph_lines.append(lines[index].strip())
            index += 1
        paragraph = " ".join(part for part in paragraph_lines if part)
        segments = _split_segments(paragraph)
        if segments:
            blocks.append(Block("paragraph", segments=segments))
    return blocks


def _image_size_cm(image_path: Path) -> tuple[float, float] | None:
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            width_px, height_px = image.size
    except Exception:
        return None
    if not width_px or not height_px:
        return None
    width_cm = width_px / 150.0 * 2.54
    height_cm = height_px / 150.0 * 2.54
    scale = min(_MAX_IMAGE_WIDTH_CM / width_cm, _MAX_IMAGE_HEIGHT_CM / height_cm, 1.0)
    return width_cm * scale, height_cm * scale


def _add_docx_table(document, rows: list[list[str]]) -> None:
    from docx.shared import Pt

    columns = max(len(row) for row in rows)
    table = document.add_table(rows=len(rows), cols=columns)
    table.style = "Table Grid"
    for row_index, row in enumerate(rows):
        for column in range(columns):
            cell = table.cell(row_index, column)
            cell.text = _unescape(row[column]) if column < len(row) else ""
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.line_spacing = 1.0
                for run in paragraph.runs:
                    run.font.name = _DOCX_FONT
                    run.font.size = Pt(_DOCX_FONT_SIZE)
                    if row_index == 0 and len(rows) > 1:
                        run.bold = True


def _setup_docx_styles(document) -> None:
    """Times New Roman 12, 1.5 line spacing, 2 cm margins, headings at 14 pt."""
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    for section in document.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2)
        section.right_margin = Cm(2)

    normal = document.styles["Normal"]
    normal.font.name = _DOCX_FONT
    normal.font.size = Pt(_DOCX_FONT_SIZE)
    normal.paragraph_format.line_spacing = _LINE_HEIGHT
    normal.paragraph_format.space_after = Pt(0)
    rpr = normal.element.get_or_add_rPr()
    fonts = rpr.get_or_add_rFonts()
    for attribute in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attribute), _DOCX_FONT)

    for level in range(1, 7):
        style = document.styles[f"Heading {level}"]
        style.font.name = _DOCX_FONT
        style.font.size = Pt(_DOCX_HEADING_FONT_SIZE)
        style.font.bold = True
        style.font.color.rgb = None
        style.paragraph_format.line_spacing = _LINE_HEIGHT
        style.paragraph_format.space_before = Pt(12)
        style.paragraph_format.space_after = Pt(6)
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.get_or_add_rFonts()
        for attribute in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            fonts.set(qn(attribute), _DOCX_FONT)


def build_docx(blocks: list[Block], bundle_dir: Path, output_path: Path) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    document = Document()
    _setup_docx_styles(document)
    has_content = False
    for block in blocks:
        if block.kind == "page_break":
            if has_content:
                document.add_page_break()
                has_content = False
        elif block.kind == "heading":
            text = _unescape(block.segments[0].text) if block.segments else ""
            heading = document.add_heading("", level=min(max(block.level, 1), 6))
            run = heading.add_run(text)
            run.font.name = _DOCX_FONT
            run.font.size = Pt(_DOCX_HEADING_FONT_SIZE)
            run.font.bold = True
            has_content = True
        elif block.kind == "list":
            for item in block.items:
                entry = document.add_paragraph(style="List Bullet")
                entry.add_run(_unescape(item))
            has_content = True
        elif block.kind == "table":
            _add_docx_table(document, block.rows)
            has_content = True
        elif block.kind == "paragraph":
            only_images = all(segment.kind == "image" for segment in block.segments)
            paragraph = document.add_paragraph()
            if only_images:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for segment in block.segments:
                if segment.kind == "text":
                    paragraph.add_run(_unescape(segment.text))
                elif segment.kind == "handwritten":
                    image_path = bundle_dir / segment.path
                    if image_path.is_file():
                        run = paragraph.add_run()
                        size = _image_size_cm(image_path)
                        if size is not None:
                            run.add_picture(str(image_path), width=Cm(size[0]), height=Cm(size[1]))
                        else:
                            run.add_picture(str(image_path))
                        if segment.text:
                            caption = paragraph.add_run(_unescape(segment.text))
                            caption.italic = True
                    else:
                        run = paragraph.add_run(_unescape(segment.text))
                        run.italic = True
                else:
                    image_path = bundle_dir / segment.path
                    if not image_path.is_file():
                        if segment.alt:
                            paragraph.add_run(f"[{segment.alt}]")
                        continue
                    run = paragraph.add_run()
                    size = _image_size_cm(image_path)
                    if size is not None:
                        run.add_picture(str(image_path), width=Cm(size[0]), height=Cm(size[1]))
                    else:
                        run.add_picture(str(image_path))
            has_content = True
    document.save(str(output_path))
    return output_path


def _resolve_font(bold: bool = False) -> tuple[str, str | None]:
    for candidate in _FONT_CANDIDATES:
        path = Path(candidate)
        if not path.is_file():
            continue
        if not bold:
            return "F0", candidate
        bold_path = path.with_name(path.name.replace("Regular", "Bold"))
        if bold_path.is_file():
            return "FB", str(bold_path)
        return "F0", candidate
    return "china-s", None


def _chars_per_line(width: float, font_size: float) -> int:
    return max(4, int(width / (font_size * 0.52)))


def _text_lines_needed(text: str, width: float, font_size: float) -> int:
    lines = 0
    for chunk in text.split("\n"):
        lines += max(1, math.ceil(len(chunk) / _chars_per_line(width, font_size)))
    return max(1, lines)


def build_pdf(blocks: list[Block], bundle_dir: Path, output_path: Path) -> Path:
    import fitz

    fontname, fontfile = _resolve_font()
    document = fitz.open()
    page = document.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
    state = {"y": _MARGIN, "used": False}

    def start_page() -> None:
        nonlocal page
        page = document.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
        state["y"] = _MARGIN
        state["used"] = False

    def write_text(text: str, font_size: float = _FONT_SIZE, bold: bool = False) -> None:
        if not text.strip():
            return
        active_font, active_file = _resolve_font(bold)
        for attempt in range(2):
            rect = fitz.Rect(_MARGIN, state["y"], _PAGE_WIDTH - _MARGIN, _PAGE_HEIGHT - _MARGIN)
            leftover = page.insert_textbox(
                rect,
                text,
                fontsize=font_size,
                fontname=active_font,
                fontfile=active_file,
                lineheight=_LINE_HEIGHT,
            )
            if leftover >= 0:
                state["y"] = _PAGE_HEIGHT - _MARGIN - leftover + _GAP
                state["used"] = True
                return
            start_page()
            font_size = max(8.0, font_size - 1.0)
        state["y"] = _MARGIN

    def write_image(image_path: Path) -> None:
        if not image_path.is_file():
            return
        try:
            pixmap = fitz.Pixmap(str(image_path))
        except Exception:
            return
        content_width = _PAGE_WIDTH - 2 * _MARGIN
        content_height = _PAGE_HEIGHT - 2 * _MARGIN
        remaining = _PAGE_HEIGHT - _MARGIN - state["y"]
        scale = min(content_width / pixmap.width, remaining / pixmap.height)
        if scale < min(content_width / pixmap.width, content_height / pixmap.height):
            start_page()
            scale = min(content_width / pixmap.width, content_height / pixmap.height)
        width = pixmap.width * scale
        height = pixmap.height * scale
        rect = fitz.Rect(_MARGIN, state["y"], _MARGIN + width, state["y"] + height)
        page.insert_image(rect, filename=str(image_path))
        state["y"] += height + _GAP
        state["used"] = True

    def write_table(rows: list[list[str]]) -> None:
        columns = max(len(row) for row in rows)
        weights = [
            max(6, max((len(row[column]) for row in rows if column < len(row)), default=6))
            for column in range(columns)
        ]
        available = _PAGE_WIDTH - 2 * _MARGIN
        column_widths = [weight / sum(weights) * available for weight in weights]

        def row_height(row: list[str]) -> float:
            lines = 1
            for column, cell in enumerate(row):
                if not cell:
                    continue
                lines = max(lines, _text_lines_needed(cell, column_widths[column], _TABLE_FONT_SIZE))
            return lines * _TABLE_FONT_SIZE * 1.35 + 6.0

        def draw_row(row: list[str]) -> None:
            height = row_height(row)
            x = _MARGIN
            for column in range(columns):
                cell_rect = fitz.Rect(x, state["y"], x + column_widths[column], state["y"] + height)
                page.draw_rect(cell_rect, color=(0.55, 0.55, 0.55), width=0.5)
                cell_text = _unescape(row[column]) if column < len(row) else ""
                if cell_text:
                    inner = fitz.Rect(cell_rect.x0 + 2, cell_rect.y0 + 2, cell_rect.x1 - 2, cell_rect.y1 - 2)
                    page.insert_textbox(
                        inner,
                        cell_text,
                        fontsize=_TABLE_FONT_SIZE,
                        fontname=fontname,
                        fontfile=fontfile,
                        lineheight=1.2,
                    )
                x += column_widths[column]
            state["y"] += height

        header = rows[0] if len(rows) > 1 else None
        index = 0
        while index < len(rows):
            height = row_height(rows[index])
            if state["used"] and state["y"] + height > _PAGE_HEIGHT - _MARGIN:
                start_page()
                if header is not None and index > 0:
                    draw_row(header)
            draw_row(rows[index])
            index += 1
        state["y"] += _GAP
        state["used"] = True

    for block in blocks:
        if block.kind == "page_break":
            if state["used"]:
                start_page()
        elif block.kind == "heading":
            text = _unescape(block.segments[0].text) if block.segments else ""
            write_text(text, _HEADING_FONT_SIZE, bold=True)
        elif block.kind == "list":
            for item in block.items:
                write_text(f"• {_unescape(item)}")
        elif block.kind == "table":
            write_table(block.rows)
        elif block.kind == "paragraph":
            pending = ""
            for segment in block.segments:
                if segment.kind == "image":
                    write_text(pending)
                    pending = ""
                    write_image(bundle_dir / segment.path)
                elif segment.kind == "handwritten":
                    image_path = bundle_dir / segment.path
                    if image_path.is_file():
                        write_text(pending)
                        pending = ""
                        write_image(image_path)
                        if segment.text:
                            write_text(_unescape(segment.text))
                    else:
                        pending += _unescape(segment.text)
                else:
                    pending += _unescape(segment.text)
            write_text(pending)

    document.save(str(output_path))
    return output_path


def reconstruct_bundle(bundle_dir: Path, formats: list[str] | None = None) -> list[Path]:
    text_file = bundle_dir / "text.txt"
    if not text_file.is_file():
        return []
    requested = formats if formats is not None else list(settings.reconstruct_formats)
    blocks = parse_bundle(_join_wrapped_links(text_file.read_text(encoding="utf-8")))
    created: list[Path] = []
    for fmt in requested:
        fmt = fmt.lower().lstrip(".")
        if fmt == "docx":
            created.append(build_docx(blocks, bundle_dir, bundle_dir / "reconstruct.docx"))
        elif fmt == "pdf":
            created.append(build_pdf(blocks, bundle_dir, bundle_dir / "reconstruct.pdf"))
    return created


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild DOCX/PDF documents from txts bundles")
    parser.add_argument("--bundle", type=Path, help="single bundle directory")
    parser.add_argument("--root", type=Path, help="bundles root (default: <output_root>/txts)")
    parser.add_argument("--format", default="", help="comma separated formats: docx,pdf")
    args = parser.parse_args()

    formats = [item.strip() for item in args.format.split(",") if item.strip()] or None
    if args.bundle is not None:
        targets = [args.bundle]
    else:
        root = args.root or settings.output_root / "txts"
        targets = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []

    for target in targets:
        for created in reconstruct_bundle(target, formats):
            print(f"created {created}")
    if not targets:
        print(f"no bundles found in {args.root or settings.output_root / 'txts'}")


if __name__ == "__main__":
    main()
