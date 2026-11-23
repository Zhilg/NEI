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
# Колонтитул-номер страницы: «Страница 5 из 12», «Стр. 3», «Page 7 of 90»,
# а также виды «— 12 —» / «- 12 -».
_PAGE_MARK_RE = re.compile(
    r"^(?:страница|стр\.?|page)\s+\d{1,4}(?:\s+(?:из|of)\s+\d{1,4})?[.\s]*$",
    re.IGNORECASE,
)
_PAGE_DASH_RE = re.compile(r"^[—–―-]\s*\d{1,4}\s*[—–―-]$")
# Заголовок оглавления.
_TOC_HEADING_RE = re.compile(r"^(?:оглавление|содержание)[:\s]*$", re.IGNORECASE)
# Строка оглавления: «Название главы ....... 12» — название, лидеры
# (точки/многоточия/тире/пробелы) и номер страницы.
_TOC_ENTRY_RE = re.compile(
    r"^.{2,100}?[.\s]*(?:\.{2,}|…+|·{2,}|‒{2,}|–{2,}|—{2,}|-{2,}| {3,})\s*\d{1,4}\s*$"
)
# Начало самостоятельного произведения — признак сборника (объединять НЕ надо).
_ARTICLE_MARKER_RE = re.compile(
    r"^(?:рассказ|повесть|новелла|очерк|статья|эссе|стихи|стихотворение|"
    r"проза|поэзия|публицистика|интервью|заметка|документ|критика|"
    r"рецензия|фельетон|корреспонденция|хроника|сказка|басня|пьеса|драма)\b",
    re.IGNORECASE,
)
# Начало главы/части — признак одной книги, разбитой на главы (объединять надо).
_CHAPTER_START_RE = re.compile(
    r"^(?:глава|часть|раздел|отдел(?:ение)?|том|книга|параграф|предисловие|"
    r"введение|заключение|послесловие|приложение|эпилог|пролог)\b"
    r"|^[IVXLC]+\.\s"
    r"|^\d{1,3}[.):]\s+\S",
    re.IGNORECASE,
)
# VLM-аннотации визуалов: «[Image: stamp: Circular blue ink stamp...]» —
# содержимого текста нет, выкидываем; «[HANDWRITTEN: 17]» — распаковываем,
# внутри текст.
_IMAGE_ANN_LINE_RE = re.compile(r"^\[Image:[^\]\n]*\]\s*$", re.IGNORECASE)
_IMAGE_ANN_INLINE_RE = re.compile(r"\[Image:[^\]\n]*\]", re.IGNORECASE)
_HANDWRITTEN_RE = re.compile(r"\[HANDWRITTEN:\s*([^\]\n]*)\]", re.IGNORECASE)
# Правовые БД: шапки/колонтитулы КонсультантПлюс и Гарант.
_LEGAL_BOILERPLATE_RES = [
    re.compile(r"^консультантплюс\s*$", re.IGNORECASE),
    re.compile(r"^документ предоставлен консультантплюс", re.IGNORECASE),
    re.compile(r"^дата сохранения:", re.IGNORECASE),
    re.compile(r"^над[её]жная правовая поддержка", re.IGNORECASE),
    re.compile(r"^www\.(consultant|garant)\.(ru|com)", re.IGNORECASE),
    re.compile(r"^гарант\s*:\s*$", re.IGNORECASE),
    re.compile(r"^см\. энциклопедии", re.IGNORECASE),
    re.compile(r"^информация об изменениях\s*:\s*$", re.IGNORECASE),
    re.compile(r"^см\. текст (пункта|наименования|статьи)", re.IGNORECASE),
]
# Перенос слова через дефис/мягкий дефис на следующей строке («Пери-\нод»).
_DEHYPHEN_RE = re.compile(r"(?<![-\s])[-\u00ad]\n(?=[а-яёa-z])")
# Конец предложения / конструкции, после которой нужен разрыв строки.
_SENTENCE_END_RE = re.compile(r"[.!?…:;»)\]]$")
# Маркер списка — строку со списком не приклеиваем к предыдущей.
_LIST_START_RE = re.compile(r"^(?:[-*•‣]|\d{1,3}[.)])\s")


def _reflow(lines: list[str]) -> list[str]:
    """Склеить разорванные строки в абзацы.

    В узкоколоночных PDF выгрузках каждая строка обрывается по ширине
    колонки («Один / из / основоположников»), а не по смыслу.  Склеиваем,
    пока предыдущая строка не заканчивается концом предложения.  Осторожные
    исключения, когда склейку НЕ делаем: перед заголовком главы, маркером
    жанра, пунктом списка, строкой В ВЕРХНЕМ РЕГИСТРЕ, а также рядом со
    строками таблиц (двойные пробелы после расплющивания |...|) — чтобы не
    склеивать строки стихов и табличные данные.
    """
    out: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append(buf)
            buf = ""

    for ln in lines:
        s = ln.strip()
        if not s:
            flush()
            if out and out[-1] != "":
                out.append("")
            continue
        if not buf:
            buf = s
            continue
        if (
            _SENTENCE_END_RE.search(buf)
            or "  " in buf
            or "  " in s
            or _CHAPTER_START_RE.match(s)
            or _ARTICLE_MARKER_RE.match(s)
            or _LIST_START_RE.match(s)
            or (_LIST_START_RE.match(buf) and s[:1].isupper())
            or (len(s) >= 4 and s.isupper())
            or (len(buf) >= 4 and buf.isupper())
        ):
            flush()
            buf = s
            continue
        buf = f"{buf} {s}"
    flush()
    return out
