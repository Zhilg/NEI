"""Tests for the text-only worker: TXT export, rubricators, watch mode, archive."""

from __future__ import annotations

import asyncio
import json
import time
import zipfile
from pathlib import Path

from idp.worker import ARCHIVE_NAME, Worker

import pytest

_FONT = Path(__file__).resolve().parents[1] / "src" / "idp" / "fonts" / "LiberationSerif-Regular.ttf"


@pytest.fixture()
def ocr_stub(monkeypatch):
    """PDF теперь ВСЕГДА идут через VLM-OCR (текстовый слой игнорируется).

    Тесты механики пайплайна (архив, журнал, watch) не должны требовать живой
    vLLM — подменяем OCR детерминированным извлечением текстового слоя,
    имитируя идеальное распознавание.
    """
    from idp import worker as worker_mod

    async def fake(self, file_path: Path, relative: Path) -> tuple[str, int]:
        if file_path.suffix.lower() == ".pdf":
            return worker_mod._extract_text(file_path)
        return "", 0

    monkeypatch.setattr(worker_mod.Worker, "_reconstruct_with_vlm", fake)


def make_text_pdf(path: Path, heading: str, body: str, pages: int = 2) -> Path:
    """A PDF whose embedded text layer really contains Cyrillic (TTF font)."""
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for page_number in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"{heading} (стр. {page_number})", fontsize=14,
                         fontname="lib", fontfile=str(_FONT))
        page.insert_textbox(fitz.Rect(72, 100, 523, 400), body, fontsize=11,
                            fontname="lib", fontfile=str(_FONT))
    doc.save(str(path))
    doc.close()
    return path


def make_scan_pdf(path: Path, pages: int = 2) -> Path:
    """A PDF without a text layer: pages are pure images, like a real scan."""
    import fitz

    from tests import fixtures

    path.parent.mkdir(parents=True, exist_ok=True)
    page_png = fixtures.make_png(path.parent / "_scan_page.png", 400, 300)
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=400, height=300)
        page.insert_image(page.rect, filename=str(page_png))
    doc.save(str(path))
    doc.close()
    page_png.unlink(missing_ok=True)
    return path


