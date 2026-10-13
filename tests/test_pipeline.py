"""End-to-end pipeline tests against the VLM stub (no GPU)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from idp.worker import Worker
from tests.vlm_stub import hash_bytes


def _run_worker(force: bool = False) -> None:
    worker = Worker(artifacts_mode=True, force=force)
    asyncio.run(worker.run())


def _page_hashes(rendered_dir: Path) -> list[str]:
    pngs = sorted(rendered_dir.glob("*.png"))
    return [hash_bytes(png.read_bytes()) for png in pngs]


def _pdf_text(path: Path) -> str:
    import fitz

    document = fitz.open(path)
    try:
        return "".join(page.get_text() for page in document)
    finally:
        document.close()


def test_pdf_pipeline_produces_bundle_and_documents(workspace, sample_pdf, stub_server):
    _run_worker()

    bundle = workspace.output / "txts" / "report.pdf"
    assert (bundle / "text.txt").is_file()
    assert (bundle / "tables.json").is_file()
    assert (bundle / "meta.json").is_file()
    assert (bundle / "reconstruct.docx").is_file()
    assert (bundle / "reconstruct.pdf").is_file()
    assert (bundle / "images" / "page_00001.png").is_file()
    assert (bundle / "images" / "figure_0001.png").is_file()
    assert (bundle / "images" / "handwritten_0001.png").is_file()

    hashes = _page_hashes(workspace.output / "rendered" / "report")
    assert len(hashes) == 3

    text = (bundle / "text.txt").read_text(encoding="utf-8")
    assert "<!-- page 1 -->" in text
    for digest in hashes:
        assert f"## Страница {digest}" in text, "every page heading must reach text.txt"

    results = [
        json.loads(line)
        for line in (workspace.output / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert results and results[0]["status"] == "ok"
    assert results[0]["entities"], "stub returns entities, results must contain them"


def test_pdf_reconstruction_keeps_headings(workspace, sample_pdf, stub_server):
    _run_worker()

    hashes = _page_hashes(workspace.output / "rendered" / "report")
    pdf_text = _pdf_text(workspace.output / "txts" / "report.pdf" / "reconstruct.pdf")
    for digest in hashes:
        assert f"Страница {digest}" in pdf_text, "reconstructed PDF must keep every page heading"
    assert "пункт 1.1" in pdf_text
    assert "|" not in pdf_text  # tables rendered as text cells, not raw pipes


def test_large_document_keeps_all_headings(workspace, large_pdf, stub_server):
    """A big file must not silently drop markup (headings) in reconstruction."""
    _run_worker()

    bundle = workspace.output / "txts" / "big" / "big_report.pdf"
    hashes = _page_hashes(workspace.output / "rendered" / "big" / "big_report")
    assert len(hashes) == 40

    text = (bundle / "text.txt").read_text(encoding="utf-8")
    for digest in hashes:
        assert f"## Страница {digest}" in text, "text.txt must keep every page heading"

    pdf_text = _pdf_text(bundle / "reconstruct.pdf")
    missing = [digest for digest in hashes if f"Страница {digest}" not in pdf_text]
    assert not missing, f"reconstructed PDF lost {len(missing)} page headings: {missing[:3]}"

    import docx

    document = docx.Document(str(bundle / "reconstruct.docx"))
    docx_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    missing_docx = [digest for digest in hashes if f"Страница {digest}" not in docx_text]
    assert not missing_docx, f"reconstructed DOCX lost {len(missing_docx)} page headings"


def test_force_reprocesses_and_rebuilds(workspace, sample_pdf, stub_server):
    _run_worker()
    first = (workspace.output / "results.jsonl").read_text(encoding="utf-8")

    _run_worker(force=True)
    second = (workspace.output / "results.jsonl").read_text(encoding="utf-8")
    assert second.count("\n") > first.count("\n"), "force re-appends results"


def test_docx_pipeline(workspace, sample_docx, stub_server):
    _run_worker()
    bundle = workspace.output / "txts" / "spec.docx"
    assert (bundle / "text.txt").is_file()
    assert (bundle / "reconstruct.docx").is_file()
    text = (bundle / "text.txt").read_text(encoding="utf-8")
    assert "Назначение" in text


def test_entity_store_and_stats(workspace, sample_pdf, stub_server):
    _run_worker()
    entities = json.loads((workspace.output / "entities.json").read_text(encoding="utf-8"))
    assert entities, "entities.json must not be empty"
    stats = (workspace.output / "stats.jsonl").read_text(encoding="utf-8").splitlines()
    assert stats and json.loads(stats[-1])["status"] == "ok"
