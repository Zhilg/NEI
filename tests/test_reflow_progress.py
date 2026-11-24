"""Прогресс длительной VLM-фазы reflow без обращения к реальной модели."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import reflow_vlm  # noqa: E402


@pytest.fixture()
def reflower_factory(monkeypatch):
    """Reflower с заглушкой клиента: сеть и VLM не нужны."""
    monkeypatch.setattr(reflow_vlm, "VlmClient", lambda: object())

    def make(total_files: int) -> reflow_vlm.Reflower:
        return reflow_vlm.Reflower(total_files=total_files)

    return make


def test_progress_reports_remaining_eta_chunks_and_active_file(reflower_factory, caplog):
    reflower = reflower_factory(total_files=10)
    reflower._finished = 3
    reflower._done = 1
    reflower._skipped = 2
    reflower._chunks_total = 8
    reflower._chunks_done = 5
    reflower._active_chunks = 2
    reflower._active_files.add("long_document.txt")
    reflower._started -= 65

    with caplog.at_level(logging.INFO, logger="reflow_vlm"):
        reflower.log_progress()

    message = caplog.records[-1].getMessage()
    assert "3/10 файлов, осталось 7" in message
    assert "VLM-чанки: готово 5 из 8" in message
    assert "в работе 2" in message
    assert "ETA" in message
    assert "long_document.txt" in message


def test_reflow_file_updates_finished_and_success_counters(reflower_factory, tmp_path, monkeypatch):
    path = tmp_path / "broken.txt"
    original = "разорванная строка\n" * 30
    path.write_text(original, encoding="utf-8")
    reflower = reflower_factory(total_files=1)

    async def fake_reflow(chunks: list[str]) -> str:
        return "\n\n".join(chunks)

    monkeypatch.setattr(reflower, "_reflow_chunks", fake_reflow)
    asyncio.run(reflower.reflow_file(path))

    assert reflower._finished == 1
    assert reflower._done == 1
    assert reflower._failed == 0
    assert reflower._chunks_total == 1
    assert not reflower._active_files
    assert path.read_text(encoding="utf-8") == original


def test_non_broken_file_counts_as_finished_skip(reflower_factory, tmp_path):
    path = tmp_path / "normal.txt"
    path.write_text("Обычный текст без лесенки.\n", encoding="utf-8")
    reflower = reflower_factory(total_files=1)

    asyncio.run(reflower.reflow_file(path))

    assert reflower._finished == 1
    assert reflower._skipped == 1
    assert reflower._done == 0
    assert reflower._failed == 0