def _read_results(output_root: Path) -> list[dict]:
    results_path = output_root / "results.jsonl"
    if not results_path.exists():
        return []
    return [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run(worker: Worker, **kwargs) -> None:
    asyncio.run(worker.run(**kwargs))


BODY = (
    "УДК 51.78\n"
    "ГРНТИ 12.34.56\n"
    "Отчёт описывает выполненные работы за отчётный период и приводит "
    "сводные показатели по объекту. Значения параметров уточняются вручную "
    "и согласуются с заказчиком перед подписанием акта сдачи-приёмки."
)


def test_pdf_to_txt_rubricators_and_archive(workspace, ocr_stub):
    make_text_pdf(workspace.input / "report.pdf", "Отчёт о работах", BODY)
    _run(Worker())

    txt = workspace.output / "txts" / "report.txt"
    assert txt.is_file(), "flat txts/<name>.txt must be written"
    content = txt.read_text(encoding="utf-8")
    assert "Отчёт о работах" in content

    results = _read_results(workspace.output)
    assert len(results) == 1
    record = results[0]
    assert record["status"] == "ok"
    codes = {
        (rubric["type"], rubric["number"])
        for doc in record["documents"]
        for rubric in doc["rubricators"]
    }
    assert ("УДК", "51.78") in codes
    assert ("ГРНТИ", "12.34.56") in codes

    archive = workspace.output / ARCHIVE_NAME
    assert archive.is_file(), "results.zip must be created"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "txts/report.txt" in names
        assert "results.jsonl" in names
        # Живой журнал в архиве свежий, а не первая версия
        zipped = zf.read("results.jsonl").decode("utf-8")
    on_disk = (workspace.output / "results.jsonl").read_bytes().decode("utf-8")
    assert zipped.replace("\r\n", "\n") == on_disk.replace("\r\n", "\n")


def test_rerun_skips_processed_and_keeps_archive(workspace, ocr_stub):
    make_text_pdf(workspace.input / "report.pdf", "Отчёт о работах", BODY)
    _run(Worker())
    _run(Worker())

    assert len(_read_results(workspace.output)) == 1, "second run must skip the file"
    with zipfile.ZipFile(workspace.output / ARCHIVE_NAME) as zf:
        assert len([n for n in zf.namelist() if n == "txts/report.txt"]) == 1


def test_new_file_added_to_archive_incrementally(workspace, ocr_stub):
    make_text_pdf(workspace.input / "first.pdf", "Первый отчёт", BODY)
    _run(Worker())

    make_text_pdf(workspace.input / "second.pdf", "Второй отчёт", BODY)
    _run(Worker())

    assert (workspace.output / "txts" / "second.txt").is_file()
    assert len(_read_results(workspace.output)) == 2
    with zipfile.ZipFile(workspace.output / ARCHIVE_NAME) as zf:
        names = set(zf.namelist())
        assert {"txts/first.txt", "txts/second.txt"} <= names
        zipped_records = [
            json.loads(line)
            for line in zf.read("results.jsonl").decode("utf-8").splitlines()
            if line.strip()
        ]
    assert len(zipped_records) == 2, "results.jsonl inside the zip must be re-synced"


def test_watch_processes_file_appearing_later(workspace, ocr_stub):
    async def scenario() -> None:
        worker = Worker()
        task = asyncio.create_task(worker.run(watch=True, interval=0.1, settle=0))
        await asyncio.sleep(0.3)
        make_text_pdf(workspace.input / "late.pdf", "Поздний отчёт", BODY)
        txt = workspace.output / "txts" / "late.txt"
        for _ in range(100):
            if txt.exists():
                break
            await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert (workspace.output / "txts" / "late.txt").is_file()
    results = _read_results(workspace.output)
    assert len(results) == 1 and results[0]["file"] == "late.pdf"
    with zipfile.ZipFile(workspace.output / ARCHIVE_NAME) as zf:
        assert "txts/late.txt" in zf.namelist()


def test_watch_settle_delays_fresh_files(workspace):
    """A file that is still being copied (fresh mtime) is not picked up yet."""
    make_text_pdf(workspace.input / "copying.pdf", "Отчёт о работах", BODY)

    async def scenario() -> None:
        worker = Worker()
        task = asyncio.create_task(worker.run(watch=True, interval=0.1, settle=3600))
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert not _read_results(workspace.output), "fresh file must wait for the settle window"
    assert not (workspace.output / "txts" / "copying.pdf").exists()


def test_startup_packs_preexisting_output_into_archive(workspace):
    """Файлы, лежавшие в output до запуска воркера, попадают в архив."""
    legacy = workspace.output / "txts" / "legacy.txt"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("результат из прошлого прогона", encoding="utf-8")

    _run(Worker())  # вход пустой — обрабатывать нечего

    with zipfile.ZipFile(workspace.output / ARCHIVE_NAME) as zf:
        assert "txts/legacy.txt" in zf.namelist()


def test_startup_adds_files_missing_from_existing_archive(workspace, ocr_stub):
    """Файл, появившийся в output мимо воркера, докидывается в уже есть архив."""
    make_text_pdf(workspace.input / "report.pdf", "Отчёт о работах", BODY)
    _run(Worker())

    manual = workspace.output / "txts" / "manual.txt"
    manual.write_text("скопировано вручную", encoding="utf-8")
    _run(Worker())

    with zipfile.ZipFile(workspace.output / ARCHIVE_NAME) as zf:
        assert "txts/manual.txt" in zf.namelist()
        assert "txts/report.txt" in zf.namelist()


def test_parallel_zip_rebuild_roundtrip(workspace):
    """Параллельная пересборка: архив читается штатным zipfile, имена
    (включая кириллицу) и содержимое совпадают, CRC сходятся."""
    from idp.worker import _rebuild_zip_parallel

    items = []
    for i in range(300):
        path = workspace.output / "txts" / f"файл_{i:03d}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Содержимое документа номер {i}.\n" * (i % 7 + 1), encoding="utf-8")
        items.append((f"txts/файл_{i:03d}.txt", path))

    tmp = workspace.output / "rebuilt.zip.tmp"
    count = _rebuild_zip_parallel(items, tmp, workers=8)

    assert count == len(items)
    with zipfile.ZipFile(tmp) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert len(names) == len(items)
        for arcname, path in items:
            assert arcname in names
            assert zf.read(arcname) == path.read_bytes()
    # Шард-остатков не осталось
    assert not list(workspace.output.glob("*.zipshard"))
    tmp.unlink()


