"""Visio exporter tests: PNG pages -> .vsdx package."""

from __future__ import annotations

import asyncio
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from idp.visio_cli import collect_inputs, parse_page_size
from idp.visio_document import build_document_vsdx
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


def _shapes(path: Path, part: str = "visio/pages/page1.xml") -> list[ET.Element]:
    with zipfile.ZipFile(path) as archive:
        page = ET.fromstring(archive.read(part))
    return page.findall(f".//{{{VISIO_NS}}}Shape")


def _cells(shape: ET.Element) -> dict[str, str]:
    return {cell.get("N"): cell.get("V") for cell in shape.findall(f"{{{VISIO_NS}}}Cell")}


def _make_bundle(root: Path, text: str) -> Path:
    """Bundle with a text file and one picture linked from it."""
    bundle = root / "bundle"
    (bundle / "images").mkdir(parents=True, exist_ok=True)
    (bundle / "text.txt").write_text(text, encoding="utf-8")
    _make_png(bundle / "images" / "figure_0001.png", 300, 150, "#22aa55")
    return bundle


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


def test_every_page_uses_its_own_picture(tmp_path):
    """Regression: the pages must not all point at the first media file."""
    images = [
        _make_png(tmp_path / f"page_{index:05d}.png", 100, 100, color)
        for index, color in enumerate(["#ff0000", "#00ff00", "#0000ff"], start=1)
    ]
    out = build_vsdx(images, tmp_path / "pages.vsdx")

    with zipfile.ZipFile(out) as archive:
        targets = []
        for index in range(1, len(images) + 1):
            rels = archive.read(f"visio/pages/_rels/page{index}.xml.rels").decode()
            targets.append(rels.split('Target="../media/')[1].split('"')[0])
    assert targets == ["image1.png", "image2.png", "image3.png"]
    assert validate_vsdx(out) == []


def test_validator_reports_a_reused_picture(tmp_path):
    images = [_make_png(tmp_path / f"page_{index:05d}.png") for index in range(1, 3)]
    out = build_vsdx(images, tmp_path / "pages.vsdx")
    broken = tmp_path / "broken.vsdx"
    with zipfile.ZipFile(out) as source, zipfile.ZipFile(broken, "w") as target:
        for item in source.namelist():
            data = source.read(item)
            if item == "visio/pages/_rels/page2.xml.rels":
                data = data.replace(b"image2.png", b"image1.png")
            target.writestr(item, data)
    problems = validate_vsdx(broken)
    assert any("media file" in problem for problem in problems), problems


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


def test_worker_visio_content_document(workspace, sample_pdf, stub_server):
    worker = Worker(artifacts_mode=True, visio_mode=True, visio_content="document")
    asyncio.run(worker.run())

    bundle = workspace.output / "txts" / "report.pdf"
    vsdx = bundle / "reconstruct.vsdx"
    assert vsdx.is_file()
    assert validate_vsdx(vsdx) == []
    summary = inspect_vsdx(vsdx)
    assert summary["texts"] > 0, "the drawing holds the text of the document"


def test_worker_rebuilds_a_scan_as_an_editable_drawing(workspace, stub_server):
    """A PNG in the input root becomes a Visio drawing with editable text."""
    _make_png(workspace.input / "scan.png", 600, 800)
    worker = Worker(artifacts_mode=True, visio_mode=True)
    asyncio.run(worker.run())

    bundle = workspace.output / "txts" / "scan.png"
    assert (bundle / "text.txt").is_file()
    assert (bundle / "images" / "page_00001.png").is_file()

    vsdx = bundle / "reconstruct.vsdx"
    assert vsdx.is_file()
    assert validate_vsdx(vsdx) == []
    summary = inspect_vsdx(vsdx)
    assert summary["texts"] > 0, "the scan is rebuilt as text, not as a picture"
    shapes = _shapes(vsdx)
    texts = [
        (shape.find(f"{{{VISIO_NS}}}Text").text or "")
        for shape in shapes
        if shape.get("Type") == "Shape" and shape.find(f"{{{VISIO_NS}}}Text") is not None
    ]
    assert any("Страница" in text for text in texts)


def test_worker_layout_mode_uses_the_vlm_json_layout(workspace, stub_server):
    """--visio-layout makes the VLM return a structured layout instead of Markdown."""
    _make_png(workspace.input / "scan.png", 600, 800)
    worker = Worker(artifacts_mode=True, visio_mode=True, visio_layout=True)
    asyncio.run(worker.run())

    bundle = workspace.output / "txts" / "scan.png"
    assert (bundle / "layout.json").is_file()
    assert "blocks" in (bundle / "layout.json").read_text(encoding="utf-8")

    vsdx = bundle / "reconstruct.vsdx"
    assert vsdx.is_file()
    assert validate_vsdx(vsdx) == []
    summary = inspect_vsdx(vsdx)
    assert summary["texts"] > 0, "the layout becomes editable shapes"


DOCUMENT_TEXT = """<!-- page 1 -->
## Отчёт по объекту

Сводные данные приведены в таблице ниже.

- пункт один;
- пункт два.

| Параметр | Значение |
| --- | --- |
| Масса | 12.5 |
| Длина | 3400 |

![chart: график](images/figure_0001.png)
"""


def _document_blocks(text: str):
    from idp.reconstructor import parse_bundle_text

    return parse_bundle_text(text)


