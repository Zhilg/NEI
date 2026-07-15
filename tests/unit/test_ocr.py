import io
from pathlib import Path

from PIL import Image

from idp.domain.models import ArtifactReference
from idp.domain.states import ArtifactRetention
from idp.services.mineru import LayoutBlock, LayoutManifest, LayoutPage
from idp.services.ocr import (
    DetectorLine,
    OcrProcessor,
    OcrRoute,
    RecognizedToken,
    RecognizerProfile,
    ScriptDecision,
)
from idp.services.vision import PageTransform
from idp.storage import LocalArtifactStore


def _page_png() -> bytes:
    image = Image.new("RGB", (200, 100), color=(180, 180, 180))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FixedDetector:
    def __init__(self) -> None:
        self.calls = 0

    def detect(self, block_crop: Path):
        self.calls += 1
        return (DetectorLine((5, 5, 50, 25), 0.91),)


class SequenceRouter:
    def __init__(self, decisions: list[ScriptDecision]) -> None:
        self._decisions = decisions
        self.calls = 0

    def route(self, line_crop: Path) -> ScriptDecision:
        decision = self._decisions[self.calls]
        self.calls += 1
        return decision


class FixedRecognizer:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    def recognize(self, line_crop: Path):
        self.calls += 1
        return (RecognizedToken(self._text, (1, 2, 20, 12), 0.88),)


def _layout(store: LocalArtifactStore) -> tuple[LayoutManifest, ArtifactReference]:
    page = store.put_bytes(
        object_key="layout/pages/00001.png",
        payload=_page_png(),
        media_type="image/png",
        retention=ArtifactRetention.TEMPORARY,
    )
    text_crop = store.put_bytes(
        object_key="layout/blocks/text/crop.png",
        payload=_page_png(),
        media_type="image/png",
        retention=ArtifactRetention.TEMPORARY,
    )
    table_crop = store.put_bytes(
        object_key="layout/blocks/table/crop.png",
        payload=_page_png(),
        media_type="image/png",
        retention=ArtifactRetention.TEMPORARY,
    )
    image_crop = store.put_bytes(
        object_key="layout/blocks/image/crop.png",
        payload=_page_png(),
        media_type="image/png",
        retention=ArtifactRetention.TEMPORARY,
    )
    layout_reference = ArtifactReference(
        object_key="layout/layout_manifest.json",
        sha256="a" * 64,
        media_type="application/json",
    )
    transform = PageTransform(1, 200, 100, 200, 100, 72, 1, 1)
    return (
        LayoutManifest(
            schema_version="layout-manifest-v1",
            source_sha256="b" * 64,
            raw_mineru=ArtifactReference(
                object_key="layout/raw.json", sha256="c" * 64, media_type="application/json"
            ),
            pages=(LayoutPage(1, page.reference, transform),),
            blocks=(
                LayoutBlock("text", 1, "text", (10, 20, 110, 70), 0, None, (), text_crop.reference, "$.text", {}),
                LayoutBlock("table", 1, "table", (0, 0, 100, 100), 1, None, (), table_crop.reference, "$.table", {}),
                LayoutBlock("image", 1, "image", (100, 0, 200, 100), 2, None, (), image_crop.reference, "$.image", {}),
            ),
        ),
        layout_reference,
    )


def _processor(store: LocalArtifactStore, router: SequenceRouter, detector: FixedDetector):
    east = FixedRecognizer("Пример")
    cyrillic = FixedRecognizer("Љубљана")
    latin = FixedRecognizer("Tokyo")
    processor = OcrProcessor(
        detector=detector,
        router=router,
        recognizers={
            OcrRoute.EAST_SLAVIC: (east, RecognizerProfile(OcrRoute.EAST_SLAVIC, "eslav_PP-OCRv5_mobile_rec", "v5")),
            OcrRoute.CYRILLIC: (cyrillic, RecognizerProfile(OcrRoute.CYRILLIC, "cyrillic_PP-OCRv5_mobile_rec", "v5")),
            OcrRoute.LATIN_CJK: (latin, RecognizerProfile(OcrRoute.LATIN_CJK, "PP-OCRv6_medium", "v6")),
        },
        artifacts=store,
        max_lines_per_block=10,
        min_token_confidence=0,
    )
    return processor, east, cyrillic, latin


def test_ocr_only_processes_text_bearing_mineru_blocks(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    layout, layout_reference = _layout(store)
    detector = FixedDetector()
    router = SequenceRouter(
        [ScriptDecision(OcrRoute.EAST_SLAVIC, "Cyrillic", "ru", 0.99)]
    )
    processor, east, cyrillic, latin = _processor(store, router, detector)

    manifest, line_crops = processor.process(
        layout=layout, layout_reference=layout_reference, artifact_prefix="run"
    )

    assert detector.calls == 1
    assert east.calls == 1
    assert cyrillic.calls == 0
    assert latin.calls == 0
    assert len(manifest.tokens) == 1
    token = manifest.tokens[0]
    assert token.block_id == "text"
    assert token.page_number == 1
    assert token.bbox == (16, 27, 35, 37)
    assert token.language == "ru"
    assert token.model_id == "eslav_PP-OCRv5_mobile_rec"
    assert token.model_revision == "v5"
    assert store.exists(token.line_crop)
    assert len(line_crops) == 1


def test_unsupported_script_creates_finding_without_recognizer_call(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    layout, layout_reference = _layout(store)
    detector = FixedDetector()
    router = SequenceRouter(
        [ScriptDecision(OcrRoute.UNSUPPORTED, "Arabic", "und", 0.9, "profile has no Arabic model")]
    )
    processor, east, cyrillic, latin = _processor(store, router, detector)

    manifest, _ = processor.process(
        layout=layout, layout_reference=layout_reference, artifact_prefix="run"
    )

    assert not manifest.tokens
    assert manifest.findings[0].code == "unsupported_script"
    assert east.calls == cyrillic.calls == latin.calls == 0
