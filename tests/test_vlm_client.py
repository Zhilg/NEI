"""VLM client tests: stub transport, retry/rotation, markdown helpers."""

from __future__ import annotations

import asyncio

import pytest

from idp.config import settings
from idp.vlm_client import (
    extract_paragraphs,
    reconstruct_markdown,
    strip_bbox_annotations,
    with_feedback,
)
from tests import fixtures
from tests.vlm_stub import StubServer


@pytest.fixture()
def two_stubs():
    """Two stub endpoints (one flaky) to exercise round-robin rotation."""
    with StubServer(fail_first=1) as flaky, StubServer() as healthy:
        previous = (settings.vl_endpoints, settings.vl_endpoint)
        settings.vl_endpoints = [flaky.endpoint, healthy.endpoint]
        settings.vl_endpoint = flaky.endpoint
        try:
            yield flaky, healthy
        finally:
            settings.vl_endpoints, settings.vl_endpoint = previous


def _run(coro):
    async def _wrapped():
        try:
            return await coro
        finally:
            from idp.vlm_client import close_vlm_client

            await close_vlm_client()

    return asyncio.run(_wrapped())


def test_reconstruct_markdown_from_stub(tmp_path):
    pages = [fixtures.make_png(tmp_path / f"p{index}.png") for index in range(3)]
    with StubServer() as server:
        settings.vl_endpoints = [server.endpoint]
        try:
            markdown = _run(reconstruct_markdown(pages))
        finally:
            settings.vl_endpoints = []
    assert "<!-- page 1 -->" in markdown
    assert "<!-- page 2 -->" in markdown
    assert markdown.count("## Страница") == 3, "one section per page image"


def test_retry_and_rotation_recovers(two_stubs, tmp_path):
    page = fixtures.make_png(tmp_path / "p.png")
    markdown = _run(reconstruct_markdown([page]))
    assert "## Страница" in markdown, "request must be retried on another endpoint"


def test_exhausted_retries_raise():
    from idp.vlm_client import VlmClient

    with StubServer(fail_first=10) as server:
        settings.vl_endpoints = [server.endpoint]
        client = VlmClient()
        try:
            with pytest.raises(RuntimeError, match="after retries"):
                _run(client.generate_document_annotation(text="аннотация документа"))
        finally:
            settings.vl_endpoints = []


def test_strip_bbox_annotations():
    text = "Сумма 5000 рублей.\n[Image: photo: портрет | bbox: 100 200 300 400 | img: 2]\n"
    cleaned = strip_bbox_annotations(text)
    assert "bbox" not in cleaned
    assert "[Image: photo: портрет" in cleaned


def test_extract_paragraphs_tracks_pages_and_sections():
    markdown = (
        "<!-- page 1 -->\n"
        "# Документ\n\n"
        "Первый абзац первой страницы.\n\n"
        "## Раздел\n\n"
        "Текст раздела.\n\n"
        "<!-- page 2 -->\n\n"
        "Текст второй страницы.\n"
    )
    paragraphs = extract_paragraphs(markdown)
    assert len(paragraphs) == 5
    assert paragraphs[0]["text"] == "# Документ"
    assert paragraphs[0]["section"] == "Документ"
    assert paragraphs[1]["text"] == "Первый абзац первой страницы."
    assert paragraphs[2]["section"] == "Раздел"
    assert paragraphs[2]["section_level"] == 2
    assert paragraphs[4]["page"] == 2
    assert paragraphs[4]["text"] == "Текст второй страницы."


def test_with_feedback_uses_recent_corrections(tmp_path, monkeypatch):
    from idp.config import settings

    corrections = tmp_path / "corrections.jsonl"
    corrections.write_text(
        '{"page_image": "x.png", "markdown": "исправленный текст", "timestamp": 1}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "operator_corrections_path", corrections)
    monkeypatch.setattr(settings, "finetune_feedback_enabled", True)

    from idp.vlm_client import _feedback_cache

    _feedback_cache._cache.clear()
    prompt = with_feedback("BASE PROMPT", force_refresh=True)
    assert "BASE PROMPT" in prompt
    assert "исправленный текст" in prompt
