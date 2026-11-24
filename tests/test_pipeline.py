"""End-to-end pipeline tests against the VLM stub (no GPU).

Tests the text-only pipeline: flat TXT output (no subfolders, no meta.json),
rubricator extraction, idempotent re-processing and watch-mode pickup
of new files dropped into data/input.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from idp.worker import Worker
from tests.vlm_stub import hash_bytes


def _run_worker(force: bool = False) -> None:
    worker = Worker(force=force)
    asyncio.run(worker.run())


def _pdf_text(path: Path) -> str:
    import fitz

    document = fitz.open(path)
    try:
        return "".join(page.get_text() for page in document)
    finally:
        document.close()


def _txt_names(output: Path) -> list[str]:
    txts = output / "txts"
    if not txts.exists():
        return []
    return sorted(p.name for p in txts.iterdir() if p.is_file())


def test_pdf_pipeline_produces_flat_txt(workspace, sample_pdf, stub_server):
    _run_worker()

    names = _txt_names(workspace.output)
    assert "report.txt" in names, f"expected report.txt, got {names}"
    # Плоская структура: только .txt файлы, нет подпапок и метафайлов
    txts = workspace.output / "txts"
    assert all(p.suffix == ".txt" for p in txts.iterdir() if p.is_file()), "только .txt в txts/"
    assert not any(p.is_dir() for p in txts.iterdir()), "нет подпапок в txts/"
    assert not (txts / "report.pdf").exists(), "нет подпапки report.pdf/"
    assert not (txts / "meta.json").exists(), "нет meta.json"

    text = (txts / "report.txt").read_text(encoding="utf-8")
    assert text.strip(), "TXT не пустой"

    results = [
        json.loads(line)
        for line in (workspace.output / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert results and results[0]["status"] == "ok"


def test_docx_pipeline(workspace, sample_docx, stub_server):
    _run_worker()
    names = _txt_names(workspace.output)
    assert "spec.txt" in names, f"expected spec.txt, got {names}"
    text = (workspace.output / "txts" / "spec.txt").read_text(encoding="utf-8")
    assert "Назначение" in text


def test_force_reprocesses_and_rebuilds(workspace, sample_pdf, stub_server):
    _run_worker()
    first = (workspace.output / "results.jsonl").read_text(encoding="utf-8")

    _run_worker(force=True)
    second = (workspace.output / "results.jsonl").read_text(encoding="utf-8")
    assert second.count("\n") > first.count("\n"), "force re-appends results"


def test_stats_written(workspace, sample_pdf, stub_server):
    _run_worker()
    stats = (workspace.output / "stats.jsonl").read_text(encoding="utf-8").splitlines()
    assert stats and json.loads(stats[-1])["status"] == "ok"


def test_watch_mode_picks_up_new_file(workspace, stub_server):
    _run_worker()  # начальный прогон (если есть файлы)

    # Добавляем новый файл после первого прогона — watch должен подхватить
    (workspace.input / "new.html").write_text(
        "<html><body><h1>Новый документ</h1><p>" + "слово " * 250 + "</p></body></html>",
        encoding="utf-8",
    )

    async def run_watch() -> None:
        w = Worker(force=False)
        task = asyncio.create_task(w.run(input_root=workspace.input, watch=True, interval=1.0))
        await asyncio.sleep(4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_watch())

    names = _txt_names(workspace.output)
    assert "new.txt" in names, f"новый файл не подхвачен watch-ом: {names}"


def test_html_file_in_input_produces_flat_txt(workspace, stub_server):
    """Одиночный HTML-файл прямо в data/input -> плоский txts/<имя>.txt."""
    (workspace.input / "page.html").write_text(
        "<html><body><h1>Заголовок</h1><p>" + "слово " * 250 + "</p></body></html>",
        encoding="utf-8",
    )

    _run_worker()

    names = _txt_names(workspace.output)
    assert "page.txt" in names, f"expected page.txt, got {names}"
    text = (workspace.output / "txts" / "page.txt").read_text(encoding="utf-8")
    assert "Заголовок" in text


def test_html_folder_book_produces_flat_txt(workspace, stub_server):
    """HTML-книга (папка с главами) -> плоский txts/<имя>.txt."""
    book = workspace.input / "книга.html"
    book.mkdir()
    (book / "chapter1.html").write_text(
        "<html><body><h1>Глава 1</h1><p>" + "слово " * 250 + "</p></body></html>",
        encoding="utf-8",
    )
    (book / "chapter2.html").write_text(
        "<html><body><h1>Глава 2</h1><p>" + "слово " * 250 + "</p></body></html>",
        encoding="utf-8",
    )

    _run_worker()

    names = _txt_names(workspace.output)
    assert "книга.txt" in names, f"expected книга.txt, got {names}"
    text = (workspace.output / "txts" / "книга.txt").read_text(encoding="utf-8")
    assert "Глава 1" in text and "Глава 2" in text
