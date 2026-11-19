"""Reconstructor unit tests: tables, wrapped links, markup preservation."""

from __future__ import annotations

from pathlib import Path

from idp.reconstructor import (
    _join_wrapped_links,
    _table_column_widths,
    build_docx,
    build_pdf,
    parse_bundle,
)


def _pdf_text(path: Path) -> str:
    import fitz

    document = fitz.open(path)
    try:
        return "".join(page.get_text() for page in document)
    finally:
        document.close()


def test_wide_table_pdf_does_not_crash(tmp_path):
    """A many-column table with one very long cell used to raise
    'text box must be finite and not empty' and kill reconstruction."""
    columns = 100
    header = " | ".join(f"К{index}" for index in range(columns))
    body = ["x"] * columns
    body[5] = "очень длинное содержимое ячейки " * 5
    text = "# Таблица по ГИП ВН\n\n" + "\n".join([
        "| " + header + " |",
        "|" + "---|" * columns,
        "| " + " | ".join(["знач"] * columns) + " |",
        "| " + " | ".join(body) + " |",
    ])
    blocks = parse_bundle(text)
    build_pdf(blocks, tmp_path, tmp_path / "reconstruct.pdf")
    build_docx(blocks, tmp_path, tmp_path / "reconstruct.docx")
    assert (tmp_path / "reconstruct.pdf").is_file()
    assert (tmp_path / "reconstruct.docx").is_file()


def test_table_column_widths_fit_page_and_auto_fit():
    available = 595.28 - 2 * 56.7
    rows = [
        ["Параметр", "Значение", "Примечание"],
        ["Масса", "12.5", "кг"],
        ["Длина", "3400", "мм"],
    ]
    widths = _table_column_widths(rows, available)
    assert len(widths) == 3
    assert sum(widths) <= available + 1e-6
    assert all(width > 4 for width in widths), "no degenerate columns"
    # the column with the longest content gets the most space
    assert widths[2] > widths[0] and widths[2] > widths[1]

    wide_rows = [
        ["Наименование объекта поставки", "Кол-во", "Сумма"],
        ["Очень длинное наименование комплекта деталей и узлов", "10", "150000"],
    ]
    wide_widths = _table_column_widths(wide_rows, available)
    assert wide_widths[0] > wide_widths[1] * 2, "content drives width"


def test_table_column_widths_survive_pathological_tables():
    available = 595.28 - 2 * 56.7
    rows = [["x"] * 200, ["y" * 400] + ["z"] * 199]
    widths = _table_column_widths(rows, available)
    assert len(widths) == 200
    assert all(width > 0 for width in widths)


def test_docx_table_columns_are_fixed_and_content_aware(tmp_path):
    build_docx(parse_bundle("| Параметр | Значение |\n|---|---|\n"
                            "| Очень длинное наименование параметра | 12.5 |"),
               tmp_path, tmp_path / "t.docx")
    import docx

    table = docx.Document(str(tmp_path / "t.docx")).tables[0]
    widths = [column.width.cm for column in table.columns]
    assert widths[0] > widths[1], "long-content column must be wider"
    assert sum(widths) <= 17.0 + 0.5
    layout = table._tbl.tblPr.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblLayout"
    )
    assert layout is not None and layout.get(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type"
    ) == "fixed"


def test_unbalanced_bracket_does_not_swallow_headings():
    text = (
        "## Раздел 1. Общие положения\n\n"
        "Схема: [Image: chart | bbox: 100 100 300 300](images/img_0001.png\n"
        "(примечание\n\n"
        "## Раздел 2. Результаты\n\n"
        "Значения уточняются (по согласованию).\n\n"
        "## Раздел 3. Заключение\n"
    )
    joined = _join_wrapped_links(text)
    assert "## Раздел 2. Результаты" in joined
    assert "## Раздел 3. Заключение" in joined
    kinds = [block.kind for block in parse_bundle(joined)]
    assert kinds.count("heading") == 3


def test_wrapped_image_link_is_repaired():
    text = "![Image: chart: график\nвыполнения | bbox: 1 2 3 4](images/\nimg_0001.png)"
    assert _join_wrapped_links(text) == (
        "![Image: chart: график выполнения | bbox: 1 2 3 4](images/img_0001.png)"
    )


def test_intact_links_are_untouched():
    text = "См. [подробнее](https://example.com/a) и [ещё](#section-2)"
    assert _join_wrapped_links(text) == text


def test_parse_bundle_keeps_structure():
    text = (
        "# Заголовок\n\n"
        "Первый абзац\nс продолжением на другой строке.\n\n"
        "- пункт один;\n- пункт два.\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        "<!-- page 2 -->\n\n"
        "## Второй раздел\n\nТекст второй страницы.\n"
    )
    blocks = parse_bundle(text)
    kinds = [block.kind for block in blocks]
    assert kinds == ["heading", "paragraph", "list", "table", "page_break", "heading", "paragraph"]
    paragraph = next(block for block in blocks if block.kind == "paragraph")
    assert paragraph.segments[0].text == "Первый абзац с продолжением на другой строке."
    heading = blocks[0]
    assert heading.segments[0].text == "Заголовок"
    assert heading.level == 1


def test_reconstruction_of_mixed_document(tmp_path):
    text = (
        "# Отчёт\n\n"
        "## Раздел 1\n\n"
        "Схема: ![Image: chart: график | bbox: 120 300 880 700](images/img_0001.png)\n\n"
        "Значение [HANDWRITTEN: 1 250 | bbox: 610 420 760 455](images/handwritten_0001.png) рублей.\n\n"
        "| Параметр | Значение |\n|---|---|\n| Масса | 12.5 |\n"
    )
    blocks = parse_bundle(text)
    build_pdf(blocks, tmp_path, tmp_path / "out.pdf")
    build_docx(blocks, tmp_path, tmp_path / "out.docx")
    pdf_text = _pdf_text(tmp_path / "out.pdf")
    assert "Отчёт" in pdf_text
    assert "Раздел 1" in pdf_text
    assert "12.5" in pdf_text
    assert "Масса" in pdf_text


def test_empty_document_does_not_crash(tmp_path):
    build_pdf(parse_bundle(""), tmp_path, tmp_path / "empty.pdf")
    build_docx(parse_bundle(""), tmp_path, tmp_path / "empty.docx")
    assert (tmp_path / "empty.pdf").is_file()
    assert (tmp_path / "empty.docx").is_file()
