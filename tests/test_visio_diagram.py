"""Tests for Visio diagram reconstruction from PNG."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from idp.visio_diagram import (
    DiagramShape,
    build_visio_diagram,
    parse_diagram_json,
)
from idp.visio_exporter import VISIO_NS, inspect_vsdx, validate_vsdx


def _make_png(path: Path, width: int = 200, height: int = 300) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), "#3366cc").save(path, "PNG")
    return path


def _shapes(vsdx_path: Path, part: str = "visio/pages/page1.xml") -> list[ET.Element]:
    with zipfile.ZipFile(vsdx_path) as archive:
        page = ET.fromstring(archive.read(part))
    return page.findall(f".//{{{VISIO_NS}}}Shape")


def test_parse_diagram_json():
    data = {
        "page": {"width": 1000, "height": 800},
        "shapes": [
            {
                "id": "rect_1",
                "type": "rectangle",
                "x": 100,
                "y": 100,
                "width": 200,
                "height": 100,
                "text": "Process",
                "font_size": 12,
                "bold": True,
                "fill": "#ffffff",
                "line_color": "#000000",
                "line_width": 1,
                "line_style": "solid",
            },
            {
                "id": "conn_1",
                "type": "connector",
                "x": 300,
                "y": 150,
                "width": 100,
                "height": 10,
                "from": "rect_1",
                "to": "rect_2",
                "text": "Yes",
                "start_arrow": "none",
                "end_arrow": "arrow",
            },
        ],
    }
    page_info, shapes = parse_diagram_json(data)
    assert page_info["width"] == 1000
    assert page_info["height"] == 800
    assert len(shapes) == 2
    assert shapes[0].id == "rect_1"
    assert shapes[0].type == "rectangle"
    assert shapes[1].type == "connector"
    assert shapes[1].from_shape == "rect_1"
    assert shapes[1].to_shape == "rect_2"


def test_build_visio_diagram_basic(tmp_path: Path):
    _make_png(tmp_path / "dummy.png")

    diagram_data = {
        "page": {"width": 1000, "height": 800},
        "shapes": [
            {
                "id": "rect_1",
                "type": "rectangle",
                "x": 100,
                "y": 100,
                "width": 200,
                "height": 100,
                "text": "Start",
                "font_size": 14,
                "bold": True,
                "fill": "#e8f5e9",
                "line_color": "#2e7d32",
                "line_width": 2,
                "line_style": "solid",
            },
            {
                "id": "diamond_1",
                "type": "diamond",
                "x": 400,
                "y": 100,
                "width": 150,
                "height": 150,
                "text": "Decision?",
                "font_size": 12,
                "fill": "#fff3e0",
                "line_color": "#ef6c00",
                "line_width": 1,
                "line_style": "dashed",
            },
            {
                "id": "text_1",
                "type": "text",
                "x": 100,
                "y": 300,
                "width": 300,
                "height": 50,
                "text": "Annotation text",
                "font_size": 10,
                "italic": True,
            },
        ],
    }

    out_path = tmp_path / "diagram.vsdx"
    build_visio_diagram(diagram_data, out_path, title="Test Diagram")

    assert out_path.is_file()
    assert validate_vsdx(out_path) == []

    summary = inspect_vsdx(out_path)
    assert summary["pages"] == 1
    assert summary["texts"] == 3

    shapes = _shapes(out_path)
    assert len(shapes) == 3


def test_build_visio_diagram_with_connectors(tmp_path: Path):
    _make_png(tmp_path / "dummy.png")

    diagram_data = {
        "page": {"width": 1000, "height": 800},
        "shapes": [
            {
                "id": "rect_1",
                "type": "rectangle",
                "x": 100,
                "y": 100,
                "width": 200,
                "height": 80,
                "text": "Start",
            },
            {
                "id": "rect_2",
                "type": "rectangle",
                "x": 500,
                "y": 100,
                "width": 200,
                "height": 80,
                "text": "End",
            },
            {
                "id": "conn_1",
                "type": "connector",
                "x": 300,
                "y": 130,
                "width": 200,
                "height": 10,
                "from": "rect_1",
                "to": "rect_2",
                "text": "Flow",
                "start_arrow": "none",
                "end_arrow": "arrow",
                "line_style": "solid",
                "line_width": 2,
            },
        ],
    }

    out_path = tmp_path / "diagram_conn.vsdx"
    build_visio_diagram(diagram_data, out_path)

    assert validate_vsdx(out_path) == []
    summary = inspect_vsdx(out_path)
    assert summary["texts"] == 3

    shapes = _shapes(out_path)
    texts = []
    for shape in shapes:
        text_elem = shape.find(f"{{{VISIO_NS}}}Text")
        if text_elem is not None and text_elem.text:
            texts.append(text_elem.text)
    assert "Start" in texts
    assert "End" in texts
    assert "Flow" in texts


def test_build_visio_diagram_various_shapes(tmp_path: Path):
    _make_png(tmp_path / "dummy.png")

    diagram_data = {
        "page": {"width": 1000, "height": 800},
        "shapes": [
            {"id": "r1", "type": "rectangle", "x": 50, "y": 50, "width": 150, "height": 80, "text": "Rect"},
            {"id": "r2", "type": "rounded_rectangle", "x": 50, "y": 200, "width": 150, "height": 80, "text": "Rounded"},
            {"id": "e1", "type": "ellipse", "x": 300, "y": 50, "width": 150, "height": 80, "text": "Ellipse"},
            {"id": "d1", "type": "diamond", "x": 300, "y": 200, "width": 120, "height": 120, "text": "Diamond"},
            {"id": "t1", "type": "triangle", "x": 550, "y": 50, "width": 120, "height": 120, "text": "Triangle"},
            {"id": "h1", "type": "hexagon", "x": 550, "y": 200, "width": 120, "height": 120, "text": "Hexagon"},
            {"id": "tx1", "type": "text", "x": 50, "y": 350, "width": 300, "height": 60, "text": "Free text"},
            {"id": "l1", "type": "line", "x": 50, "y": 450, "width": 400, "height": 2, "text": ""},
        ],
    }

    out_path = tmp_path / "diagram_shapes.vsdx"
    build_visio_diagram(diagram_data, out_path)

    assert validate_vsdx(out_path) == []
    summary = inspect_vsdx(out_path)
    assert summary["texts"] == 8


def test_diagram_page_size_scaling(tmp_path: Path):
    _make_png(tmp_path / "dummy.png")

    diagram_data = {
        "page": {"width": 1000, "height": 1000},
        "shapes": [
            {
                "id": "rect_1",
                "type": "rectangle",
                "x": 0,
                "y": 0,
                "width": 1000,
                "height": 1000,
                "text": "Full Page",
            },
        ],
    }

    out_path = tmp_path / "diagram_full.vsdx"
    build_visio_diagram(diagram_data, out_path, dpi=100)

    summary = inspect_vsdx(out_path)
    page_width, page_height = summary["sizes"][0]
    assert abs(page_width - 10.0) < 0.1
    assert abs(page_height - 10.0) < 0.1

    shapes = _shapes(out_path)
    shape = shapes[0]
    cells = {cell.get("N"): cell.get("V") for cell in shape.findall(f"{{{VISIO_NS}}}Cell")}
    shape_width = float(cells["Width"])
    shape_height = float(cells["Height"])
    assert abs(shape_width - page_width) < 0.1
    assert abs(shape_height - page_height) < 0.1


def test_diagram_empty_shapes(tmp_path: Path):
    _make_png(tmp_path / "dummy.png")

    diagram_data = {
        "page": {"width": 1000, "height": 800},
        "shapes": [],
    }

    out_path = tmp_path / "diagram_empty.vsdx"
    build_visio_diagram(diagram_data, out_path)

    # Validator warns about empty page but file is structurally valid
    problems = validate_vsdx(out_path)
    assert any("no shapes" in p for p in problems)
    summary = inspect_vsdx(out_path)
    assert summary["pages"] == 1
    assert summary["texts"] == 0


def test_diagram_rotation(tmp_path: Path):
    _make_png(tmp_path / "dummy.png")

    diagram_data = {
        "page": {"width": 1000, "height": 800},
        "shapes": [
            {
                "id": "rect_1",
                "type": "rectangle",
                "x": 200,
                "y": 200,
                "width": 200,
                "height": 100,
                "text": "Rotated",
                "rotation": 45,
            },
        ],
    }

    out_path = tmp_path / "diagram_rotated.vsdx"
    build_visio_diagram(diagram_data, out_path)

    assert validate_vsdx(out_path) == []
    summary = inspect_vsdx(out_path)
    assert summary["texts"] == 1


def test_build_visio_diagram_dpi_scaling(tmp_path: Path):
    _make_png(tmp_path / "dummy.png")

    diagram_data = {
        "page": {"width": 1000, "height": 1000},
        "shapes": [
            {
                "id": "rect_1",
                "type": "rectangle",
                "x": 250,
                "y": 250,
                "width": 500,
                "height": 500,
                "text": "Center",
            },
        ],
    }

    out_path = tmp_path / "diagram_dpi.vsdx"
    build_visio_diagram(diagram_data, out_path, dpi=200)

    summary = inspect_vsdx(out_path)
    page_width, page_height = summary["sizes"][0]
    assert abs(page_width - 5.0) < 0.1
    assert abs(page_height - 5.0) < 0.1