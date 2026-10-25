"""Build a Visio drawing (.vsdx) from the text of a reconstructed document.

A bundle of a reconstructed document (``txts/<file name>/text.txt``) stores the
document as markdown: headings, paragraphs, lists, tables and links to the
pictures of the document.  This module turns that text into a Visio drawing
with *editable* shapes:

* headings and paragraphs become text shapes (Times New Roman, as in DOCX/PDF);
* list items become text shapes with a bullet;
* tables become a grid of rectangles, one shape per cell;
* pictures are embedded as picture shapes.

Unlike the page render export (:func:`idp.visio_exporter.build_vsdx`) the result
is not a set of images: the text can be corrected right in Visio.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from idp.visio_exporter import (
    IMAGE_CONTENT_TYPES,
    VisioPage,
    VisioShape,
    image_size,
    write_drawing,
)

logger = logging.getLogger(__name__)

A4_SIZE = (8.27, 11.69)
DEFAULT_MARGIN = 0.5
BODY_SIZE = 11.0
TABLE_SIZE = 10.0
LINE_FACTOR = 1.25
HEADING_SIZES = {1: 18.0, 2: 15.0, 3: 13.0, 4: 12.0, 5: 11.0, 6: 11.0}
HEADING_SPACE_BEFORE = {1: 0.20, 2: 0.16, 3: 0.12}
HEADING_SPACE_AFTER = 0.07
PARAGRAPH_SPACE_AFTER = 0.08
LIST_INDENT = 0.25
LIST_HANGING = 0.18
LIST_SPACE_AFTER = 0.04
CELL_PADDING = 0.05
MIN_COLUMN_WIDTH = 0.7
MAX_IMAGE_HEIGHT = 4.0
IMAGE_DPI = 150.0
WRAP_SAFETY = 1.03
TEXT_PADDING = 0.02

_FONT_DIR = Path(__file__).parent / "fonts"
_FONT_FILES = {
    (False, False): "LiberationSerif-Regular.ttf",
    (True, False): "LiberationSerif-Bold.ttf",
    (False, True): "LiberationSerif-Italic.ttf",
    (True, True): "LiberationSerif-Bold.ttf",
}
_MEASURE_SCALE = 4  # pixels per point used for measuring
_FONT_SIZE = 12.0


@lru_cache(maxsize=8)
def _font(bold: bool, italic: bool):
    """Font used for measuring the text (None when Pillow is unavailable)."""
    try:
        from PIL import ImageFont
    except Exception:  # noqa: BLE001 - measuring is optional
        return None
    path = _FONT_DIR / _FONT_FILES[(bold, italic)]
    if not path.is_file():
        return None
    try:
        return ImageFont.truetype(str(path), size=int(_FONT_SIZE * _MEASURE_SCALE))
    except Exception:  # noqa: BLE001 - measuring is optional
        return None


def text_width(text: str, size_pt: float, bold: bool = False, italic: bool = False) -> float:
    """Width of the text in inches."""
    font = _font(bold, italic)
    if font is not None:
        try:
            length = font.getlength(text)
            return length / (_MEASURE_SCALE * 72.0) * (size_pt / _FONT_SIZE)
        except Exception:  # noqa: BLE001 - fall back to the estimate below
            logger.debug("font measurement failed, using the average width")
    return len(text) * size_pt * 0.5 / 72.0


def wrap_text(
    text: str,
    width: float,
    size_pt: float,
    bold: bool = False,
    italic: bool = False,
) -> list[str]:
    """Split the text into lines that fit into ``width`` inches."""
    words = text.split()
    if not words:
        return [""]
    limit = max(0.2, width) * WRAP_SAFETY
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_width(candidate, size_pt, bold, italic) <= limit:
            current = candidate
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return lines


class _Layout:
    """Flow layout: shapes are placed from the top of the page downwards."""

    def __init__(self, page_size: tuple[float, float], margin: float) -> None:
        self.page_width, self.page_height = page_size
        self.margin = margin
        self.content_width = self.page_width - 2 * margin
        self.content_height = self.page_height - 2 * margin
        self.pages: list[VisioPage] = []
        self.page: VisioPage | None = None
        self.y = 0.0

    def start_page(self, force: bool = False) -> None:
        """Start a new page; an empty page is reused instead of adding one."""
        if self.page is not None and (not force or not self.page.shapes):
            return
        self.page = VisioPage(
            name=f"Page-{len(self.pages) + 1}",
            width=self.page_width,
            height=self.page_height,
        )
        self.pages.append(self.page)
        self.y = self.margin

    def space(self, amount: float) -> None:
        """Leave vertical space; the page breaks when the space does not fit."""
        if amount <= 0:
            return
        if self.page is None:
            self.start_page()
        assert self.page is not None
        if self.y + amount > self.page_height - self.margin and self.page.shapes:
            self.start_page(force=True)
            return
        self.y += amount

    def place(self, shape: VisioShape, height: float, *, break_allowed: bool = True) -> None:
        """Put a shape at the current position and move the cursor down."""
        if self.page is None:
            self.start_page()
        assert self.page is not None
        if break_allowed and self.y + height > self.page_height - self.margin and self.page.shapes:
            self.start_page(force=True)
        shape.bottom = self.page_height - self.y - height
        self.page.shapes.append(shape)
        self.y += height


def _add_text(
    layout: _Layout,
    text: str,
    *,
    size: float = BODY_SIZE,
    bold: bool = False,
    italic: bool = False,
    indent: float = 0.0,
    hanging: float = 0.0,
    align: str = "left",
    name: str = "Text",
    space_after: float = 0.0,
) -> None:
    """Add a text block; long blocks are split over several pages."""
    if not text.strip():
        return
    width = max(0.5, layout.content_width - indent)
    lines = wrap_text(text, max(0.3, width - hanging), size, bold, italic)
    line_height = size * LINE_FACTOR / 72.0
    max_lines = max(1, int(layout.content_height // line_height))
    chunks = [lines[index : index + max_lines] for index in range(0, len(lines), max_lines)]
    for index, chunk in enumerate(chunks):
        height = len(chunk) * line_height + TEXT_PADDING
        shape = VisioShape(
            kind="text",
            name=f"{name} {index + 1}" if len(chunks) > 1 else name,
            left=layout.margin + indent,
            bottom=0.0,
            width=width,
            height=height,
            text="\n".join(chunk),
            font_size=size / 72.0,
            bold=bold,
            italic=italic,
            align=align,
            indent=hanging,
        )
        layout.place(shape, height)
    if space_after:
        layout.space(space_after)


def _column_widths(rows: list[list[str]], available: float) -> list[float]:
    """Column widths of a table: proportional to the widest cell."""
    columns = max((len(row) for row in rows), default=0)
    if columns == 0:
        return []
    weights = []
    for column in range(columns):
        longest = 0.0
        for row in rows:
            if column >= len(row):
                continue
            longest = max(longest, text_width(row[column], TABLE_SIZE, bold=True))
        weights.append(max(longest, MIN_COLUMN_WIDTH))
    total = sum(weights)
    if total <= 0:
        return [available / columns] * columns
    return [available * weight / total for weight in weights]


def _add_table_row(
    layout: _Layout,
    row: list[str],
    widths: list[float],
    *,
    header: bool,
    name: str,
    repeat_header: list[str] | None = None,
) -> None:
    """Add one row of a table; the header is repeated after a page break."""
    size = TABLE_SIZE
    line_height = size * LINE_FACTOR / 72.0
    cells: list[tuple[int, list[str]]] = []
    row_lines = 1
    for column, width in enumerate(widths):
        text = row[column] if column < len(row) else ""
        lines = wrap_text(text, width - 2 * CELL_PADDING, size, bold=header)
        cells.append((column, lines))
        row_lines = max(row_lines, len(lines))
    height = row_lines * line_height + 2 * CELL_PADDING
    if (
        layout.page is not None
        and layout.page.shapes
        and layout.y + height > layout.page_height - layout.margin
    ):
        layout.start_page(force=True)
        if repeat_header is not None:
            _add_table_row(layout, repeat_header, widths, header=True, name="Header")
    for column, lines in cells:
        shape = VisioShape(
            kind="rect",
            name=f"{name} {column + 1}",
            left=layout.margin + sum(widths[:column]),
            bottom=0.0,
            width=widths[column],
            height=height,
            text="\n".join(lines),
            font_size=size / 72.0,
            bold=header,
            fill="#f2f2f2" if header else "#ffffff",
            margins=(CELL_PADDING, CELL_PADDING, CELL_PADDING / 2, CELL_PADDING / 2),
            vertical_align=1,
        )
        layout.place(shape, height, break_allowed=False)


def _add_table(layout: _Layout, rows: list[list[str]]) -> None:
    """Add a table as a grid of cell shapes."""
    if not rows:
        return
    widths = _column_widths(rows, layout.content_width)
    header = rows[0] if len(rows) > 1 else None
    body = rows[1:] if header is not None else rows
    if header is not None:
        _add_table_row(layout, header, widths, header=True, name="Header")
    for index, row in enumerate(body, start=1):
        _add_table_row(
            layout,
            row,
            widths,
            header=False,
            name=f"Row {index}",
            repeat_header=header,
        )
    layout.space(PARAGRAPH_SPACE_AFTER)


def _add_image(layout: _Layout, path: Path, alt: str = "") -> None:
    """Embed a picture, scaled to the width of the text area."""
    width_px, height_px = image_size(path)
    if width_px <= 0 or height_px <= 0:
        return
    width = width_px / IMAGE_DPI
    height = height_px / IMAGE_DPI
    scale = min(1.0, layout.content_width / width, MAX_IMAGE_HEIGHT / height)
    width *= scale
    height *= scale
    extension = path.suffix.lower()
    shape = VisioShape(
        kind="picture",
        name=alt or path.name,
        left=layout.margin + (layout.content_width - width) / 2.0,
        bottom=0.0,
        width=width,
        height=height,
        image=path,
        compression=IMAGE_CONTENT_TYPES.get(extension, ("image/png", "PNG"))[1],
    )
    layout.place(shape, height)
    layout.space(PARAGRAPH_SPACE_AFTER)


def _resolve_image(base_dir: Path | None, path: str) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    return candidate if candidate.is_file() else None


def _add_paragraph(layout: _Layout, segments: list, base_dir: Path | None) -> None:
    """Add a paragraph: its text and the pictures it links to."""
    texts: list[str] = []
    pictures: list[tuple[str, Path | None]] = []
    for segment in segments:
        kind = getattr(segment, "kind", "text")
        if kind == "image":
            pictures.append(
                (getattr(segment, "alt", ""), _resolve_image(base_dir, getattr(segment, "path", "")))
            )
        elif kind == "handwritten":
            texts.append(getattr(segment, "text", "") or "")
            pictures.append(("", _resolve_image(base_dir, getattr(segment, "path", ""))))
        else:
            texts.append(getattr(segment, "text", "") or "")
    text = "".join(texts).strip()
    if text:
        _add_text(
            layout,
            text,
            name="Paragraph",
            space_after=PARAGRAPH_SPACE_AFTER,
        )
    for alt, path in pictures:
        if path is not None:
            _add_image(layout, path, alt)
        elif alt:
            _add_text(layout, f"[{alt}]", name="Caption", space_after=PARAGRAPH_SPACE_AFTER)


def build_layout_vsdx(
    layout: dict,
    out_path: Path,
    *,
    page_size: tuple[float, float] | None = None,
    margin: float = DEFAULT_MARGIN,
    title: str = "",
    base_dir: Path | None = None,
) -> Path:
    """Write a Visio drawing with the layout the VLM produced.

    ``layout`` is the JSON the VLM returned: ``{"blocks": [ ... ]}``. Every
    block becomes one or more editable shapes, placed in reading order.
    """
    blocks = layout.get("blocks", []) if isinstance(layout, dict) else []
    flow = _Layout(page_size or A4_SIZE, margin)
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "heading":
            level = min(max(int(block.get("level", 1) or 1), 1), 6)
            text = str(block.get("text", "")).strip()
            flow.space(HEADING_SPACE_BEFORE.get(level, 0.10))
            _add_text(
                flow,
                text,
                size=HEADING_SIZES.get(level, BODY_SIZE),
                bold=True,
                name=f"Heading {level}",
                space_after=HEADING_SPACE_AFTER,
            )
        elif kind == "paragraph":
            text = str(block.get("text", "")).strip()
            _add_text(flow, text, name="Paragraph", space_after=PARAGRAPH_SPACE_AFTER)
        elif kind == "list":
            for item in block.get("items", []) or []:
                _add_text(
                    flow,
                    f"• {item}".strip(),
                    indent=LIST_INDENT,
                    hanging=LIST_HANGING,
                    name="List",
                    space_after=LIST_SPACE_AFTER,
                )
        elif kind == "table":
            rows = block.get("rows", []) or []
            _add_table(flow, [[str(cell) for cell in row] for row in rows])
        elif kind == "image":
            caption = str(block.get("caption", "")).strip()
            if caption:
                _add_text(flow, f"[{caption}]", name="Caption", space_after=PARAGRAPH_SPACE_AFTER)
    if not flow.pages:
        flow.start_page()
    return write_drawing(flow.pages, out_path, title=title)


def build_document_vsdx(
    blocks: list,
    out_path: Path,
    *,
    page_size: tuple[float, float] | None = None,
    margin: float = DEFAULT_MARGIN,
    title: str = "",
    base_dir: Path | None = None,
) -> Path:
    """Write a Visio drawing with the editable content of a document.

    ``blocks`` are the blocks of a parsed bundle (see
    :func:`idp.reconstructor.parse_bundle`); ``base_dir`` is the directory the
    links to the pictures are resolved against.
    """
    layout = _Layout(page_size or A4_SIZE, margin)
    for block in blocks:
        kind = getattr(block, "kind", "")
        if kind == "page_break":
            layout.start_page(force=True)
        elif kind == "heading":
            level = min(max(int(getattr(block, "level", 1) or 1), 1), 6)
            text = "".join(
                getattr(segment, "text", "") or "" for segment in getattr(block, "segments", [])
            ).strip()
            layout.space(HEADING_SPACE_BEFORE.get(level, 0.10))
            _add_text(
                layout,
                text,
                size=HEADING_SIZES.get(level, BODY_SIZE),
                bold=True,
                name=f"Heading {level}",
                space_after=HEADING_SPACE_AFTER,
            )
        elif kind == "list":
            for item in getattr(block, "items", []):
                _add_text(
                    layout,
                    f"• {item}".strip(),
                    indent=LIST_INDENT,
                    hanging=LIST_HANGING,
                    name="List",
                    space_after=LIST_SPACE_AFTER,
                )
        elif kind == "table":
            _add_table(layout, getattr(block, "rows", []))
        elif kind == "paragraph":
            _add_paragraph(layout, getattr(block, "segments", []), base_dir)
    if not layout.pages:
        layout.start_page()
    return write_drawing(layout.pages, out_path, title=title)
