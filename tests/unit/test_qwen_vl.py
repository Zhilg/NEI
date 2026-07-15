import io
from pathlib import Path

import pytest
from PIL import Image

from idp.domain.models import ArtifactReference
from idp.domain.states import ArtifactRetention
from idp.services.mineru import LayoutBlock, LayoutManifest, LayoutPage
from idp.services.ocr import OcrManifest, OcrToken
from idp.services.qwen_vl import ReconstructionAssembler, ReconstructionError
from idp.services.vision import PageTransform
from idp.storage import LocalArtifactStore


def _png() -> bytes:
    image = Image.new("RGB", (100, 100), color=(180, 180, 180))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.contexts = []

    def reconstruct(self, context):
        self.contexts.append(context)
        return self.responses.pop(0)


def _documents(store: LocalArtifactStore, block_count: int = 2):
    page = store.put_bytes(
        object_key="page.png", payload=_png(), media_type="image/png", retention=ArtifactRetention.TEMPORARY
    )
    blocks = []
    for index in range(block_count):
        crop = store.put_bytes(
            object_key=f"crop-{index}.png",
            payload=_png(),
            media_type="image/png",
            retention=ArtifactRetention.TEMPORARY,
        )
        blocks.append(
            LayoutBlock(
                block_id=f"b{index}",
                page_number=1,
                kind="text" if index == 0 else "table",
                bbox=(index * 10.0, 0.0, index * 10.0 + 10.0, 10.0),
                reading_order=index,
                parent_block_id=None,
                relations=(),
                crop=crop.reference,
                vendor_path=f"$.blocks[{index}]",
                attributes={},
            )
        )
    layout_reference = ArtifactReference("layout.json", "a" * 64, "application/json")
    ocr_reference = ArtifactReference("ocr.json", "b" * 64, "application/json")
    layout = LayoutManifest(
        schema_version="layout-manifest-v1",
        source_sha256="c" * 64,
        raw_mineru=ArtifactReference("raw.json", "d" * 64, "application/json"),
        pages=(LayoutPage(1, page.reference, PageTransform(1, 100, 100, 100, 100, 72, 1, 1)),),
        blocks=tuple(blocks),
    )
    token = OcrToken(
        token_id="t0",
        block_id="b0",
        page_number=1,
        bbox=(1, 1, 3, 3),
        raw_text="ошибкa",
        normalized_text="ошибкa",
        confidence=0.5,
        detector_confidence=0.9,
        script="Cyrillic",
        language="ru",
        model_id="eslav",
        model_revision="v5",
        line_crop=blocks[0].crop,
    )
    ocr = OcrManifest("ocr-manifest-v1", "c" * 64, layout_reference, (token,), ())
    return layout, layout_reference, ocr, ocr_reference


def _assembler(store, client, max_blocks=10, max_images=20):
    return ReconstructionAssembler(
        client=client,
        artifacts=store,
        model_id="Qwen2.5-VL-32B-Instruct",
        model_revision="pinned",
        prompt_version="reconstruction-v1",
        max_blocks_per_request=max_blocks,
        max_images_per_request=max_images,
    )


