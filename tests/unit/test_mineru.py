import io
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from idp.domain.models import ArtifactReference
from idp.domain.states import ArtifactRetention
from idp.services.mineru import CommandMinerURunner, LayoutAdapter
from idp.services.vision import (
    ImageQualityGate,
    PageTransform,
    RenderLimits,
    RenderedPage,
    VisionPreparation,
)
from idp.storage import LocalArtifactStore


def _png() -> bytes:
    image = Image.new("RGB", (300, 300), color=(180, 180, 180))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakeRenderer:
    def render(self, pdf_path: Path, limits: RenderLimits):
        return (
            RenderedPage(
                transform=PageTransform(
                    page_number=1,
                    pdf_width_points=300,
                    pdf_height_points=300,
                    render_width_pixels=300,
                    render_height_pixels=300,
                    dpi=72,
                    image_scale_x=1,
                    image_scale_y=1,
                ),
                png=_png(),
            ),
        )


def _vision(tmp_path: Path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    preparation = VisionPreparation(
        renderer=FakeRenderer(),
        artifacts=store,
        quality_gate=ImageQualityGate(0.12, 0.01),
    )
    manifest, _ = preparation.prepare(
        pdf_path=tmp_path / "unused.pdf",
        source_sha256="a" * 64,
        artifact_prefix="run",
        limits=RenderLimits(72, 1, 100_000, 100_000),
    )
    return store, manifest


def test_layout_adapter_retains_all_block_types_without_vendor_text(tmp_path: Path) -> None:
    store, vision = _vision(tmp_path)
    raw = ArtifactReference(
        object_key="run/mineru/middle.json",
        sha256="b" * 64,
        media_type="application/json",
    )
    middle = {
        "pdf_info": [
            {
                "page_no": 1,
                "para_blocks": [
                    {"type": "text", "bbox": [0, 0, 30, 30], "text": "must not leak"},
                    {"type": "table", "bbox": [40, 0, 80, 30], "html": "<table>"},
                    {"type": "image", "bbox": [90, 0, 130, 30], "caption": "picture"},
                    {"type": "chart", "bbox": [140, 0, 180, 30], "content": "chart data"},
                    {"type": "formula", "bbox": [190, 0, 230, 30], "latex": "x^2"},
                    {"type": "header", "bbox": [0, 40, 30, 60]},
                    {"type": "footer", "bbox": [40, 40, 70, 60]},
                    {"type": "stamp", "bbox": [80, 40, 110, 60]},
                    {"type": "signature", "bbox": [120, 40, 150, 60]},
                    {"type": "future_widget", "bbox": [160, 40, 190, 60], "text": "future"},
                ],
            }
        ]
    }

    layout, crops = LayoutAdapter().normalize(
        middle_json=middle,
        vision=vision,
        raw_reference=raw,
        artifacts=store,
        artifact_prefix="run",
    )

    assert [block.kind for block in layout.blocks] == [
        "text",
        "table",
        "image",
        "chart",
        "formula",
        "header",
        "footer",
        "stamp",
        "signature",
        "future_widget",
    ]
    assert len(crops) == len(layout.blocks)
    assert all(store.exists(crop.reference) for crop in crops)
    serialized = str(layout)
    assert "must not leak" not in serialized
    assert "chart data" not in serialized
    assert "x^2" not in serialized


def test_nested_blocks_preserve_parent_internal_block_id(tmp_path: Path) -> None:
    store, vision = _vision(tmp_path)
    raw = ArtifactReference(
        object_key="run/mineru/middle.json",
        sha256="b" * 64,
        media_type="application/json",
    )
    middle = {
        "pages": [
            {
                "page_number": 1,
                "blocks": [
                    {
                        "type": "table",
                        "bbox": [0, 0, 100, 100],
                        "children": [{"type": "table_cell", "bbox": [10, 10, 50, 50]}],
                    }
                ],
            }
        ]
    }

    layout, _ = LayoutAdapter().normalize(
        middle_json=middle,
        vision=vision,
        raw_reference=raw,
        artifacts=store,
        artifact_prefix="run",
    )

    assert len(layout.blocks) == 2
    assert layout.blocks[1].parent_block_id == layout.blocks[0].block_id
    assert layout.blocks[1].vendor_path.endswith("children[0]")


def test_command_runner_creates_pdf_and_normalizes_mineru_middle_json(
    tmp_path: Path, monkeypatch
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    first = pages / "first.png"
    second = pages / "second.png"
    first.write_bytes(_png())
    second.write_bytes(_png())
    output = tmp_path / "output"
    completed = Mock()

    def fake_run(command, **kwargs) -> None:
        assert command == [
            "magic-pdf",
            "--path",
            str(output / "source.pdf"),
            "--output-dir",
            str(output),
            "--method",
            "ocr",
        ]
        assert (output / "source.pdf").is_file()
        generated = output / "source"
        generated.mkdir()
        (generated / "source_middle.json").write_text('{"pdf_info": []}', encoding="utf-8")
        return completed

    monkeypatch.setattr("idp.services.mineru.subprocess.run", fake_run)

    result = CommandMinerURunner(
        ("magic-pdf", "--path", "{input}", "--output-dir", "{output}", "--method", "ocr")
    ).run({2: second, 1: first}, output)

    assert result == output / "middle.json"
    assert result.read_text(encoding="utf-8") == '{"pdf_info": []}'
