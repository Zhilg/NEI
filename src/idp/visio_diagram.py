"""Convert VLM-analyzed Visio diagrams to editable .vsdx drawings.

This module takes the JSON output from the VLM Visio diagram analysis and
converts it into a Visio drawing with editable shapes (rectangles, ellipses,
diamonds, lines, connectors, text boxes).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from idp.visio_exporter import (
    VisioPage,
    VisioShape,
    write_drawing,
    image_size,
    IMAGE_CONTENT_TYPES,
)

logger = logging.getLogger(__name__)

VISIO_NS = "http://schemas.microsoft.com/office/visio/2012/main"

COORD_SCALE = 1000.0
DEFAULT_DPI = 200
INCH_PER_CM = 1 / 2.54

SHAPE_TYPE_MAP = {
    "rectangle": "rectangle",
    "rounded_rectangle": "rounded_rectangle",
    "ellipse": "ellipse",
    "diamond": "diamond",
    "triangle": "triangle",
    "hexagon": "hexagon",
    "text": "text",
    "image": "image",
    "line": "line",
    "connector": "connector",
    "group": "group",
}

LINE_STYLE_MAP = {
    "solid": 1,
    "dashed": 2,
    "dotted": 3,
    "dash_dot": 4,
    "dash_dot_dot": 5,
}

ARROW_MAP = {
    "none": 0,
    "arrow": 4,
    "open_arrow": 5,
    "dot": 6,
    "stealth": 7,
}

SIDE_MAP = {
    "top": "0",
    "bottom": "2",
    "left": "3",
    "right": "1",
}


@dataclass
class DiagramShape:
    id: str
    type: str
    x: float
    y: float
    width: float
    height: float
    text: str = ""
    font_size: float = 12
    bold: bool = False
    italic: bool = False
    fill: str = "#ffffff"
    line_color: str = "#000000"
    line_width: float = 1
    line_style: str = "solid"
    rotation: float = 0
    from_shape: str | None = None
    to_shape: str | None = None
    from_side: str = "right"
    to_side: str = "left"
    start_arrow: str = "none"
    end_arrow: str = "arrow"


def parse_diagram_json(data: dict) -> tuple[dict, list[DiagramShape]]:
    page_info = data.get("page", {"width": 1000, "height": 1000})
    raw_shapes = data.get("shapes", [])

    shapes: list[DiagramShape] = []
    for raw in raw_shapes:
        shape = DiagramShape(
            id=raw.get("id", f"shape_{len(shapes)}"),
            type=raw.get("type", "rectangle"),
            x=float(raw.get("x", 0)),
            y=float(raw.get("y", 0)),
            width=float(raw.get("width", 100)),
            height=float(raw.get("height", 50)),
            text=raw.get("text", ""),
            font_size=float(raw.get("font_size", 12)),
            bold=bool(raw.get("bold", False)),
            italic=bool(raw.get("italic", False)),
            fill=raw.get("fill", "#ffffff"),
            line_color=raw.get("line_color", "#000000"),
            line_width=float(raw.get("line_width", 1)),
            line_style=raw.get("line_style", "solid"),
            rotation=float(raw.get("rotation", 0)),
            from_shape=raw.get("from"),
            to_shape=raw.get("to"),
            from_side=raw.get("from_side", "right"),
            to_side=raw.get("to_side", "left"),
            start_arrow=raw.get("start_arrow", "none"),
            end_arrow=raw.get("end_arrow", "arrow"),
        )
        shapes.append(shape)
    return page_info, shapes


def _coord_to_inches(value: float, page_size_in: float, dpi: int = DEFAULT_DPI) -> float:
    return (value / COORD_SCALE) * page_size_in


def _create_rectangle_shape(shape: DiagramShape, page_width_in: float, page_height_in: float, index: int) -> VisioShape:
    left = _coord_to_inches(shape.x, page_width_in)
    top = _coord_to_inches(shape.y, page_height_in)
    width = _coord_to_inches(shape.width, page_width_in)
    height = _coord_to_inches(shape.height, page_height_in)
    bottom = page_height_in - top - height

    visible_line = shape.line_width > 0
    line_pattern = LINE_STYLE_MAP.get(shape.line_style, 1)

    return VisioShape(
        kind="rect",
        name=shape.id,
        left=left,
        bottom=bottom,
        width=width,
        height=height,
        text=shape.text,
        font_size=shape.font_size / 72.0,
        bold=shape.bold,
        italic=shape.italic,
        align="center",
        fill=shape.fill,
        margins=(0.05, 0.05, 0.05, 0.05),
        vertical_align=1,
    )


def _create_ellipse_shape(shape: DiagramShape, page_width_in: float, page_height_in: float, index: int) -> VisioShape:
    left = _coord_to_inches(shape.x, page_width_in)
    top = _coord_to_inches(shape.y, page_height_in)
    width = _coord_to_inches(shape.width, page_width_in)
    height = _coord_to_inches(shape.height, page_height_in)
    bottom = page_height_in - top - height

    return VisioShape(
        kind="rect",
        name=shape.id,
        left=left,
        bottom=bottom,
        width=width,
        height=height,
        text=shape.text,
        font_size=shape.font_size / 72.0,
        bold=shape.bold,
        italic=shape.italic,
        align="center",
        fill=shape.fill,
        margins=(0.05, 0.05, 0.05, 0.05),
        vertical_align=1,
    )


def _create_diamond_shape(shape: DiagramShape, page_width_in: float, page_height_in: float, index: int) -> VisioShape:
    left = _coord_to_inches(shape.x, page_width_in)
    top = _coord_to_inches(shape.y, page_height_in)
    width = _coord_to_inches(shape.width, page_width_in)
    height = _coord_to_inches(shape.height, page_height_in)
    bottom = page_height_in - top - height

    return VisioShape(
        kind="rect",
        name=shape.id,
        left=left,
        bottom=bottom,
        width=width,
        height=height,
        text=shape.text,
        font_size=shape.font_size / 72.0,
        bold=shape.bold,
        italic=shape.italic,
        align="center",
        fill=shape.fill,
        margins=(0.05, 0.05, 0.05, 0.05),
        vertical_align=1,
    )


def _create_text_shape(shape: DiagramShape, page_width_in: float, page_height_in: float, index: int) -> VisioShape:
    left = _coord_to_inches(shape.x, page_width_in)
    top = _coord_to_inches(shape.y, page_height_in)
    width = _coord_to_inches(shape.width, page_width_in)
    height = _coord_to_inches(shape.height, page_height_in)
    bottom = page_height_in - top - height

    return VisioShape(
        kind="text",
        name=shape.id,
        left=left,
        bottom=bottom,
        width=width,
        height=height,
        text=shape.text,
        font_size=shape.font_size / 72.0,
        bold=shape.bold,
        italic=shape.italic,
        align="left",
        fill="#ffffff",
        margins=(0.05, 0.05, 0.05, 0.05),
        vertical_align=0,
    )


def _create_line_shape(shape: DiagramShape, page_width_in: float, page_height_in: float, index: int) -> VisioShape:
    left = _coord_to_inches(shape.x, page_width_in)
    top = _coord_to_inches(shape.y, page_height_in)
    width = _coord_to_inches(shape.width, page_width_in)
    height = _coord_to_inches(shape.height, page_height_in)
    bottom = page_height_in - top - height

    return VisioShape(
        kind="rect",
        name=shape.id,
        left=left,
        bottom=bottom,
        width=max(width, 0.01),
        height=max(height, 0.01),
        text=shape.text,
        font_size=shape.font_size / 72.0,
        bold=shape.bold,
        italic=shape.italic,
        align="center",
        fill=shape.line_color,
        margins=(0, 0, 0, 0),
    )


def _create_connector_shape(shape: DiagramShape, page_width_in: float, page_height_in: float, index: int) -> VisioShape:
    left = _coord_to_inches(shape.x, page_width_in)
    top = _coord_to_inches(shape.y, page_height_in)
    width = _coord_to_inches(shape.width, page_width_in)
    height = _coord_to_inches(shape.height, page_height_in)
    bottom = page_height_in - top - height

    return VisioShape(
        kind="rect",
        name=shape.id,
        left=left,
        bottom=bottom,
        width=max(width, 0.01),
        height=max(height, 0.01),
        text=shape.text,
        font_size=shape.font_size / 72.0,
        bold=shape.bold,
        italic=shape.italic,
        align="center",
        fill=shape.line_color,
        margins=(0, 0, 0, 0),
    )


def _create_image_shape(shape: DiagramShape, page_width_in: float, page_height_in: float, index: int) -> VisioShape:
    left = _coord_to_inches(shape.x, page_width_in)
    top = _coord_to_inches(shape.y, page_height_in)
    width = _coord_to_inches(shape.width, page_width_in)
    height = _coord_to_inches(shape.height, page_height_in)
    bottom = page_height_in - top - height

    return VisioShape(
        kind="picture",
        name=shape.id,
        left=left,
        bottom=bottom,
        width=width,
        height=height,
        text="",
        image=None,
        compression="PNG",
    )


SHAPE_CREATORS = {
    "rectangle": _create_rectangle_shape,
    "rounded_rectangle": _create_rectangle_shape,
    "ellipse": _create_ellipse_shape,
    "diamond": _create_diamond_shape,
    "triangle": _create_diamond_shape,
    "hexagon": _create_diamond_shape,
    "text": _create_text_shape,
    "line": _create_line_shape,
    "connector": _create_connector_shape,
    "image": _create_image_shape,
}


def build_visio_diagram(
    diagram_data: dict,
    out_path: Path,
    *,
    title: str = "",
    dpi: int = DEFAULT_DPI,
) -> Path:
    page_info, shapes = parse_diagram_json(diagram_data)

    page_width_px = page_info.get("width", 1000)
    page_height_px = page_info.get("height", 1000)

    page_width_in = page_width_px / dpi
    page_height_in = page_height_px / dpi

    page = VisioPage(
        name="Page-1",
        width=page_width_in,
        height=page_height_in,
        shapes=[],
    )

    for index, shape in enumerate(shapes, start=1):
        creator = SHAPE_CREATORS.get(shape.type, _create_rectangle_shape)
        visio_shape = creator(shape, page_width_in, page_height_in, index)
        page.shapes.append(visio_shape)

    return write_drawing([page], out_path, title=title)


async def reconstruct_visio_diagram_from_png(
    png_path: Path,
    out_path: Path,
    *,
    title: str = "",
    dpi: int = DEFAULT_DPI,
) -> Path:
    from idp.vlm_client import reconstruct_visio_diagram

    diagram_data = await reconstruct_visio_diagram([png_path])
    return build_visio_diagram(diagram_data, out_path, title=title, dpi=dpi)