def test_reconstruction_preserves_layout_order_and_validates_correction(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    layout, layout_ref, ocr, ocr_ref = _documents(store)
    client = RecordingClient(
        [
            {
                "blocks": [
                    {
                        "block_id": "b0",
                        "page_number": 1,
                        "bbox": [0.0, 0.0, 10.0, 10.0],
                        "markdown": "Исправленный текст",
                        "evidence": "visible b0 crop",
                        "corrections": [
                            {
                                "token_id": "t0",
                                "original_text": "ошибкa",
                                "corrected_text": "ошибка",
                                "evidence": "visible word in b0 image",
                            }
                        ],
                    },
                    {
                        "block_id": "b1",
                        "page_number": 1,
                        "bbox": [10.0, 0.0, 20.0, 10.0],
                        "markdown": "| A | B |\n|---|---|\n| 1 | 2 |",
                        "evidence": "visible b1 crop",
                        "corrections": [],
                    },
                ],
                "findings": [
                    {
                        "code": "ocr_disagreement",
                        "severity": "warning",
                        "detail": "one token corrected",
                        "block_ids": ["b0"],
                        "evidence": "b0 image",
                    }
                ],
            }
        ]
    )

    result = _assembler(store, client).reconstruct(
        layout=layout, layout_reference=layout_ref, ocr=ocr, ocr_reference=ocr_ref
    )

    assert [block.block_id for block in result.blocks] == ["b0", "b1"]
    assert result.blocks[0].corrections[0].corrected_text == "ошибка"
    assert "Исправленный текст" in result.markdown
    assert result.findings[0].block_ids == ("b0",)
    assert len(client.contexts[0]["images"]) == 3
    assert client.contexts[0]["ocr_tokens"][0]["token_id"] == "t0"
    assert client.contexts[0]["page_transforms"][0]["page_number"] == 1


def test_reconstruction_rejects_missing_or_reordered_blocks(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    layout, layout_ref, ocr, ocr_ref = _documents(store)
    client = RecordingClient(
        [
            {
                "blocks": [
                    {
                        "block_id": "b1",
                        "page_number": 1,
                        "bbox": [10.0, 0.0, 20.0, 10.0],
                        "markdown": "wrong order",
                        "evidence": "visible b1 crop",
                        "corrections": [],
                    }
                ],
                "findings": [],
            }
        ]
    )

    with pytest.raises(ReconstructionError, match="complete MinerU block order"):
        _assembler(store, client).reconstruct(
            layout=layout, layout_reference=layout_ref, ocr=ocr, ocr_reference=ocr_ref
        )


def test_reconstruction_rejects_finding_without_grounded_block(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    layout, layout_ref, ocr, ocr_ref = _documents(store)
    client = RecordingClient(
        [
            {
                "blocks": [
                    {
                        "block_id": "b0",
                        "page_number": 1,
                        "bbox": [0.0, 0.0, 10.0, 10.0],
                        "markdown": "first",
                        "evidence": "visible",
                        "corrections": [],
                    },
                    {
                        "block_id": "b1",
                        "page_number": 1,
                        "bbox": [10.0, 0.0, 20.0, 10.0],
                        "markdown": "second",
                        "evidence": "visible",
                        "corrections": [],
                    },
                ],
                "findings": [
                    {
                        "code": "unreadable",
                        "severity": "warning",
                        "detail": "region unclear",
                        "block_ids": [],
                        "evidence": "visible blur",
                    }
                ],
            }
        ]
    )

    with pytest.raises(ReconstructionError, match="one or more known block IDs"):
        _assembler(store, client).reconstruct(
            layout=layout, layout_reference=layout_ref, ocr=ocr, ocr_reference=ocr_ref
        )


def test_reconstruction_chunks_page_deterministically_with_image_budget(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    layout, layout_ref, ocr, ocr_ref = _documents(store, block_count=3)
    responses = []
    for block_id, bbox in (("b0", [0.0, 0.0, 10.0, 10.0]), ("b1", [10.0, 0.0, 20.0, 10.0]), ("b2", [20.0, 0.0, 30.0, 10.0])):
        responses.append(
            {
                "blocks": [
                    {
                        "block_id": block_id,
                        "page_number": 1,
                        "bbox": bbox,
                        "markdown": block_id,
                        "evidence": f"visible {block_id} crop",
                        "corrections": [],
                    }
                ],
                "findings": [],
            }
        )
    client = RecordingClient(responses)

    result = _assembler(store, client, max_blocks=10, max_images=2).reconstruct(
        layout=layout, layout_reference=layout_ref, ocr=ocr, ocr_reference=ocr_ref
    )

    assert [block.block_id for block in result.blocks] == ["b0", "b1", "b2"]
    assert result.markdown == "b0\n\nb1\n\nb2"
    assert all(len(context["images"]) == 2 for context in client.contexts)