def test_scan_pdf_without_text_layer_goes_through_vlm(workspace, stub_server):
    """Скан без текстового слоя обязан обрабатываться, а не скипаться."""
    make_scan_pdf(workspace.input / "scan.pdf")
    _run(Worker())

    results = _read_results(workspace.output)
    assert len(results) == 1
    record = results[0]
    assert record["status"] == "ok", record.get("error")
    assert record["pages"] == 2

    txt = workspace.output / "txts" / "scan.txt"
    assert txt.is_file()
    assert txt.read_text(encoding="utf-8").strip(), "VLM must return some text"
    # Рендеры страниц — временные, на диске не остаются
    assert not (workspace.output / "rendered").exists()
    with zipfile.ZipFile(workspace.output / ARCHIVE_NAME) as zf:
        assert "txts/scan.txt" in zf.namelist()


def test_png_scan_goes_through_vlm(workspace, stub_server):
    """PNG/JPEG — это скан по определению: всегда через VLM."""
    from tests import fixtures

    fixtures.make_png(workspace.input / "photo.png", 600, 400)
    _run(Worker())

    results = _read_results(workspace.output)
    assert len(results) == 1
    assert results[0]["status"] == "ok", results[0].get("error")
    assert results[0]["pages"] == 1
    assert (workspace.output / "txts" / "photo.txt").is_file()


def test_archive_contains_only_text_results(workspace, stub_server):
    """В results.zip — только txts и журналы: никаких рендеров и прочего мусора."""
    make_scan_pdf(workspace.input / "scan.pdf")
    make_text_pdf(workspace.input / "text.pdf", "Отчёт о работах", BODY)
    # Мусор от старых прогонов в output
    junk = workspace.output / "rendered" / "old"
    junk.mkdir(parents=True)
    (junk / "page_00001.png").write_bytes(b"\x89PNG")
    (workspace.output / "entities.json").write_text("{}", encoding="utf-8")

    _run(Worker())

    with zipfile.ZipFile(workspace.output / ARCHIVE_NAME) as zf:
        names = zf.namelist()
    assert "txts/scan.txt" in names
    assert "txts/text.txt" in names
    assert "results.jsonl" in names
    assert not any(n.startswith("rendered/") for n in names)
    assert "entities.json" not in names


def test_many_files_processed_in_parallel(workspace, ocr_stub):
    """Пачка файлов проходит быстро: очередь воркеров, а не файл за файлом."""
    for i in range(24):
        make_text_pdf(workspace.input / f"doc_{i:02d}.pdf", f"Отчёт {i}", BODY)

    start = time.perf_counter()
    _run(Worker())
    elapsed = time.perf_counter() - start

    results = _read_results(workspace.output)
    assert len(results) == 24
    assert all(r["status"] == "ok" for r in results)
    assert len(list((workspace.output / "txts").glob("*.txt"))) == 24
    with zipfile.ZipFile(workspace.output / ARCHIVE_NAME) as zf:
        assert len([n for n in zf.namelist() if n.startswith("txts/")]) == 24
    assert elapsed < 30, f"24 files took {elapsed:.1f}s — parallelism regressed"


def test_html_folder_and_docx(workspace, stub_server):
    # stub_server нужен: make_docx кладёт figure.png рядом в input,
    # а картинки теперь идут через VLM.
    from tests import fixtures

    fixtures.make_docx(workspace.input / "spec.docx")
    _run(Worker())

    txt = workspace.output / "txts" / "spec.txt"
    assert txt.is_file()
    assert "Назначение" in txt.read_text(encoding="utf-8")
    # make_docx кладёт figure.png рядом в input — он тоже обрабатывается (skip),
    # поэтому ищем именно запись про DOCX.
    results = _read_results(workspace.output)
    docx_records = [r for r in results if r["type"] == "docx"]
    assert docx_records and docx_records[0]["status"] == "ok"
