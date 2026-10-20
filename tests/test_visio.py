"""Visio exporter tests: PNG pages -> .vsdx package."""

from __future__ import annotations

import asyncio
import sys
import zipfile
from pathlib import Path

import pytest

from idp.visio_cli import collect_inputs, parse_page_size
from idp.visio_exporter import (
    VISIO_NS,
    build_bundle_vsdx,
    build_vsdx,
    inspect_vsdx,
    validate_vsdx,
)
from idp.worker import Worker


def _make_png(path: Path, width: int = 200, height: int = 300, color: str = "#3366cc") -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color).save(path, "PNG")
    return path


def _names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def test_build_vsdx_contains_pages_and_media(tmp_path):
    images = [_make_png(tmp_path / "page_00001.png"), _make_png(tmp_path / "page_00002.png")]
    out = build_vsdx(images, tmp_path / "drawing.vsdx")

    assert out.is_file()
    names = _names(out)
    assert "[Content_Types].xml" in names
    assert "visio/document.xml" in names
    assert "visio/pages/pages.xml" in names
    assert "visio/pages/page1.xml" in names
    assert "visio/pages/page2.xml" in names
    assert "visio/media/image1.png" in names
    assert "visio/media/image2.png" in names

    summary = inspect_vsdx(out)
    assert summary["pages"] == 2
    assert summary["images"] == 2
    assert all(width and height for width, height in summary["sizes"])


def test_embedded_images_keep_original_bytes(tmp_path):
    images = [_make_png(tmp_path / "a.png", 120, 80)]
    out = build_vsdx(images, tmp_path / "drawing.vsdx")

    with zipfile.ZipFile(out) as archive:
        assert archive.read("visio/media/image1.png") == images[0].read_bytes()


def test_validate_reports_no_problems(tmp_path):
    images = [_make_png(tmp_path / "page.png")]
    out = build_vsdx(images, tmp_path / "drawing.vsdx")
    assert validate_vsdx(out) == []


def test_validate_detects_broken_drawing(tmp_path):
    images = [_make_png(tmp_path / "page.png")]
    out = build_vsdx(images, tmp_path / "drawing.vsdx")
    broken = tmp_path / "broken.vsdx"
    with zipfile.ZipFile(out) as source, zipfile.ZipFile(broken, "w") as target:
        for item in source.namelist():
            data = source.read(item)
            if item == "visio/document.xml":
                data = data.replace(
                    b'<ColorEntry IX="24" RGB="#7F7F7F"/>', b""
                ).replace(b'<ColorEntry IX="25" RGB="#FFFFFF"/>', b"")
            target.writestr(item, data)
    problems = validate_vsdx(broken)
    assert any("ColorEntry" in problem for problem in problems), problems


def test_drawing_matches_official_visio_schema(tmp_path):
    """XSD check against VisioSchema15 (tests/schemas/visio.xsd)."""
    pytest.importorskip("lxml")
    from lxml import etree

    schema_path = Path(__file__).parent / "schemas" / "visio.xsd"
    schema = etree.XMLSchema(etree.parse(str(schema_path)))

    images = [_make_png(tmp_path / "page_00001.png"), _make_png(tmp_path / "page_00002.png")]
    out = build_vsdx(images, tmp_path / "drawing.vsdx")

    checked = 0
    with zipfile.ZipFile(out) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue
            tree = etree.fromstring(archive.read(name))
            if etree.QName(tree).namespace != VISIO_NS:
                continue
            assert schema.validate(tree), f"{name}: {schema.error_log}"
            checked += 1
    assert checked >= 4, "document, pages, page parts and windows must be validated"


def test_page_size_follows_dpi(tmp_path):
    images = [_make_png(tmp_path / "page.png", 200, 400)]
    out = build_vsdx(images, tmp_path / "drawing.vsdx", dpi=100)
    summary = inspect_vsdx(out)
    width, height = summary["sizes"][0]
    assert abs(width - 2.0) < 1e-6
    assert abs(height - 4.0) < 1e-6


