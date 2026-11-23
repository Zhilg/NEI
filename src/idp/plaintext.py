"""Приведение любого извлечённого текста к чистому plain text.

Корпус должен содержать «просто нормальные текстовые файлы»: абзацы, заголовки
строками, списки.  Никакой markdown-разметки, HTML-комментариев, сущностей,
разделителей таблиц, разметки ссылок/картинок, символов форматной подачи.

Используется в двух местах:

* worker — текст после извлечения/реконструкции нормализуется до записи
  в результаты и txt;
* scripts/fix_results.py — та же нормализация применяется к уже накопленным
  результатам.

Модуль зависит только от stdlib — его можно загрузить напрямую по пути файла,
минуя пакет idp (важно для скриптов вне контейнера).
"""

from __future__ import annotations

import html
import re

# Строка целиком — HTML/служебный комментарий (<!-- file: ... -->, <!-- page 2 -->,
# <!-- bbox ... --> и любые другие).
_COMMENT_LINE_RE = re.compile(r"^\s*<!--.*?-->\s*$")
# Строка-разделитель таблицы (| --- | :---: |) или горизонтальное правило.
_TABLE_HR_RE = re.compile(r"^[\s\-:|]+$")
# Заголовок markdown (# ... ###### ...).
_HEADER_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
# Цитата.
_QUOTE_RE = re.compile(r"^>\s?")
# Картинка ![alt](src) -> alt; ссылка [text](url) -> text.
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Жирный/курсив/моноширинный (в том числе внутри слов не трогаем окружение).
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_CODE_RE = re.compile(r"`([^`]+)`")
# Одиночные * и _ вокруг словосочетаний — самые осторожные паттерны последними.
_EM_STAR_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_EM_UNDER_RE = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
# Маркер нумерованного списка сохраняем, но приводим пробелы.
_NUM_LIST_RE = re.compile(r"^(\d{1,3})[.)]\s+")


def _flatten_line(s: str) -> str:
    # Таблица: | a | b | -> "a  b" (строки-разделители отсеяны выше).
    if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
        cells = [c.strip() for c in s.strip("|").split("|")]
        cells = [c for c in cells if c and not re.fullmatch(r":?-+:?", c)]
        if cells:
            s = "  ".join(cells)
        else:
            return ""
    s = _HEADER_RE.sub(r"\1", s) if _HEADER_RE.match(s) else s
    s = _QUOTE_RE.sub("", s)
    s = _IMAGE_RE.sub(r"\1", s)
    s = _LINK_RE.sub(r"\1", s)
    s = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2), s)
    s = _CODE_RE.sub(r"\1", s)
    s = _EM_STAR_RE.sub(r"\1", s)
    s = _EM_UNDER_RE.sub(r"\1", s)
    s = _NUM_LIST_RE.sub(r"\1. ", s)
    return html.unescape(s).strip()


def to_plain_text(text: str) -> str:
    """Привести markdown/HTML-мусор к чистому plain text."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    out: list[str] = []
    in_code_fence = False
    for raw in text.split("\n"):
        s = raw.strip()
        if s.startswith("```"):
            in_code_fence = not in_code_fence
            continue  # сами ограждения кода выбрасываем, содержимое оставляем
        if not s:
            out.append("")
            continue
        if _COMMENT_LINE_RE.match(s):
            continue
        if not in_code_fence and _TABLE_HR_RE.match(s) and "-" in s:
            continue
        if in_code_fence:
            out.append(html.unescape(raw.rstrip()))
            continue
        flat = _flatten_line(s)
        out.append(flat if flat else "")
    # Схлопываем серии пустых строк до одной, режем края.
    collapsed: list[str] = []
    blank = False
    for ln in out:
        if ln:
            collapsed.append(ln)
            blank = False
        elif not blank:
            collapsed.append("")
            blank = True
    return "\n".join(collapsed).strip("\n")
