import io
from pathlib import Path

from PIL import Image

from idp.domain.states import ArtifactRetention
from idp.services.vision import (
    ImageQualityGate,
    PageTransform,
    RenderLimits,
    VisionPreparation,
)
from idp.storage import LocalArtifactStore


def _png(width: int, height: int, color: int) -> bytes:
    image = Image.new("L", (width, height), color=color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class ReturningUpscaler:
    def __init__(self, output: bytes) -> None:
        self._output = output

    def upscale(self, image_png: bytes) -> bytes:
        return self._output


def test_page_transform_round_trips_pdf_coordinates() -> None:
    transform = PageTransform(
        page_number=1,
        pdf_width_points=612,
        pdf_height_points=792,
        render_width_pixels=1700,
        render_height_pixels=2200,
        dpi=200,
        image_scale_x=1700 / 612,
        image_scale_y=2200 / 792,
    )

    rendered = transform.pdf_to_image(100, 200)

    assert transform.image_to_pdf(*rendered) == (100, 200)


def test_quality_gate_falls_back_when_upscale_is_smaller() -> None:
    original = _png(100, 100, 120)
    smaller = _png(50, 50, 120)

    decision = ImageQualityGate(0.12, 0.01).select(original, smaller)

    assert decision.selected == "render"
    assert decision.reason == "upscale_dimensions_smaller"


def test_quality_gate_falls_back_when_upscale_clipping_increases() -> None:
    original = _png(100, 100, 120)
    clipped = _png(200, 200, 255)

    decision = ImageQualityGate(0.12, 0.01).select(original, clipped)

    assert decision.selected == "render"
    assert decision.reason == "upscale_clipping_increased"


def test_preparation_records_selected_upscaled_transform(tmp_path: Path) -> None:
    original = _png(100, 100, 120)
    enhanced = _png(200, 200, 120)
    store = LocalArtifactStore(tmp_path / "artifacts")
    preparation = VisionPreparation(
        renderer=FakeRenderer(original),
        artifacts=store,
        quality_gate=ImageQualityGate(0.12, 0.01),
        upscaler=ReturningUpscaler(enhanced),
    )

    manifest, manifest_artifact = preparation.prepare(
        pdf_path=tmp_path / "ignored.pdf",
        source_sha256="a" * 64,
        artifact_prefix="runs/one",
        limits=RenderLimits(200, 1, 100_000, 100_000),
    )

    page = manifest.pages[0]
    assert page.decision.selected == "enhanced"
    assert page.selected.reference.object_key.endswith("upscaled.png")
    assert page.selected_transform.render_width_pixels == 200
    assert store.exists(manifest_artifact.reference)
    assert page.render.retention == ArtifactRetention.TEMPORARY


class FakeRenderer:
    def __init__(self, png: bytes) -> None:
        self._png = png

    def render(self, pdf_path: Path, limits: RenderLimits):
        from idp.services.vision import RenderedPage

        return (
            RenderedPage(
                transform=PageTransform(
                    page_number=1,
                    pdf_width_points=72,
                    pdf_height_points=72,
                    render_width_pixels=100,
                    render_height_pixels=100,
                    dpi=100,
                    image_scale_x=100 / 72,
                    image_scale_y=100 / 72,
                ),
                png=self._png,
            ),
        )
