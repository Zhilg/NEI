"""Builders for sample documents used by the offline test suite."""

from __future__ import annotations

from pathlib import Path


def make_pdf(path: Path, pages: int = 3, with_image: bool = True) -> Path:
    """A multi-page text PDF with headings, paragraphs, lists and a table."""
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for page_number in range(1, pages + 1):
        page = doc.new_page(width=595.28, height=841.89)
        y = 72.0
        title = f"Отчёт о выполненных работах (страница {page_number})"
        page.insert_text((72, y), title, fontsize=16, fontname="hebo")
        y += 30
        if page_number % 2 == 1:
            page.insert_text((72, y), "Раздел 1. Общие положения", fontsize=13, fontname="hebo")
            y += 24
            body = (
                "Настоящий документ описывает объём выполненных работ за отчётный период "
                "и содержит сводные данные по объекту. Значение параметра уточняется вручную."
            )
            page.insert_textbox(fitz.Rect(72, y, 523, y + 120), body, fontsize=11)
            y += 130
        if page_number % 3 == 0:
            rows = 4
            cols = 3
            row_height = 24.0
            col_width = 150.0
            table = fitz.Rect(72, y, 72 + col_width * cols, y + row_height * rows)
            page.draw_rect(table, color=(0, 0, 0), width=0.7)
            for row in range(1, rows):
                page.draw_line((table.x0, table.y0 + row * row_height),
                               (table.x1, table.y0 + row * row_height), width=0.5)
            for col in range(1, cols):
                page.draw_line((table.x0 + col * col_width, table.y0),
                               (table.x0 + col * col_width, table.y1), width=0.5)
            headers = ["Параметр", "Значение", "Ед. изм."]
            for col, header in enumerate(headers):
                page.insert_text((table.x0 + col * col_width + 4, table.y0 + 16),
                                 header, fontsize=10, fontname="hebo")
            values = [("Масса", "12.5", "кг"), ("Длина", "3400", "мм")]
            for row, (name, value, unit) in enumerate(values, start=1):
                for col, cell in enumerate((name, value, unit)):
                    page.insert_text((table.x0 + col * col_width + 4,
                                      table.y0 + row * row_height + 16), cell, fontsize=10)
            y = table.y1 + 30
        if with_image and page_number == 1:
            # a chart-like block the VLM must describe as [Image: ...]
            chart = fitz.Rect(120, 560, 480, 740)
            page.draw_rect(chart, color=(0.1, 0.3, 0.7), fill=(0.9, 0.95, 1.0), width=1)
            for index in range(1, 6):
                height = 20 * index
                page.draw_rect(fitz.Rect(140 + index * 60, chart.y1 - 20 - height,
                                         170 + index * 60, chart.y1 - 20),
                               color=None, fill=(0.2, 0.4, 0.8))
            page.insert_text((chart.x0, chart.y0 + 14), "График выполнения", fontsize=11)
        if page_number == pages:
            page.insert_text((400, 800), "И.И. Иванов", fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def make_docx(path: Path) -> Path:
    """A DOCX with headings, a list, a table and an inline image."""
    from docx import Document
    from docx.shared import Cm

    document = Document()
    document.add_heading("Техническое задание", level=0)
    document.add_heading("1. Назначение", level=1)
    document.add_paragraph(
        "Документ определяет требования к изделию и порядок приёмки. "
        "Срок действия — 12 месяцев со дня подписания."
    )
    document.add_heading("2. Требования", level=2)
    for item in ("первое требование;", "второе требование."):
        document.add_paragraph(item, style="List Bullet")
    table = document.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    headers = ("Параметр", "Значение", "Ед. изм.")
    for column, header in enumerate(headers):
        table.cell(0, column).text = header
        for paragraph in table.cell(0, column).paragraphs:
            for run in paragraph.runs:
                run.bold = True
    rows = (("Масса", "12.5", "кг"), ("Длина", "3400", "мм"))
    for row_index, row in enumerate(rows, start=1):
        for column, value in enumerate(row):
            table.cell(row_index, column).text = value
    document.add_heading("3. Приложение", level=1)
    paragraph = document.add_paragraph()
    paragraph.add_run().add_picture(str(make_png(path.with_name("figure.png"))), width=Cm(8))
    document.add_paragraph("Подпись: И.И. Иванов")
    document.save(str(path))
    return path


def make_png(path: Path, width: int = 400, height: int = 200) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(image)
    for index in range(1, 6):
        bar_height = 20 * index
        draw.rectangle([30 + index * 60, height - 20 - bar_height,
                        90 + index * 60, height - 20], fill=(40, 90, 200))
    draw.text((20, 20), "График выполнения", fill=(20, 20, 20))
    image.save(path)
    return path


def make_html(path: Path) -> Path:
    html = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Служебная записка</title></head>
<body>
<h1>Служебная записка</h1>
<h2>1. Сведения</h2>
<p>Текст записки с упоминанием <b>И.И. Иванова</b> и суммы 150 000 рублей.</p>
<table><tr><th>Параметр</th><th>Значение</th></tr>
<tr><td>Масса</td><td>12.5 кг</td></tr></table>
<p><img src="figure.png" alt="График выполнения"></p>
</body></html>
"""
    path.write_text(html, encoding="utf-8")
    make_png(path.with_name("figure.png"))
    return path


def make_pptx(path: Path) -> Path:
    from pptx import Presentation

    presentation = Presentation()
    for index in range(1, 3):
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = f"Слайд {index}"
        body = slide.placeholders[1].text_frame
        body.text = f"Содержание слайда {index}: показатели за период."
    presentation.save(str(path))
    return path
