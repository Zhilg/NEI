"""Эмуляция реальных болевых случаев корпуса: мелкий шрифт, две колонки,
сноски, битые сканы, повёрнутые страницы, 'лесенка' колонок.

VLM локально не эмулируется — эти тесты проверяют всё, что проверяемо
детерминированно: геометрию рендера (фактическое разрешение, которое видит
модель), устойчивость пайплайна к битым файлам, классификацию (текстовый
слой/скан) и пост-обработку 'лесенки' на синтетическом OCR-выводе.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import fix_results  # noqa: E402

from idp.config import settings  # noqa: E402
from idp.plaintext import to_plain_text  # noqa: E402
from idp.renderer import render_pdf_to_pngs  # noqa: E402
from idp.worker import Worker  # noqa: E402
from tests.test_worker import (  # noqa: E402
    _FONT,
    _read_results,
    _run,
    make_scan_pdf,
    make_text_pdf,
    ocr_stub,  # noqa: F401 — фикстура, используется через аргументы
)

_pdf_has_text_layer = fix_results._pdf_has_text_layer


def make_two_column_small_font_pdf(path: Path) -> Path:
    """А4, две узкие колонки, 7pt — классика научной статьи/книги."""
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    body = (
        "История вопроса начинается с работ классиков отрасли, в которых "
        "были заложены основы дальнейших исследований. Каждый последующий "
        "этап развития науки опирался на результаты предыдущих лет. "
    ) * 6
    for _ in range(3):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_textbox(fitz.Rect(50, 50, 287, 790), body, fontsize=7,
                            fontname="lib", fontfile=str(_FONT))
        page.insert_textbox(fitz.Rect(307, 50, 545, 790), body, fontsize=7,
                            fontname="lib", fontfile=str(_FONT))
    doc.save(str(path))
    doc.close()
    return path


def make_footnote_pdf(path: Path) -> Path:
    """А4, основной текст 11pt, сноски 5.5pt внизу страницы."""
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    body = (
        "Основной текст документа, описывающий предмет исследования. "
        "Изложение ведётся в академическом стиле со ссылками на источники. "
    ) * 5
    footnote = (
        "1 См.: Иванов И.И. Избранные труды. М., 1985. С. 12–34. "
        "2 Там же. С. 45. Подробнее см. также комментарии к изданию 1998 г."
    )
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(fitz.Rect(50, 50, 545, 640), body, fontsize=11,
                        fontname="lib", fontfile=str(_FONT))
    page.draw_line((50, 660), (545, 660), color=(0, 0, 0), width=0.5)
    page.insert_textbox(fitz.Rect(50, 670, 545, 800), footnote, fontsize=5.5,
                        fontname="lib", fontfile=str(_FONT))
    doc.save(str(path))
    doc.close()
    return path


def make_rotated_scan_pdf(path: Path) -> Path:
    """'Скан' повёрнутой страницы: страница с рендером, повёрнута на 90°."""
    import fitz
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    src = make_text_pdf(path.parent / "_src.pdf", "Глава первая",
                        "Текст страницы, отсканированной набок. " * 4, pages=1)
    doc = fitz.open(str(src))
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csRGB, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).rotate(
        90, expand=True
    )
    png = path.parent / "_rot.png"
    img.save(png)
    doc.close()

    scan = fitz.open()
    page = scan.new_page(width=img.width, height=img.height)
    page.insert_image(page.rect, filename=str(png))
    scan.save(str(path))
    scan.close()
    src.unlink(missing_ok=True)
    png.unlink(missing_ok=True)
    return path


# ---------------------------------------------------------------------------
# Разрешение: гвард пакета 300dpi / 3072px / 6.4Мп
# ---------------------------------------------------------------------------

def test_a4_render_resolution_guard(tmp_path, monkeypatch):
    """А4 при 300dpi = 2480x3508, кап 3072 ужимает длинную сторону до 3072.

    Эффективное разрешение на А4 ~263dpi; ниже 250 — регресс качества OCR
    мелкого шрифта (было ~160dpi при пакете 200dpi/2048px/2.5Мп).
    """
    monkeypatch.setattr(settings, "render_dpi", 300)
    monkeypatch.setattr(settings, "max_image_dimension", 3072)
    pdf = make_text_pdf(tmp_path / "a4.pdf", "Документ", "Текст " * 40, pages=1)
    pngs = render_pdf_to_pngs(pdf, tmp_path / "pages")
    assert len(pngs) == 1
    from PIL import Image

    with Image.open(pngs[0]) as im:
        long_side = max(im.size)
    assert long_side == 3072, f"кап размера не применился: {im.size}"
    effective_dpi = 3072 / (842 / 72)
    assert effective_dpi >= 250, f"эффективное разрешение просело: {effective_dpi:.0f}dpi"


def test_compose_defaults_guard():
    """Дефолты compose не должны откатываться на старый пиксельный потолок."""
    compose = (REPO_ROOT / "infra" / "compose" / "local.yml").read_text(encoding="utf-8")
    checks = {
        "IDP_RENDER_DPI": r"IDP_RENDER_DPI:-300\b",
        "IDP_MAX_IMAGE_DIMENSION": r"IDP_MAX_IMAGE_DIMENSION:-3072\b",
        "VLLM_IMAGE_MAX_PIXELS": r"IDP_VLLM_IMAGE_MAX_PIXELS:-6400000\b",
        "IDP_VL_MAX_TOKENS": r"IDP_VL_MAX_TOKENS:-2048\b",
    }
    for name, pattern in checks.items():
        assert re.search(pattern, compose), (
            f"дефолт {name} в compose откатился — качество OCR просядет"
        )


def test_degenerate_strip_page_boosted_to_min_side(tmp_path, monkeypatch):
    """Страницы-«полосы» (медиабокс в пунктах) давали 4px-картинки, на которых
    падал Qwen3VLProcessor (400 Bad Request на всех репликах сразу)."""
    monkeypatch.setattr(settings, "render_dpi", 300)
    monkeypatch.setattr(settings, "max_image_dimension", 3072)
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=2, height=500)  # узкая полоса ~8x2083 px @300dpi
    page.insert_text((1, 250), "x")
    pdf = tmp_path / "strip.pdf"
    doc.save(str(pdf))
    doc.close()

    pngs = render_pdf_to_pngs(pdf, tmp_path / "pages")
    assert len(pngs) == 1
    from PIL import Image

    with Image.open(pngs[0]) as im:
        assert min(im.size) >= 64, f"вырожденная сторона не поднята: {im.size}"


def test_zero_page_document_raises_clear_error(tmp_path):
    """PDF с битым page tree открывается, но содержит 0 страниц — раньше это
    умирало с непонятным «max_workers must be greater than 0»."""
    pdf = tmp_path / "empty.pdf"
    # fitz не даёт сохранить 0-страничный документ — пишем минимальный
    # валидный PDF-контейнер без страниц (как у битых файлов корпуса,
    # которые открываются fitz'ом, но дают page_count == 0).
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )

    with pytest.raises(ValueError, match="0 страниц"):
        render_pdf_to_pngs(pdf, tmp_path / "pages")


def test_ensure_vlm_safe_image_keeps_normal_pages():
    """Обычные страницы книг не трогаем — только вырожденные случаи."""
    from PIL import Image

    from idp.renderer import ensure_vlm_safe_image

    normal = Image.new("RGB", (2480, 3508), "white")
    assert ensure_vlm_safe_image(normal) is normal
    # нечётные стороны у крупной картинки — мягкий ресайз до чётных, без канваса
    odd = Image.new("RGB", (1653, 2339), "white")
    fixed = ensure_vlm_safe_image(odd)
    assert fixed.size == (1654, 2340), fixed.size


def test_ensure_vlm_safe_image_letterboxes_strips():
    """Полосы и мини-страницы — белый квадратный канвас, кратный 64."""
    from PIL import Image

    from idp.renderer import ensure_vlm_safe_image

    for size in [(4, 1250), (125, 313)]:
        img = Image.new("RGB", size, "white")
        safe = ensure_vlm_safe_image(img)
        w, h = safe.size
        assert w == h, f"канвас не квадратный: {safe.size}"
        assert w % 64 == 0, f"сторона не кратна 64: {safe.size}"
        assert min(safe.size) >= 256
    # force — канвас даже для нормальной картинки (retry-путь VLM)
    normal = Image.new("RGB", (2480, 3508), "white")
    forced = ensure_vlm_safe_image(normal, force=True)
    assert forced.size[0] == forced.size[1]
    assert forced.size[0] % 64 == 0


def test_post_reconstruct_retries_with_safe_images(tmp_path):
    """processor-400 не убивает чанк: один повтор с безопасным форматом."""
    import asyncio

    from PIL import Image

    from idp.vlm_client import _post_reconstruct

    png = tmp_path / "page_00001.png"
    Image.new("RGB", (125, 313), "white").save(png, "PNG")

    class _Resp:
        status_code = 200
        text = "ok"

    calls = []

    class _Sel:
        def next(self):
            return "http://fake:8000/v1"

    class _Http:
        selector = _Sel()

        async def post(self, url, payload):
            calls.append(payload)
            if len(calls) == 1:
                raise RuntimeError(
                    'VLM request failed: 400 {"error":{"message":"Failed to apply '
                    'Qwen3VLProcessor on data=..."}}'
                )
            return _Resp()

    def safe_payload():
        return {"safe": True}

    resp = asyncio.run(_post_reconstruct(_Http(), {"orig": True}, safe_payload))
    assert resp.status_code == 200
    assert len(calls) == 2
    assert calls[1] == {"safe": True}


def test_post_reconstruct_passes_through_other_errors():
    """Другие RuntimeError не дёргают retry — пробрасываются как есть."""
    import asyncio

    from idp.vlm_client import _post_reconstruct

    class _Sel:
        def next(self):
            return "http://fake:8000/v1"

    class _Http:
        selector = _Sel()

        async def post(self, url, payload):
            raise RuntimeError("VLM request failed: 500 internal")

    with pytest.raises(RuntimeError, match="500 internal"):
        asyncio.run(_post_reconstruct(_Http(), {}, lambda: {}))


# ---------------------------------------------------------------------------
# Быстрый путь: чистый текстовый слой -> сразу текст, мусор -> VLM
# ---------------------------------------------------------------------------

from idp.textlayer import extract_clean_text_layer, looks_like_clean_text  # noqa: E402


def test_looks_like_clean_text_accepts_real_russian():
    text = ("Война и мир. " * 500 + "\n") * 3
    assert looks_like_clean_text(text, page_count=3)


@pytest.mark.parametrize("bad", [
    "█" * 3000,                                    # псевдографика
    "Òàêñè èìåíà ñëîâàðÿ " * 150,                  # битая кодировка cp1251
    "word " * 30,                                  # слишком мало текста
    ("x" * 200 + " ") * 10 + "a" * 30,             # нет алфавитных слов
    "абвгд " * 10 + "�" * 900,                     # replacement chars
])
def test_looks_like_clean_text_rejects_garbage(bad):
    assert not looks_like_clean_text(bad, page_count=2), bad[:40]


def _make_layer_pdf(path: Path, body: str, pages: int = 2) -> Path:
    """PDF с реальным кириллическим текстовым слоем, крупная область текста."""
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_textbox(
            fitz.Rect(50, 50, 545, 780), body, fontsize=12,
            fontname="lib", fontfile=str(_FONT),
        )
    doc.save(str(path))
    doc.close()
    return path


def test_extract_clean_text_layer_on_born_digital_pdf(tmp_path):
    body = "Война и мир. " * 250  # ~3200 знаков на страницу
    pdf = _make_layer_pdf(tmp_path / "book.pdf", body, pages=2)
    got = extract_clean_text_layer(pdf)
    assert got is not None
    text, pages = got
    assert pages == 2
    assert "Война и мир" in text
    assert looks_like_clean_text(text, pages)


def test_extract_clean_text_layer_rejects_garbage_layer(tmp_path):
    pdf = _make_layer_pdf(tmp_path / "junk.pdf", "█" * 3000, pages=2)
    assert extract_clean_text_layer(pdf) is None


def test_worker_clean_textlayer_bypasses_vlm(workspace, monkeypatch):
    """Чистый слой обрабатывается сразу — VLM-фолбэк не дёргается."""
    from idp import worker as worker_mod

    calls: list = []

    async def fake(self, file_path, relative):
        calls.append(file_path)
        return "", 0

    monkeypatch.setattr(worker_mod.Worker, "_reconstruct_with_vlm", fake)
    body = "Война и мир. " * 250
    _make_layer_pdf(workspace.input / "book.pdf", body, pages=2)
    _run(Worker())

    assert calls == [], "VLM вызван для чистого текстового слоя!"
    txt = (workspace.output / "txts" / "book.txt").read_text(encoding="utf-8")
    assert "Война и мир" in txt
    results = _read_results(workspace.output)
    assert results[0]["documents"][0].get("source") == "textlayer"


def test_worker_dirty_layer_falls_back_to_vlm(workspace, monkeypatch):
    """Отравленный слой (битая кодировка) отсеивается эвристикой -> VLM-фолбэк."""
    from idp import worker as worker_mod

    calls: list = []

    async def fake(self, file_path, relative):
        calls.append(file_path)
        return worker_mod._extract_text(file_path)

    monkeypatch.setattr(worker_mod.Worker, "_reconstruct_with_vlm", fake)
    body = "Òàêñè èìåíà ñëîâàðÿ. " * 200  # cp1251, прочитанная как latin1
    _make_layer_pdf(workspace.input / "junk.pdf", body, pages=2)
    _run(Worker())

    assert len(calls) == 1, "VLM-фолбэк не вызван для отравленного слоя"
    results = _read_results(workspace.output)
    assert results[0]["status"] == "ok"
    assert results[0]["documents"][0].get("source") != "textlayer"


def test_pdf_requeue_keeps_textlayer_records():
    """Записи быстрого пути (source=textlayer) не выкидываются как старый формат."""
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    rec = {"file": "sub/book.pdf", "status": "ok",
           "documents": [{"text": "текст", "source": "textlayer", "rubricators": []}]}
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    res = fix_results._classify_journal_line(line, frozenset({"book"}))
    assert res is not None, "textlayer-запись ошибочно выкинута"
    # контроль: та же запись без source — выкидывается
    rec2 = {"file": "sub/book.pdf", "status": "ok",
            "documents": [{"text": "текст", "rubricators": []}]}
    line2 = (json.dumps(rec2, ensure_ascii=False) + "\n").encode("utf-8")
    assert fix_results._classify_journal_line(line2, frozenset({"book"})) is None


# ---------------------------------------------------------------------------
# Текстовые файлы книг: .txt/.fb2 идут на обработку сразу, без VLM
# ---------------------------------------------------------------------------

def test_txt_files_processed_immediately(workspace):
    """.txt в input обрабатываются как обычные файлы (они раньше игнорировались —
    не было в SUPPORTED_EXTENSIONS) — в любой кодировке."""
    body = "Война и мир. " * 300
    (workspace.input / "book.txt").write_text(body, encoding="utf-8")
    (workspace.input / "legacy.txt").write_bytes(body.encode("cp1251"))
    (workspace.input / "wide.txt").write_bytes(body.encode("utf-16"))
    _run(Worker())

    for name in ("book.txt", "legacy.txt", "wide.txt"):
        stem = name.rsplit(".", 1)[0]
        out = (workspace.output / "txts" / f"{stem}.txt").read_text(encoding="utf-8")
        assert "Война и мир" in out, f"{name}: текст не извлёкся"
    results = _read_results(workspace.output)
    assert len(results) == 3
    assert all(r["status"] == "ok" for r in results)


def test_fb2_processed_immediately(workspace):
    """FictionBook (.fb2) — XML, текст собирается без VLM."""
    paragraphs = "\n".join(
        f"<p>Глава {i}. Однажды в студёную зимнюю пору слово слово слово.</p>"
        for i in range(1, 30)
    )
    fb2 = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<FictionBook><body><section><title><p>Книга</p></title>"
        f"{paragraphs}"
        "</section></body></FictionBook>"
    )
    (workspace.input / "novel.fb2").write_text(fb2, encoding="utf-8")
    _run(Worker())

    out = (workspace.output / "txts" / "novel.txt").read_text(encoding="utf-8")
    assert "Однажды в студёную зимнюю пору" in out
    results = _read_results(workspace.output)
    assert results[0]["status"] == "ok"


def test_worker_startup_normalizes_dirty_txts(workspace):
    """Грязные txt в output/txts чистятся автоматически при старте воркера —
    даже если input пуст и воркеру обрабатывать нечего."""
    txts = workspace.output / "txts"
    txts.mkdir()
    dirty = (
        "Оглавление\n"
        "Глава первая ....... 3\n"
        "Глава вторая ....... 27\n"
        "\n"
        "Страница 3 из 12\n"
        "Наташа Ростова вышла в сад и задумалась о предстоящем вечере. "
        "Вечер обещал быть интересным, гостей ждали много.\n"
        "Страница 4 из 12\n"
        "Пьер Безухов чувствовал себя не в своей тарелке. "
        "Он давно не бывал на таких приёмах. "
    )
    (txts / "novel.txt").write_text(dirty, encoding="utf-8")
    _run(Worker())

    out = (txts / "novel.txt").read_text(encoding="utf-8")
    assert "Страница 3 из 12" not in out
    assert "Страница 4 из 12" not in out
    assert "Глава первая ......." not in out
    assert "Наташа Ростова" in out
    assert "Пьер Безухов" in out


def test_worker_startup_merges_split_book(workspace):
    """Книга, ошибочно разбитая на части (base.txt + base_2.txt), склеивается
    в один файл при старте воркера; сборники и газеты не трогаем."""
    txts = workspace.output / "txts"
    txts.mkdir()
    sentence = (
        "Долгие годы эта рукопись лежала в архиве, пока её не нашёл исследователь. "
    )
    (txts / "chronicle.txt").write_text(
        "Глава 1\n\n" + sentence * 60, encoding="utf-8"
    )
    (txts / "chronicle_2.txt").write_text(
        "Глава 2\n\n" + sentence * 60, encoding="utf-8"
    )
    # Сборник рассказов (3+ части с маркерами жанра) — должен остаться разбитым.
    story = "Рассказ\n\n" + "Он вышел на улицу и задумался о прожитом дне. " * 40
    (txts / "collection.txt").write_text(story, encoding="utf-8")
    (txts / "collection_2.txt").write_text(story, encoding="utf-8")
    (txts / "collection_3.txt").write_text(story, encoding="utf-8")
    _run(Worker())

    merged = (txts / "chronicle.txt").read_text(encoding="utf-8")
    assert "Глава 1" in merged and "Глава 2" in merged
    assert not (txts / "chronicle_2.txt").exists()
    assert (txts / "collection.txt").exists()
    assert (txts / "collection_2.txt").exists()
    assert (txts / "collection_3.txt").exists()


# ---------------------------------------------------------------------------
# verify_reprocess: сверка output (журнал vs txts), input — только для проблем
# ---------------------------------------------------------------------------

import verify_reprocess  # noqa: E402


def _write_rec(output_root, rel, status, **extra):
    rec = {"file": rel, "type": "pdf", "status": status, "documents": []}
    rec.update(extra)
    with open(output_root / "results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_verify_requeues_ok_without_txt(workspace):
    """ok-запись без txt на диске -> файл на переработку, записи выкинуты."""
    make_text_pdf(workspace.input / "lost.pdf", "Документ", "Текст " * 50)
    _write_rec(workspace.output, "lost.pdf", "ok")
    _write_rec(workspace.output, "fine.pdf", "ok")
    (workspace.output / "txts").mkdir()
    (workspace.output / "txts" / "fine.txt").write_text("живой результат", encoding="utf-8")

    assert verify_reprocess.cmd_verify(workspace.input, workspace.output) == 0

    state = json.loads((workspace.output / verify_reprocess.STATE_NAME).read_text(encoding="utf-8"))
    assert state["retry"] == ["lost.pdf"]
    assert state["quarantined"] == {}
    # Запись lost выкинута из журнала (воркер переработает), fine осталась.
    journal = (workspace.output / "results.jsonl").read_text(encoding="utf-8")
    assert "lost.pdf" not in journal
    assert "fine.pdf" in journal


def test_verify_quarantines_dead_error_file(workspace):
    """error + мёртвый input (0 байт / не PDF) -> карантин: skip-запись,
    повторных ретраев не будет, отчёт в quarantine.txt."""
    (workspace.input / "empty.pdf").write_bytes(b"")
    (workspace.input / "notpdf.pdf").write_bytes(b"MZ" + b"\x00" * 300)
    _write_rec(workspace.output, "empty.pdf", "error", error="boom")
    _write_rec(workspace.output, "notpdf.pdf", "error", error="boom")

    assert verify_reprocess.cmd_verify(workspace.input, workspace.output) == 0

    state = json.loads((workspace.output / verify_reprocess.STATE_NAME).read_text(encoding="utf-8"))
    assert state["retry"] == []
    assert set(state["quarantined"]) == {"empty.pdf", "notpdf.pdf"}
    # В журнале ровно одна skip-запись на файл, с пометкой quarantine.
    journal = (workspace.output / "results.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(l) for l in journal.splitlines() if l.strip()]
    assert len(lines) == 2
    assert all(l["status"] == "skip" and l["quarantine"] for l in lines)
    assert not any(l["status"] == "error" for l in lines)
    report = (workspace.output / verify_reprocess.QUARANTINE_NAME).read_text(encoding="utf-8")
    assert "empty.pdf" in report and "notpdf.pdf" in report


def test_verify_keeps_live_error_for_retry(workspace):
    """error + живой input -> в списке переработки; запись выкидывается
    (processed-сет воркера строится из ok/skip — он ретраит файл сам)."""
    make_text_pdf(workspace.input / "alive.pdf", "Документ", "Текст " * 50)
    _write_rec(workspace.output, "alive.pdf", "error", error="vlm timeout")

    assert verify_reprocess.cmd_verify(workspace.input, workspace.output) == 0

    state = json.loads((workspace.output / verify_reprocess.STATE_NAME).read_text(encoding="utf-8"))
    assert state["retry"] == ["alive.pdf"]
    assert state["quarantined"] == {}
    # В журнале не осталось записей о файле — при следующем старте воркер
    # увидит его как необработанный и ретраит.
    journal = (workspace.output / "results.jsonl").read_text(encoding="utf-8")
    assert "alive.pdf" not in journal


def test_verify_clean_when_nothing_missing(workspace):
    """Все ok имеют txt — verify ничего не трогает."""
    (workspace.output / "txts").mkdir()
    (workspace.output / "txts" / "a.txt").write_text("текст", encoding="utf-8")
    _write_rec(workspace.output, "a.pdf", "ok")

    assert verify_reprocess.cmd_verify(workspace.input, workspace.output) == 0
    state = json.loads((workspace.output / verify_reprocess.STATE_NAME).read_text(encoding="utf-8"))
    assert state["retry"] == [] and state["quarantined"] == {}


def test_wait_exits_when_retried(workspace):
    """wait дожидается ok/skip-записей по списку из verify."""
    _write_rec(workspace.output, "a.pdf", "ok")  # журнал непустой
    state = {
        "retry": ["a.pdf", "b.pdf"],
        "quarantined": {},
        "journal_offset": (workspace.output / "results.jsonl").stat().st_size,
    }
    (workspace.output / verify_reprocess.STATE_NAME).write_text(
        json.dumps(state), encoding="utf-8"
    )
    _write_rec(workspace.output, "a.pdf", "ok")
    _write_rec(workspace.output, "b.pdf", "skip")

    assert verify_reprocess.cmd_wait(workspace.output, timeout_sec=5) == 0


# ---------------------------------------------------------------------------
# Болевые типы документов: классификация и устойчивость пайплайна
# ---------------------------------------------------------------------------

def test_two_column_small_font_is_textlayer_pdf(tmp_path):
    pdf = make_two_column_small_font_pdf(tmp_path / "article.pdf")
    assert _pdf_has_text_layer(pdf) is True, (
        "двухколоночный born-digital PDF должен попасть в pdf-requeue"
    )


def test_footnote_fixture_really_has_small_footnotes(tmp_path):
    import fitz

    pdf = make_footnote_pdf(tmp_path / "doc.pdf")
    doc = fitz.open(str(pdf))
    text = doc[0].get_text()
    assert "Избранные труды" in text, "сноски должны быть в фикстуре"
    sizes = {span["size"] for block in doc[0].get_text("dict")["blocks"]
             if block["type"] == 0 for line in block["lines"] for span in line["spans"]}
    assert min(sizes) < 7, f"в фикстуре нет мелкого шрифта: {sizes}"
    doc.close()


def test_rotated_scan_classifies_as_scan(tmp_path):
    pdf = make_rotated_scan_pdf(tmp_path / "rotated.pdf")
    assert _pdf_has_text_layer(pdf) is False, (
        "повёрнутый скан — картинка без текстового слоя, должен идти через VLM"
    )


def test_corrupt_pdfs_never_crash_pipeline(workspace, stub_server):
    """Битые файлы: не падать, не ломать соседние, помечаться error.

    Идём через реальный путь VLM (stub_server), а не через ocr_stub:
    битый pdf должен упасть на fitz.open внутри _reconstruct_with_vlm и
    стать status=error — если он станет 'skip', он пропадёт из пайплайна
    навсегда (skip считается обработанным).
    """
    make_text_pdf(workspace.input / "good.pdf", "Нормальный документ", "Текст " * 20)
    good_bytes = (workspace.input / "good.pdf").read_bytes()
    (workspace.input / "truncated.pdf").write_bytes(good_bytes[: len(good_bytes) // 2])
    (workspace.input / "garbage.pdf").write_bytes(b"%PDF-1.4\n" + b"\x00\xff" * 1024)

    assert _pdf_has_text_layer(workspace.input / "truncated.pdf") is False
    assert _pdf_has_text_layer(workspace.input / "garbage.pdf") is False

    from idp.worker import Worker

    _run(Worker())
    results = {r["file"]: r["status"] for r in _read_results(workspace.output)}
    assert results.get("good.pdf") == "ok", f"валидный файл пострадал: {results}"
    # неоткрываемый мусор — error: виден в журнале и может быть перекинут
    assert results.get("garbage.pdf") == "error", f"мусорный pdf не помечен error: {results}"
    # обрезанный pymupdf часто восстанавливает (рендерит остаток) — главное,
    # что он не пропал молча: любой осмысленный статус, запись есть
    assert results.get("truncated.pdf") in ("ok", "error"), (
        f"обрезанный pdf потерялся: {results}"
    )


def test_same_stem_in_nested_folders_survives_restart(workspace, ocr_stub):
    """API/usermanual.pdf и API/API/usermanual.pdf — оба живут в output.

    Без предзагрузки _used с диска второй запуск воркера перезаписал бы
    чужой txt (экспортер пустой после рестарта).  После переименования по
    теме первый файл живёт под именем темы, второй — под usermanual_2
    (карта имён в _names_map.json связывает их с журналом).
    """
    from idp.worker import Worker

    (workspace.input / "API" / "API").mkdir(parents=True)
    make_text_pdf(workspace.input / "API" / "usermanual.pdf", "Первый файл",
                  "Содержимое первого файла " * 5)
    _run(Worker())
    make_text_pdf(workspace.input / "API" / "API" / "usermanual.pdf", "Второй файл",
                  "Содержимое второго файла " * 5)
    _run(Worker())

    txts = {p.name: p.read_text(encoding="utf-8")
            for p in (workspace.output / "txts").glob("*.txt")}
    assert "Первый файл.txt" in txts, f"первый файл потерялся: {sorted(txts)}"
    # второй файл — под usermanual_2: стебель usermanual занят записью в
    # журнале (экспортер считает занятыми имена из журнала, а не с диска)
    assert "usermanual_2.txt" in txts, f"второй файл не записан: {sorted(txts)}"
    assert "Содержимое первого файла" in txts["Первый файл.txt"]
    assert "Содержимое второго файла" in txts["usermanual_2.txt"]
    # исходное имя -> тема записаны в карту (verify свяжет с журналом)
    names_map = json.loads(
        (workspace.output / "txts" / "_names_map.json").read_text(encoding="utf-8")
    )
    assert names_map.get("usermanual") == "Первый файл"


# ---------------------------------------------------------------------------
# Ремонт битых PDF через pikepdf (fitz.reject -> repair -> fitz.open)
# ---------------------------------------------------------------------------

def test_open_pdf_falls_back_to_repair(tmp_path, monkeypatch):
    """fitz отказал -> pikepdf чинит копию -> документ открывается."""
    import fitz

    from idp import renderer

    pdf = make_text_pdf(tmp_path / "doc.pdf", "Документ", "Текст " * 40, pages=2)
    orig_open = fitz.open
    calls = {"n": 0}

    def flaky_open(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1 and args and str(args[0]) == str(pdf):
            raise fitz.FileDataError("simulated corrupt xref")
        return orig_open(*args, **kwargs)

    monkeypatch.setattr(fitz, "open", flaky_open)
    doc = renderer.open_pdf(pdf)
    try:
        assert len(doc) == 2, "починенная копия должна открываться"
    finally:
        doc.close()
    assert calls["n"] >= 2, "ремонтный путь не вызван"


def test_requeue_detects_textlayer_after_repair(tmp_path, monkeypatch):
    """pdf-requeue: битый файл с текстовым слоем всё равно на переработку."""
    import fitz

    pdf = make_text_pdf(tmp_path / "doc.pdf", "Документ", "Слой текста " * 40, pages=1)
    orig_open = fitz.open
    calls = {"n": 0}

    def flaky_open(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1 and args and str(args[0]) == str(pdf):
            raise fitz.FileDataError("simulated corrupt xref")
        return orig_open(*args, **kwargs)

    monkeypatch.setattr(fitz, "open", flaky_open)
    assert fix_results._pdf_has_text_layer(pdf) is True


def test_repair_gives_up_on_empty_and_garbage(tmp_path):
    from idp.pdf_repair import repair_pdf

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert repair_pdf(empty) is None
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"%PDF-1.4\n" + b"\x00\xff" * 1024)
    assert repair_pdf(garbage) is None


def test_has_textlayer_never_raises_on_broken_page_tree(tmp_path, monkeypatch):
    """fitz.open прошёл, а page_count/страницы падают — функция молчит False.

    Регрессия: 'Анна Петровна.pdf' открывался, но len(doc) кидал исключение
    посреди pdf-requeue/wait и валил весь прогон.
    """
    import fitz

    class _DeadDoc:
        @property
        def page_count(self):
            raise RuntimeError("damaged page tree")

        def close(self):
            raise RuntimeError("close also broken")

    pdf = tmp_path / "dead.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")  # не важно: open подменён
    monkeypatch.setattr(fitz, "open", lambda *a, **kw: _DeadDoc())
    assert fix_results._pdf_has_text_layer(pdf) is False


# ---------------------------------------------------------------------------
# 'Лесенка' колонок: детерминированная эмуляция OCR-вывода
# ---------------------------------------------------------------------------

def test_reflow_joins_column_broken_lines():
    """Строки, оборванные узкой колонкой посреди фразы, склеиваются в абзацы."""
    text = (
        "Первое предложение абзаца разорвано колон\n"
        "кой посреди слова без переноса и оно долж\n"
        "но склеиться в одну строку. Второе предложе\n"
        "ние продолжает абзац дальше.\n"
    )
    out = to_plain_text(text)
    lines = [ln for ln in out.split("\n") if ln]
    assert len(lines) == 1, f"абзац не собран: {lines}"
    assert "склеиться в одну строку. Второе предложе ние" in lines[0]


def test_reflow_keeps_tables_lists_and_headings_intact():
    """Табличные строки, списки и заголовки ВЕРХНИМ РЕГИСТРОМ не склеиваются."""
    text = (
        "ЗАГОЛОВОК ГЛАВЫ\n"
        "| Параметр | Значение |\n"
        "|---|---|\n"
        "| Масса | 12.5 |\n"
        "| Длина | 3400 |\n"
        "- первый пункт списка\n"
        "- второй пункт списка\n"
    )
    out = to_plain_text(text)
    lines = [ln for ln in out.split("\n") if ln]
    assert "ЗАГОЛОВОК ГЛАВЫ" in lines
    assert any("Масса" in ln and "12.5" in ln for ln in lines)
    assert any("Длина" in ln and "3400" in ln for ln in lines)
    # табличные строки остались раздельными (двойной пробел — признак таблицы)
    assert sum(1 for ln in lines if "  " in ln) == 3
    assert sum(1 for ln in lines if ln.startswith("- первый")) == 1
    assert sum(1 for ln in lines if ln.startswith("- второй")) == 1
    for i, ln in enumerate(lines):
        if "Масса" in ln:
            assert i + 1 < len(lines) and "Длина" in lines[i + 1], (
                "строки таблицы склеились с соседними"
            )


# ---------------------------------------------------------------------------
# Чистка 'не-основного текста': шапки УДК, списки литературы/источников
# ---------------------------------------------------------------------------

from idp.plaintext import (  # noqa: E402
    extract_title,
    sanitize_filename,
    strip_references,
)
from idp.txt_normalize import rename_by_theme  # noqa: E402


def test_strip_references_cuts_list_at_end():
    """Список литературы в конце документа вырезается целиком."""
    body = "В работе рассматриваются основные подходы к анализу данных. " * 20
    text = (
        "Методы исследования\n"
        "\n"
        + body
        + "\n\n"
        "Список использованных источников\n"
        "1. Иванов И.И. Избранные труды. М., 1985. 234 с.\n"
        "2. Петров П.П. Основы науки. Л., 1990. 150 с.\n"
        "3. Сидоров С.С. Статистика. М., 1999. 88 с.\n"
        "4. Кузнецов К.К. Анализ. СПб., 2001. 300 с.\n"
        "5. Волков В.В. Методы. М., 2005. 120 с.\n"
    )
    out = strip_references(text)
    assert "Список использованных источников" not in out
    assert "Иванов И.И." not in out
    assert "основные подходы" in out


def test_strip_references_keeps_word_literature_mid_text():
    """Слово «литература» отдельной строкой посреди текста не трогаем."""
    body = ("В этом разделе даётся обзор литературы по теме исследования. "
            "Рассматриваются ключевые работы. ") * 10
    text = (
        "Введение в предмет\n"
        "\n"
        + body
        + "\n\n"
        "Литература\n"
        "\n"
        "Далее идёт обычный абзац без списка источников, он должен остаться. "
        "Продолжение абзаца с пояснениями."
    )
    out = strip_references(text)
    assert "Литература" in out
    assert "обычный абзац без списка источников" in out


def test_to_plain_text_strips_udk_in_header_only():
    """«УДК/ГРНТИ/ББК» вырезаются в шапке, но не в середине текста."""
    body = "Документ описывает методику расчёта систем отопления. " * 10
    text = (
        "УДК 338.245\n"
        "ГРНТИ 06.58.01\n"
        "ББК 22.1я7\n"
        "\n"
        "Методика расчёта теплоснабжения\n"
        "\n"
        + body
        + "\n\n"
        "В приложении приводится классификатор УДК 621.3 для справок."
    )
    out = to_plain_text(text)
    assert "УДК 338.245" not in out
    assert "ГРНТИ 06.58.01" not in out
    assert "ББК 22.1я7" not in out
    assert "Методика расчёта теплоснабжения" in out
    assert "УДК 621.3" in out  # упоминание в середине — содержание, оставляем


def test_extract_title_prefers_real_heading():
    assert extract_title("Война и мир\nТом первый\n\nНаташа вышла...") == "Война и мир"
    # «Глава 1» без текста — не заголовок документа
    assert extract_title("Глава 1\n\nТекст главы здесь. Ещё текст.") is None
    # строки-обрывы предложений (с точкой на конце) — не заголовок
    assert extract_title("Это был ясный морозный день. Далее текст. " * 5) is None
    # короткий мусор и страницы пропускаются
    assert extract_title("Страница 1 из 5\nУДК 123\n\nНастоящий заголовок") == "Настоящий заголовок"
    assert extract_title("ab\n\nТекст.") is None


def test_sanitize_filename():
    assert sanitize_filename('Методика: расчет "тепла"/трубы?') == "Методика расчет теплатрубы"
    assert sanitize_filename("A" * 200) == "A" * 120
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("...текст...") == "текст"


def test_rename_by_theme_and_names_map(tmp_path):
    """Переименование по теме: имя = заголовок, карта имён пишется, коллизии
    не затираются, повторный прогон идемпотентен."""
    txts = tmp_path / "txts"
    txts.mkdir()
    (txts / "scan0001.txt").write_text(
        "Методика расчёта теплоснабжения\n\nТекст документа. " * 10, encoding="utf-8"
    )
    (txts / "scan0002.txt").write_text(
        "Методика расчёта теплоснабжения\n\nДругой документ. " * 10, encoding="utf-8"
    )
    (txts / "plain.txt").write_text(
        "Просто текст без заголовка, начинается с предложения. " * 10, encoding="utf-8"
    )

    assert rename_by_theme(txts, threads=4) == 2
    names = sorted(p.name for p in txts.glob("*.txt"))
    assert "Методика расчёта теплоснабжения.txt" in names
    assert "Методика расчёта теплоснабжения (2).txt" in names
    assert "plain.txt" in names  # заголовка нет — имя сохранилось
    names_map = json.loads((txts / "_names_map.json").read_text(encoding="utf-8"))
    assert names_map["scan0001"] == "Методика расчёта теплоснабжения"
    assert names_map["scan0002"] == "Методика расчёта теплоснабжения (2)"

    # второй прогон ничего не переименовывает (уже переименованные пропускаем)
    assert rename_by_theme(txts, threads=4) == 0
    assert sorted(p.name for p in txts.glob("*.txt")) == names


def test_verify_matches_renamed_txt_via_names_map(workspace):
    """ok-запись журнала находит переименованный по теме txt через карту."""
    (workspace.output / "txts").mkdir()
    (workspace.output / "txts" / "scan0001.txt").write_text(
        "Тема документа\n\nСодержание. " * 5, encoding="utf-8"
    )
    rename_by_theme(workspace.output / "txts", threads=2)
    _write_rec(workspace.output, "scan0001.pdf", "ok")

    assert verify_reprocess.cmd_verify(workspace.input, workspace.output) == 0
    state = json.loads((workspace.output / verify_reprocess.STATE_NAME).read_text(encoding="utf-8"))
    assert state["retry"] == [] and state["quarantined"] == {}


def test_to_plain_text_strips_embedded_base64_images():
    """base64-вставки картинок (data-URI или голый блоб) — не текст, вырезаем."""
    blob = "Qk028QUAAAAAADYAAAAoAAAAoAEAAOoAAAABACAAAwAAAADxBQ" * 4  # ~200 символов
    text = (
        "Методика расчёта\n"
        "\n"
        f"data:image/bitmap;base64,{blob}\n"
        f"{blob}\n"
        "Основной текст документа, который должен остаться. "
        f"Рисунок: data:image/png;base64,{blob} конец строки.\n"
    )
    out = to_plain_text(text)
    assert "Qk028QU" not in out
    assert "data:image" not in out
    assert "Методика расчёта" in out
    assert "Основной текст документа" in out
    assert "конец строки" in out


def test_reprocess_overwrites_own_txt_no_dups(workspace):
    """Переработка (verify -> retry) перезаписывает свой txt на месте —
    в том числе под переименованным по теме именем.  Никаких _2 и сирот."""
    from idp.worker import Worker

    make_text_pdf(workspace.input / "report.pdf", "Старый заголовок",
                  "Первая версия текста " * 100)
    _run(Worker())
    txts = workspace.output / "txts"
    assert (txts / "report.txt").is_file()

    # стартовая нормализация второго прогона переименует txt по теме
    make_text_pdf(workspace.input / "report.pdf", "Новый заголовок",
                  "Вторая версия текста " * 100)
    # verify сбросил запись в retry и запомнил владельца txt
    state = {
        "retry": ["report.pdf"],
        "quarantined": {},
        "owners": {"report.pdf": ["report"]},
        "journal_offset": 0,
    }
    (workspace.output / "verify_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    (workspace.output / "results.jsonl").write_text("", encoding="utf-8")
    _run(Worker())

    names = sorted(p.name for p in txts.glob("*.txt"))
    assert names == ["Старый заголовок.txt"], f"плодятся копии/сироты: {names}"
    content = (txts / "Старый заголовок.txt").read_text(encoding="utf-8")
    assert "Вторая версия" in content
    assert "Первая версия" not in content


def test_verify_records_txt_owners_in_state(workspace):
    """verify кладёт владельцев txt (поле txts журнала) в verify_state.json —
    воркер перезапишет эти файлы при переработке, а не создаст _2."""
    make_text_pdf(workspace.input / "lost.pdf", "Документ", "Текст " * 50)
    _write_rec(workspace.output, "lost.pdf", "ok", txts=["lost"])
    (workspace.output / "txts").mkdir(exist_ok=True)

    assert verify_reprocess.cmd_verify(workspace.input, workspace.output) == 0
    state = json.loads((workspace.output / verify_reprocess.STATE_NAME).read_text(encoding="utf-8"))
    assert state["owners"]["lost.pdf"] == ["lost"]