# Заголовок оглавления и строка оглавления определены выше
# (_TOC_HEADING_RE / _TOC_ENTRY_RE).


def strip_toc(text: str) -> str:
    """Вырезать блоки оглавления («Оглавление»/«Содержание» + строки глав с
    номерами страниц).  Срабатывает только когда после заголовка идут минимум
    две похожие на пункты оглавления строки — чтобы не съесть обычный текст,
    где слово «содержание» стоит отдельной строкой."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _TOC_HEADING_RE.match(lines[i].strip()):
            j = i + 1
            entries = 0
            while j < n:
                s = lines[j].strip()
                if not s:
                    j += 1
                    continue
                if _TOC_ENTRY_RE.match(s):
                    entries += 1
                    j += 1
                    continue
                break
            if entries >= 2:
                i = j  # заголовок + пункты выкидываем целиком
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


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
    # Склейка перенесённых слов («Пери-\нод» -> «Период») — до построчной
    # обработки, иначе части слова пойдут разными строками.
    text = _DEHYPHEN_RE.sub("", text)
    # HANDWRITTEN-обёртки: внутри текст, наружу его и выпускаем.
    text = _HANDWRITTEN_RE.sub(r"\1", text)
    # Image-аннотации в середине строки — без содержимого, вырезаем.
    text = _IMAGE_ANN_INLINE_RE.sub("", text)
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
        if _IMAGE_ANN_LINE_RE.match(s):
            continue
        if not in_code_fence and _PAGE_MARK_RE.match(s):
            continue  # «Страница 5 из 12», «Стр. 3»
        if not in_code_fence and _PAGE_DASH_RE.match(s):
            continue  # «— 12 —»
        if not in_code_fence and _TABLE_HR_RE.match(s) and "-" in s:
            continue
        if not in_code_fence and any(r.match(s) for r in _LEGAL_BOILERPLATE_RES):
            continue  # шапки КонсультантПлюс/Гарант
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
    text = "\n".join(collapsed).strip("\n")
    # Оглавления — не содержание, вырезаем блоки целиком.
    text = strip_toc(text)
    # Склейка разорванных колонкой строк в абзацы.
    reflowed = _reflow(text.split("\n"))
    # Схлопываем серии пустых строк до одной, режем края.
    final: list[str] = []
    blank = False
    for ln in reflowed:
        if ln:
            final.append(ln)
            blank = False
        elif not blank:
            final.append("")
            blank = True
    return "\n".join(final).strip("\n")


def _first_nonempty_line(text: str) -> str:
    for ln in text.split("\n"):
        if ln.strip():
            return ln.strip()
    return ""


def plan_merge(parts: list[str]) -> bool:
    """Решить, нужно ли объединять части разбитого документа в один файл.

    Разбивка на ``_2``, ``_3`` ... правильна только для газет и сборников
    статей/рассказов.  Книга, расколотая на главы (из-за эвристики
    коллекций), должна лежать одним файлом.

    Правила:
    * у двух и более частей первая строка — маркер жанра (Рассказ, Статья,
      Очерк...) -> это сборник, НЕ объединяем;
    * большинство частей начинается с «Глава/Часть/...» -> это одна книга,
      объединяем;
    * иначе: длинные сплошные тексты -> книга (объединяем), короткие
      самостоятельные куски -> оставляем как есть.
    """
    texts = [p.strip() for p in parts if p and p.strip()]
    if len(texts) < 2:
        return False
    starts = [_first_nonempty_line(t) for t in texts[1:]]
    starts = [s for s in starts if s]
    if not starts:
        return True
    genre = sum(1 for s in starts if _ARTICLE_MARKER_RE.match(s))
    if genre >= 2:
        return False  # сборник статей/рассказов
    chapter = sum(1 for s in starts if _CHAPTER_START_RE.match(s))
    if chapter >= max(1, round(0.5 * len(starts))):
        return True  # книга с главами
    avg = sum(len(t) for t in texts) // len(texts)
    return avg > 1500


def merge_parts(parts: list[str]) -> str:
    """Склеить части в один текст (части уже в порядке следования)."""
    texts = [p.strip() for p in parts if p and p.strip()]
    return "\n\n".join(texts)