def test_fixed_page_size_keeps_aspect_ratio(tmp_path):
    images = [_make_png(tmp_path / "page.png", 200, 400)]
    out = build_vsdx(images, tmp_path / "drawing.vsdx", dpi=100, page_size=(8.27, 11.69))
    summary = inspect_vsdx(out)
    width, height = summary["sizes"][0]
    assert abs(width - 8.27) < 1e-6
    assert abs(height - 11.69) < 1e-6

    from xml.etree import ElementTree as ET

    with zipfile.ZipFile(out) as archive:
        page = ET.fromstring(archive.read("visio/pages/page1.xml"))
    shape = page.find(f".//{{{VISIO_NS}}}Shape")
    cells = {cell.get("N"): cell.get("V") for cell in shape.findall(f"{{{VISIO_NS}}}Cell")}
    shape_width = float(cells["Width"])
    shape_height = float(cells["Height"])
    assert abs(shape_width / shape_height - 0.5) < 1e-3, "picture keeps its aspect ratio"


def test_sheet_mode_tiles_images_on_one_page(tmp_path):
    images = [
        _make_png(tmp_path / f"page_{index}.png") for index in range(1, 5)
    ]
    out = build_vsdx(images, tmp_path / "sheet.vsdx", mode="sheet", columns=2)
    summary = inspect_vsdx(out)
    assert summary["pages"] == 1
    assert summary["images"] == 4

    from xml.etree import ElementTree as ET

    with zipfile.ZipFile(out) as archive:
        page = ET.fromstring(archive.read("visio/pages/page1.xml"))
    shapes = page.findall(f".//{{{VISIO_NS}}}Shape")
    assert len(shapes) == 4


def test_bundle_export_uses_page_renders(tmp_path):
    images_dir = tmp_path / "bundle" / "images"
    _make_png(images_dir / "page_00001.png", 100, 100)
    _make_png(images_dir / "page_00002.png", 100, 100)
    _make_png(images_dir / "figure_0001.png", 50, 50)
    (tmp_path / "bundle" / "text.txt").write_text("text", encoding="utf-8")

    out = build_bundle_vsdx(tmp_path / "bundle")
    assert out.name == "reconstruct.vsdx"
    summary = inspect_vsdx(out)
    assert summary["pages"] == 2, "only page renders become Visio pages"


def test_bundle_without_pages_falls_back_to_embedded_images(tmp_path):
    images_dir = tmp_path / "bundle" / "images"
    _make_png(images_dir / "docx_image_0001.png", 100, 100)
    out = build_bundle_vsdx(tmp_path / "bundle")
    assert inspect_vsdx(out)["pages"] == 1


def test_cli_converts_directory_of_pngs(tmp_path, monkeypatch, capsys):
    from idp import visio_cli

    _make_png(tmp_path / "pages" / "page_00001.png")
    _make_png(tmp_path / "pages" / "page_00002.png")
    out = tmp_path / "drawing.vsdx"
    monkeypatch.setattr(
        sys, "argv",
        ["idp-visio", "--out", str(out), str(tmp_path / "pages")],
    )
    visio_cli.main()
    captured = capsys.readouterr()
    assert "structure check: ok" in captured.out
    assert inspect_vsdx(out)["pages"] == 2


def test_collect_inputs_expands_directories(tmp_path):
    _make_png(tmp_path / "dir" / "a.png")
    _make_png(tmp_path / "dir" / "nested" / "b.png")
    found = collect_inputs([str(tmp_path / "dir")], "*.png")
    assert len(found) == 2


def test_parse_page_size():
    assert parse_page_size("8.27x11.69") == (8.27, 11.69)
    assert parse_page_size("") is None


def test_worker_visio_flag_builds_drawing(workspace, sample_pdf, stub_server):
    worker = Worker(artifacts_mode=True, visio_mode=True)
    asyncio.run(worker.run())

    bundle = workspace.output / "txts" / "report.pdf"
    vsdx = bundle / "reconstruct.vsdx"
    assert vsdx.is_file()
    assert validate_vsdx(vsdx) == []
    assert inspect_vsdx(vsdx)["pages"] == 3