def test_document_mode_creates_editable_shapes(tmp_path):
    bundle = _make_bundle(tmp_path, DOCUMENT_TEXT)
    out = build_document_vsdx(
        _document_blocks(DOCUMENT_TEXT),
        tmp_path / "document.vsdx",
        base_dir=bundle,
    )

    assert validate_vsdx(out) == []
    summary = inspect_vsdx(out)
    assert summary["pages"] >= 1
    assert summary["texts"] > 0, "the text of the document stays editable"
    assert summary["images"] == 1, "the picture of the document is embedded"

    shapes = _shapes(out)
    texts = [
        (shape.find(f"{{{VISIO_NS}}}Text").text or "")
        for shape in shapes
        if shape.get("Type") == "Shape" and shape.find(f"{{{VISIO_NS}}}Text") is not None
    ]
    assert any("Отчёт по объекту" in text for text in texts)
    assert any(text.startswith("• пункт один") for text in texts)
    assert "Масса" in texts and "12.5" in texts


def test_document_mode_headings_are_bold(tmp_path):
    bundle = _make_bundle(tmp_path, DOCUMENT_TEXT)
    out = build_document_vsdx(
        _document_blocks(DOCUMENT_TEXT),
        tmp_path / "document.vsdx",
        base_dir=bundle,
    )
    shapes = _shapes(out)
    heading = next(
        shape
        for shape in shapes
        if shape.get("Name", "").startswith("Heading")
    )
    character = heading.find(f"{{{VISIO_NS}}}Section[@N='Character']")
    style = character.find(f"{{{VISIO_NS}}}Cell[@N='Style']")
    assert style is not None
    assert int(style.get("V", "0")) & 1, "the heading is bold"


def test_document_mode_keeps_the_table_inside_the_page(tmp_path):
    bundle = _make_bundle(tmp_path, DOCUMENT_TEXT)
    out = build_document_vsdx(
        _document_blocks(DOCUMENT_TEXT),
        tmp_path / "document.vsdx",
        base_dir=bundle,
    )
    summary = inspect_vsdx(out)
    page_width, page_height = summary["sizes"][0]
    for shape in _shapes(out):
        cells = _cells(shape)
        left = float(cells["PinX"]) - float(cells["Width"]) / 2
        bottom = float(cells["PinY"]) - float(cells["Height"]) / 2
        assert left >= -0.01, "the shape starts at the left edge of the page"
        assert bottom >= -0.01, "the shape starts above the bottom edge"
        assert left + float(cells["Width"]) <= page_width + 0.01
        assert bottom + float(cells["Height"]) <= page_height + 0.01


def test_document_mode_splits_long_text_over_pages(tmp_path):
    text = "\n\n".join(f"Абзац номер {index} " + "слово " * 200 for index in range(6))
    bundle = _make_bundle(tmp_path, text)
    out = build_document_vsdx(
        _document_blocks(text),
        tmp_path / "document.vsdx",
        base_dir=bundle,
    )
    assert inspect_vsdx(out)["pages"] > 1


def test_document_drawing_matches_the_official_schema(tmp_path):
    pytest.importorskip("lxml")
    from lxml import etree

    schema_path = Path(__file__).parent / "schemas" / "visio.xsd"
    schema = etree.XMLSchema(etree.parse(str(schema_path)))

    bundle = _make_bundle(tmp_path, DOCUMENT_TEXT)
    out = build_document_vsdx(
        _document_blocks(DOCUMENT_TEXT),
        tmp_path / "document.vsdx",
        base_dir=bundle,
    )

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
    assert checked >= 4


def test_cli_document_mode_converts_a_bundle(tmp_path, monkeypatch, capsys):
    from idp import visio_cli

    bundle = _make_bundle(tmp_path, DOCUMENT_TEXT)
    out = tmp_path / "document.vsdx"
    monkeypatch.setattr(
        sys, "argv",
        ["idp-visio", "--bundle", str(bundle), "--mode", "document", "--out", str(out)],
    )
    visio_cli.main()
    captured = capsys.readouterr()
    assert "structure check: ok" in captured.out
    assert out.is_file()
    assert inspect_vsdx(out)["texts"] > 0


def test_cli_document_mode_reads_a_text_file(tmp_path, monkeypatch, capsys):
    from idp import visio_cli

    bundle = _make_bundle(tmp_path, DOCUMENT_TEXT)
    text_file = bundle / "text.txt"
    out = tmp_path / "document.vsdx"
    monkeypatch.setattr(
        sys, "argv",
        ["idp-visio", "--text", str(text_file), "--out", str(out)],
    )
    visio_cli.main()
    captured = capsys.readouterr()
    assert "structure check: ok" in captured.out
    assert out.is_file()


def test_reconstructor_builds_a_document_drawing(tmp_path, monkeypatch):
    from idp.config import settings
    from idp.reconstructor import reconstruct_bundle

    bundle = _make_bundle(tmp_path, DOCUMENT_TEXT)
    previous = settings.visio_content
    settings.visio_content = "document"
    try:
        created = reconstruct_bundle(bundle, ["vsdx"])
    finally:
        settings.visio_content = previous

    assert created, "the drawing must be created"
    out = created[0]
    assert out.name == "reconstruct.vsdx"
    assert validate_vsdx(out) == []
    assert inspect_vsdx(out)["texts"] > 0
