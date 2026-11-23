"""Эмуляция реальных болевых случаев корпуса: мелкий шрифт, две колонки,
сноски, битые сканы, повёрнутые страницы, 'лесенка' колонок.

VLM локально не эмулируется — эти тесты проверяют всё, что проверяемо
детерминированно: геометрию рендера (фактическое разрешение, которое видит
модель), устойчивость пайплайна к битым файлам, классификацию (текстовый
слой/скан) и пост-обработку 'лесенки' на синтетическом OCR-выводе.
"""

from __future__ import annotations

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
from tests.test_worker import (  # noqa: E402
    _FONT,
    _read_results,
    _run,
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
    чужой txt (экспортер пустой после рестарта).
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
    assert "usermanual.txt" in txts, f"первый файл потерялся: {sorted(txts)}"
    assert "usermanual_2.txt" in txts, f"второй файл перезаписал первый: {sorted(txts)}"
    assert "Первый файл" in txts["usermanual.txt"]
    assert "Второй файл" in txts["usermanual_2.txt"]


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
