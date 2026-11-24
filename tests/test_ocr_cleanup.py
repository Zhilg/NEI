from __future__ import annotations

import json
import sys
from pathlib import Path

from idp.plaintext import extract_title, to_plain_text
from idp.txt_normalize import rename_by_theme

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from requeue_bad_ocr import is_obviously_bad_ocr, requeue_bad_ocr  # noqa: E402


BAD_OCR = (
    "Ш ш ■ с - шш QrhTem по (&евщ tit)У ЗШ & f> j* п-\n"
    "щ / . I а жр*т тжшят еша*** m трекеа* ? Отара&го\n"
    "а е еат* аегерт аажедаееь ^рѵшш$яя& ш ш&мьт sm\n"
) * 45


def test_strip_ministry_header_and_keep_body():
    text = (
        "Министерство обороны Российской Федерации\n"
        "Федеральное государственное казенное военное образовательное учреждение\n"
        "«РЯЗАНСКОЕ ГВАРДЕЙСКОЕ ВЫСШЕЕ ВОЗДУШНО-ДЕСАНТНОЕ УЧИЛИЩЕ»\n"
        "Экз. № _ Рег. № НИОКТР__________\n"
        "СОГЛАСОВАНО\n"
        "Основы тактической подготовки\n\n"
        "Это основной текст документа, который должен остаться."
    )
    out = to_plain_text(text)
    assert "Министерство обороны" not in out
    assert "Рег. №" not in out
    assert "СОГЛАСОВАНО" not in out
    assert "Основы тактической подготовки" in out
    assert "основной текст документа" in out


def test_strip_title_bibliography_annotation_and_toc():
    text = (
        "«ОСНОВЫ СИСТЕМЫ МЕНЕДЖМЕНТА КАЧЕСТВА»\n"
        "Учебное пособие Санкт-Петербург 2016\n"
        "А.В. Боговик. Учеб. пособие. – СПб.: ВАС, 2016. – 399 с.\n"
        "В учебном пособии излагаются основы и вопросы моделирования систем.\n"
        "Ил. Табл. Библиогр.: назв. © ВАС 2016\n"
        "ОГЛАВЛЕНИЕ\n"
        "ОСНОВНЫЕ СОКРАЩЕНИЯ И ОБОЗНАЧЕНИЯ……………………….\n"
        "ПРЕДИСЛОВИЕ…………………………………………………………….. 5\n"
        "1. ТЕОРЕТИЧЕСКИЕ ОСНОВЫ УПРАВЛЕНИЯ………………… 12\n"
        "ПРЕДИСЛОВИЕ\n\n"
        "Это начало основного текста после титульного блока."
    )
    out = to_plain_text(text)
    assert "Боговик" not in out
    assert "В учебном пособии излагаются" not in out
    assert "ОГЛАВЛЕНИЕ" not in out
    assert "ТЕОРЕТИЧЕСКИЕ ОСНОВЫ" not in out
    assert out.startswith("ПРЕДИСЛОВИЕ")
    assert "начало основного текста" in out


def test_strip_periodical_header_with_soft_hyphens():
    text = (
        "«За\u00adру\u00adбеж\u00adное во\u00adен\u00adное обо\u00adзре\u00adние» № 1/2023\n"
        "ISSN 0134-921X\nАРУБЕЖНОЕ\nОЕННОЕ\nБОЗРЕНИЕ\n"
        "АРУБЕЖНОЕ\nОЕННОЕ\nБОЗРЕНИЕ\nЗ В О\n1. 2023\nЗ В О\n1. 2023\n"
        "Турецкие вооружённые силы\n\nОсновной текст статьи."
    )
    out = to_plain_text(text)
    assert "ISSN" not in out
    assert "БОЗРЕНИЕ" not in out
    assert "З В О" not in out
    assert "1. 2023" not in out
    assert "Турецкие вооружённые силы" in out
    assert "Основной текст статьи" in out


def test_bad_titles_rejected_and_real_title_kept():
    assert extract_title("ШшШ ■ с - шш QrhTem\n\nэто основной текст документа. Далее текст.") is None
    assert extract_title("новостной обрывок без заголовка\n\nэто основной текст. Далее текст.") is None
    assert extract_title("Основы системы менеджмента качества\n\nТекст.") == (
        "Основы системы менеджмента качества"
    )


def test_bad_automatic_names_fixed_or_restored(tmp_path):
    txts = tmp_path / "txts"
    txts.mkdir()
    (txts / "ШшШ ■ с - шш QrhTem.txt").write_text(
        "это обычный основной текст без заголовка. " * 5, encoding="utf-8",
    )
    (txts / "old garbage.txt").write_text(
        "Правильное название документа\n\nОсновной текст.", encoding="utf-8",
    )
    (txts / "Хороший заголовок.txt").write_text(
        "текст без нового заголовка. " * 5, encoding="utf-8",
    )
    (txts / "_names_map.json").write_text(json.dumps({
        "source1": "ШшШ ■ с - шш QrhTem",
        "source2": "old garbage",
        "source3": "Хороший заголовок",
    }), encoding="utf-8")

    assert rename_by_theme(txts, threads=2) == 2
    assert (txts / "source1.txt").is_file()
    assert (txts / "Правильное название документа.txt").is_file()
    assert (txts / "Хороший заголовок.txt").is_file()
    mapping = json.loads((txts / "_names_map.json").read_text(encoding="utf-8"))
    assert mapping["source1"] == "source1"
    assert mapping["source2"] == "Правильное название документа"
    assert mapping["source3"] == "Хороший заголовок"


def test_bad_ocr_detector_is_conservative():
    assert is_obviously_bad_ocr(BAD_OCR)
    normal = (
        "Это основной текст документа, в котором излагаются основы системы "
        "управления качеством и описываются методы исследования. "
    ) * 150
    table = "1 Втулка ГОСТ 1245 12 Сталь 0.25\n2 Кронштейн СБ 4 Алюминий 1.75\n" * 150
    assert not is_obviously_bad_ocr(normal)
    assert not is_obviously_bad_ocr(table)
    assert not is_obviously_bad_ocr("Короткий текст с пометкой ■ не перерабатываем")


def test_requeue_deletes_only_matched_bad_txt_and_journal_records(tmp_path):
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    txts = output_root / "txts"
    input_root.mkdir()
    txts.mkdir(parents=True)
    (input_root / "scan1.pdf").write_bytes(b"%PDF-1.4 source")
    (input_root / "scan2.pdf").write_bytes(b"%PDF-1.4 source")
    (txts / "broken OCR.txt").write_text(BAD_OCR, encoding="utf-8")
    (txts / "unmatched.txt").write_text(BAD_OCR, encoding="utf-8")
    (txts / "normal.txt").write_text("Нормальный заголовок\n\nОсновной текст.", encoding="utf-8")
    (txts / "_names_map.json").write_text(
        json.dumps({"scan1": "broken OCR", "scan2": "normal"}), encoding="utf-8",
    )
    records = [
        {"file": "scan1.pdf", "status": "ok", "txts": ["scan1"]},
        {"file": "scan2.pdf", "status": "ok", "txts": ["scan2"]},
    ]
    (output_root / "results.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )

    assert requeue_bad_ocr(input_root, output_root, threads=2) == (1, 1, 0)
    assert not (txts / "broken OCR.txt").exists()
    assert (txts / "unmatched.txt").is_file()
    assert (txts / "normal.txt").is_file()
    remaining = [
        json.loads(line)
        for line in (output_root / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["file"] for record in remaining] == ["scan2.pdf"]
