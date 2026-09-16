"""Document type detection based on extension and text heuristics."""

from __future__ import annotations

import re
from pathlib import Path

_DEFAULT_TYPE = "other"

_TYPE_ALIASES = {
    "article": {"article", "paper", "journal", "научная статья", "статья"},
    "report": {"report", "доклад", "presentation", "презентация", "докладчик"},
    "thesis": {"thesis", "диссертация", "диссертаци", "автореферат"},
    "tech_doc": {"tech_doc", "technical", "техническая документация", "ттз", "техзадание", "спецификация", "руководство", "инструкция"},
    "directive": {"directive", "приказ", "указание", "распоряжение", "постановление", "поручение"},
    "certificate": {"certificate", "справка", "выписка", "удостоверение"},
    "summary": {"summary", "сводка", "отчет", "отчёт", "итог", "дайджест"},
    "review": {"review", "обзор", "state-of-the-art", "сравнение", "тест", "обзор литературы"},
    "textbook": {"textbook", "учебник", "пособие", "методичка", "курс лекций"},
    "ttkh": {"ttkh", "ттх", "тактико-технические характеристики", "характеристики"},
    "ttz": {"ttz", "ттз", "техническое задание", "техзадание"},
}

_EXT_TYPE = {
    ".pdf": None,
    ".docx": None,
    ".pptx": "report",
    ".html": None,
    ".htm": None,
}

_KEYWORD_HINTS = {
    "article": re.compile(r"(?:abstract|introduction|methods|results|discussion|references|аннотация|введение|методы|результаты|обсуждение|список литературы)", re.IGNORECASE),
    "report": re.compile(r"(?:slide|слайд|конференц|семинар|цель работы|методы|выводы|итоги)", re.IGNORECASE),
    "thesis": re.compile(r"(?:диссертация|автореферат|кандидат|доктор|глава\s+\d+|введение|обзор литературы|методика|результаты|заключение|список литературы)", re.IGNORECASE),
    "tech_doc": re.compile(r"(?:требования|функционал|нефункционал|спецификация|инструкция|руководство оператора|реvision|редакция|действует)", re.IGNORECASE),
    "directive": re.compile(r"(?:приказываю|указываю|поручаю|обязываю|рекомендую|в соответствии с|на основании|срок исполнения|контроль оставляю)", re.IGNORECASE),
    "certificate": re.compile(r"(?:справка|выдана|№\s*[A-Za-z0-9]+|от\s+\d{2}\.\d{2}\.\d{4}|имеет|не имеет|по состоянию на|за период)", re.IGNORECASE),
    "summary": re.compile(r"(?:сводка|итого|всего|по состоянию на|дтп|пожар|происшествие|статистика|за\s+\d+\s+(?:час|сутки|день|месяц))", re.IGNORECASE),
    "review": re.compile(r"(?:плюсы|минусы|итог|вердикт|сравнение|в отличие|по сравнению|оценка|рейтинг)", re.IGNORECASE),
    "textbook": re.compile(r"(?:определение|теорема|пример\s+\d+|задача\s+\d+|решение|глава\s+\d+|параграф\s+\d+|§\s*\d+|\n\n[0-9]+(?:\.[0-9]+)*\s+)", re.IGNORECASE),
    "ttkh": re.compile(r"(?:тактико-технические характеристики|ттх|масса|габариты|экипаж|вооружение|дальность|скорость|запас хода|двигатель)", re.IGNORECASE),
    "ttz": re.compile(r"(?:техническое задание|ттз|должно обеспечивать|не должно превышать|в диапазоне от|вероятность не менее|требования|пункт\s+\d+(?:\.\d+)*)", re.IGNORECASE),
}


def detect_document_type(path: Path, text: str = "") -> str:
    ext = path.suffix.lower()
    if ext in _EXT_TYPE:
        pass
    text_sample = text[:4000] if text else ""
    scores: dict[str, int] = {}
    for dtype, pattern in _KEYWORD_HINTS.items():
        if pattern.search(text_sample):
            scores[dtype] = scores.get(dtype, 0) + 1
    if scores:
        return max(scores, key=scores.get)
    return _DEFAULT_TYPE
