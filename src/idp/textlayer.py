"""Извлечение текстового слоя PDF — только если он чистый.

Слои в этом корпусе часто «отравлены» (битая кодировка cp1251→latin1,
OCR-мусор, псевдографика) — такие файлы должны идти через VLM-OCR.  Чистые
born-digital слои забираем сразу: секунды CPU вместо минут на GPU.

Эвристика намеренно консервативна: при любом намёке на мусор возвращаем
None (файл уходит в VLM).  Лучше лишний VLM-прогон, чем мусор в корпусе.
"""

from __future__ import annotations

import logging
from pathlib import Path

from idp.renderer import open_pdf

logger = logging.getLogger(__name__)

# Минимум содержимого: совсем короткий слой (обложки, картинки) не даём —
# такой файл осмысленнее прогнать через VLM.
_MIN_TOTAL_CHARS = 1000
_MIN_CHARS_PER_PAGE = 250
# Доля «алфавитных» символов — защита от слоёв-таблиц и псевдографики.
_MIN_ALPHA_RATIO = 0.4
# Порог мусорных символов (replacement/control/псевдографика).
_MAX_REPLACEMENT_CHARS = 0
_MAX_CONTROL_RATIO = 0.001
_MAX_PSEUDOGRAPH_RATIO = 0.005
# Слова неразумной длины — типичный признак OCR-перекоса.
_MAX_LONG_WORD_RATIO = 0.001
_LONG_WORD = 40
# Средняя длина слова (с пробелами) вне разумного диапазона.
_AVG_WORD_MIN = 3.0
_AVG_WORD_MAX = 12.0
# Битая кодировка: заметная non-ASCII латиница при почти полном отсутствии
# кириллицы (корпус русскоязычный).  Немецкие/французские тексты при таком
# пороге уйдут в VLM — осознанная жертва консервативности.
_MAX_CYRILLIC_RATIO = 0.05
_MIN_DIACRITIC_LATIN_RATIO = 0.005

_PSEUDOGRAPH = "█▓▒░│┃┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬"


def _is_cyrillic(ch: str) -> bool:
    return "\u0400" <= ch <= "\u04FF" or ch == "ё" or ch == "Ё"


def looks_like_clean_text(text: str, page_count: int) -> bool:
    """Эвристика чистоты текстового слоя.  Любое сомнение -> False (VLM)."""
    t = text.strip()
    if page_count <= 0 or len(t) < _MIN_TOTAL_CHARS:
        return False
    if len(t) / page_count < _MIN_CHARS_PER_PAGE:
        return False
    n = len(t)
    if t.count("\ufffd") > _MAX_REPLACEMENT_CHARS:
        return False
    bad_ctrl = sum(1 for c in t if ord(c) < 32 and c not in "\n\r\t\f")
    if bad_ctrl / n > _MAX_CONTROL_RATIO:
        return False
    pseudo = sum(t.count(ch) for ch in _PSEUDOGRAPH)
    if pseudo / n > _MAX_PSEUDOGRAPH_RATIO:
        return False
    alpha = sum(1 for c in t if c.isalpha())
    if alpha / n < _MIN_ALPHA_RATIO:
        return False
    words = t.split()
    if not words:
        return False
    long_words = sum(1 for w in words if len(w) > _LONG_WORD)
    if long_words / len(words) > _MAX_LONG_WORD_RATIO:
        return False
    avg = n / len(words)
    if not (_AVG_WORD_MIN <= avg <= _AVG_WORD_MAX):
        return False
    cyrillic = sum(1 for c in t if _is_cyrillic(c))
    if cyrillic / n >= _MAX_CYRILLIC_RATIO:
        return True
    # кириллицы почти нет — либо иностранный текст, либо битая кодировка.
    # Различаем по non-ASCII латинице (диакритика) — её много только
    # при cp1251-перекосе вида «Òàêñè».
    diacritic_latin = sum(
        1 for c in t if c.isalpha() and not c.isascii() and not _is_cyrillic(c)
    )
    return diacritic_latin / n < _MIN_DIACRITIC_LATIN_RATIO


def extract_clean_text_layer(pdf_path: Path) -> tuple[str, int] | None:
    """(text, page_count) чистого текстового слоя, либо None -> идти в VLM.

    Никогда не бросает исключений: битые файлы просто не подходят под
    быстрый путь и обрабатываются через VLM/ремонт дальше по пайплайну.
    """
    try:
        doc = open_pdf(pdf_path)
    except Exception:  # noqa: BLE001 — непочинимые fitz/pikepdf идут в VLM-путь
        return None
    try:
        try:
            page_count = doc.page_count
        except Exception:  # noqa: BLE001 — битый page tree
            return None
        if page_count <= 0:
            return None
        parts: list[str] = []
        try:
            for page in doc:
                parts.append(page.get_text())
        except Exception:  # noqa: BLE001 — одна битая страница не решает за файл,
            return None    # но слой всё равно считаем недоверенным
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    text = "\n".join(parts)
    if not looks_like_clean_text(text, page_count):
        return None
    logger.debug("PDF %s: чистый текстовый слой (%d стр., %d знаков)",
                 pdf_path.name, page_count, len(text))
    return text, page_count
