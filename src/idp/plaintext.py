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
# Шапка научного документа: «УДК 338.245», «ГРНТИ 06.58.01», «ББК 22.1я7» —
# классификационные коды в первых строках, к содержанию отношения не имеют.
_UDK_HEADER_RE = re.compile(r"^(?:удк|грнти|ббк|мрти|okso)\s*[:№]?\s*\S", re.IGNORECASE)
# Заголовок списка источников (всё от него до конца документа — не основной текст).
_REFS_HEADING_RE = re.compile(
    r"^(?:список использованных (?:источников|литературы)|список литературы|"
    r"использованные источники|литература|библиограф(?:ия|ический список)|"
    r"references|bibliography)[.\s]*$",
    re.IGNORECASE,
)
# Строка, похожая на записи источников: «12. Иванов И.И. ...», «[4] ...», «— М., 1985 ...».
_REF_ENTRY_START_RE = re.compile(r"^\s*(?:\d{1,3}[.)]\s|\[\d{1,3}\]\s*|[—–-]\s+[А-ЯA-Z])")
# Встроенные картинки/вложения, снятые текстовым слоем как текст: data-URI
# целиком («data:image/bitmap;base64,Qk02...») или голый base64-блоб одной
# строкой (вложения FB2/DOCX, шрифты, картинки PDF) — содержимого текста нет.
_BASE64_DATAURI_RE = re.compile(
    r"data:image/[a-z0-9.+-]*;base64,[a-z0-9+/=\s]+", re.IGNORECASE
)
_BASE64_BLOB_LINE_RE = re.compile(r"^[a-z0-9+/]{120,}={0,3}$", re.IGNORECASE)
# Перенос слова через дефис/мягкий дефис на следующей строке («Пери-\нод»).
_DEHYPHEN_RE = re.compile(r"(?<![-\s])[-\u00ad]\n(?=[а-яёa-z])")
# Конец предложения / конструкции, после которой нужен разрыв строки.
_SENTENCE_END_RE = re.compile(r"[.!?…:;»)\]]$")
# Маркер списка — строку со списком не приклеиваем к предыдущей.
_LIST_START_RE = re.compile(r"^(?:[-*•‣]|\d{1,3}[.)])\s")
# Типовая шапка стандарта — только в начале документа. Упоминания ГОСТ внутри
# содержательного текста этот фильтр не затрагивает.
_COVER_BOILERPLATE_RE = re.compile(
    r"^(?:издание\s+официальное\b|дата\s+введения\b|"
    r"гост(?:\s+\d|\s*$)|межгосударственный\s+стандарт\b|"
    r"единая\s+система\s+конструкторской\s+документации\b|"
    r"unified\s+system\s+\w+\s+design\s+documentation\b|"
    r"группа\s+[а-яa-z]\d+\b|[мm][кk][сc]\s*\d)",
    re.IGNORECASE,
)
_STANDARD_CODE_LINE_RE = re.compile(r"^\d+(?:\.\d+)+\s*-\s*\d+$")
_ADMIN_HEADER_START_RE = re.compile(
    r"^(?:министерство\s+обороны\b|"
    r"федеральное\s+государственное\s+(?:каз[её]нное\s+)?"
    r"(?:военное\s+)?образовательное\s+учреждение\b)",
    re.IGNORECASE,
)
_ADMIN_HEADER_END_RE = re.compile(r"^(?:согласовано|утверждаю)\b", re.IGNORECASE)
_ADMIN_METADATA_RE = re.compile(
    r"^(?:согласовано|утверждаю|экз\.?\s*№|рег\.?\s*№|министерств[оа]\s+обороны\b)",
    re.IGNORECASE,
)
_PERIODICAL_HEADER_RE = re.compile(
    r"^(?:[«\"']?зарубежное\s+военное\s+обозрение[»\"']?"
    r"(?:\s*№\s*\d{1,2}/\d{4})?|issn\b.*|"
    r"арубежное|оенное|бозрение|з\s+в\s+о|\d{1,2}\.\s*\d{4})$",
    re.IGNORECASE,
)
_CONTENT_START_RE = re.compile(
    r"^(?:основные\s+сокращения(?:\s+и\s+обозначения)?|предисловие|введение|"
    r"глава\s+(?:1|первая)\b|1[.)]\s+\S|1\s+[А-ЯA-ZЁ])",
    re.IGNORECASE,
)
_TOC_LEADER_RE = re.compile(r"(?:\.{3,}|…|·{3,}|‒{3,}|–{3,}|—{3,})")


def _strip_admin_header_blocks(text: str) -> str:
    """Удалить ведомственную шапку целиком до подписи согласования."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i < 40 and _ADMIN_HEADER_START_RE.match(lines[i].strip()):
            end = None
            for j in range(i + 1, min(i + 31, len(lines))):
                if _ADMIN_HEADER_END_RE.match(lines[j].strip()):
                    end = j
                    break
            if end is not None:
                i = end + 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _strip_front_matter(text: str) -> str:
    """Отбросить титул, аннотацию и оглавление только при явном начале текста.

    Без повторного содержательного заголовка после оглавления ничего не режем:
    лучше оставить шапку, чем удалить начало документа по неуверенной эвристике.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines[:250]):
        if not _TOC_HEADING_RE.match(line.strip()):
            continue
        toc_entries = 0
        for j in range(i + 1, min(i + 251, len(lines))):
            s = lines[j].strip()
            if not s or _PAGE_MARK_RE.match(s) or _PAGE_DASH_RE.match(s):
                continue
            if _TOC_ENTRY_RE.match(s) or _TOC_LEADER_RE.search(s):
                toc_entries += 1
                continue
            if toc_entries < 1 or not _CONTENT_START_RE.match(s):
                continue
            next_content = next(
                (lines[k].strip() for k in range(j + 1, min(j + 8, len(lines))) if lines[k].strip()),
                "",
            )
            if _TOC_ENTRY_RE.match(next_content) or _TOC_LEADER_RE.search(next_content):
                continue  # это ещё пункт оглавления, а не повторный заголовок в тексте
            return "\n".join(lines[j:])
    return text


def _reflow(lines: list[str]) -> list[str]:
    """Склеить разорванные строки за линейное время.

    Признаки уже собранного абзаца хранятся в счётчиках, а части — в списке.
    Ни поиск по всему буферу, ни повторное копирование строки на каждой новой
    строке не выполняются: на TXT в сотни мегабайт старый вариант был O(n²).
    """
    out: list[str] = []
    buf_lines: list[str] = []
    buf_len = 0
    buf_ends_sentence = False
    buf_has_double_space = False
    buf_is_list = False
    buf_has_lower = False
    buf_has_cased = False

    def flush() -> None:
        nonlocal buf_lines, buf_len, buf_ends_sentence, buf_has_double_space
        nonlocal buf_is_list, buf_has_lower, buf_has_cased
        if buf_lines:
            out.append(" ".join(buf_lines))
            buf_lines = []
        buf_len = 0
        buf_ends_sentence = False
        buf_has_double_space = False
        buf_is_list = False
        buf_has_lower = False
        buf_has_cased = False

    for ln in lines:
        s = ln.strip()
        if not s:
            flush()
            if out and out[-1] != "":
                out.append("")
            continue
        if not buf_lines:
            buf_lines = [s]
            buf_len = len(s)
            buf_ends_sentence = bool(_SENTENCE_END_RE.search(s))
            buf_has_double_space = "  " in s
            buf_is_list = bool(_LIST_START_RE.match(s))
            buf_has_lower = s.upper() != s
            buf_has_cased = buf_has_lower or s.lower() != s
            continue
        buf_is_upper = buf_has_cased and not buf_has_lower
        if (
            buf_ends_sentence
            or buf_has_double_space
            or "  " in s
            or _CHAPTER_START_RE.match(s)
            or _ARTICLE_MARKER_RE.match(s)
            or _LIST_START_RE.match(s)
            or (buf_is_list and s[:1].isupper())
            or (len(s) >= 4 and s.isupper())
            or (buf_len >= 4 and buf_is_upper)
        ):
            flush()
            buf_lines = [s]
            buf_len = len(s)
            buf_ends_sentence = bool(_SENTENCE_END_RE.search(s))
            buf_has_double_space = "  " in s
            buf_is_list = bool(_LIST_START_RE.match(s))
            buf_has_lower = s.upper() != s
            buf_has_cased = buf_has_lower or s.lower() != s
            continue
        buf_lines.append(s)
        buf_len += len(s) + 1
        buf_ends_sentence = bool(_SENTENCE_END_RE.search(s))
        buf_has_double_space = buf_has_double_space or "  " in s
        s_has_lower = s.upper() != s
        buf_has_lower = buf_has_lower or s_has_lower
        buf_has_cased = buf_has_cased or s_has_lower or s.lower() != s
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


def strip_references(text: str) -> str:
    """Вырезать список литературы/источников — всё от заголовка до конца
    документа.

    Заголовок («Список использованных источников», «Литература» ...)
    считаем списком только если он стоит в последней трети документа
    ИЛИ после него идут минимум три строки, похожие на записи источников
    («12. Иванов И.И. ...», «[4] ...», «— М., 1985 ...») — чтобы не съесть
    обычный абзац, где слово «литература» стоит отдельной строкой."""
    lines = text.split("\n")
    n = len(lines)
    for i, ln in enumerate(lines):
        if not _REFS_HEADING_RE.match(ln.strip()):
            continue
        tail = lines[i + 1 :]
        entries = sum(1 for s in tail if _REF_ENTRY_START_RE.match(s.strip()))
        # 3+ записей-источников — точно список; в последней трети документа
        # хватает и одной записи.  Просто слово «литература» посреди текста
        # (без записей) не трогаем.
        if entries >= 3 or (i >= (2 * n) // 3 and entries >= 1):
            return "\n".join(lines[:i]).strip("\n")
    return text


# Строка, похожая на обрыв предложения, а не на заголовок (заканчивается
# знаком препинания) — такую первую строку в заголовок документа не берём.
_TITLE_BAD_END_RE = re.compile(r"[:;,.!?…]$")
# Строка из одних цифр/разделителей — не заголовок.
_TITLE_NUMERIC_RE = re.compile(r"^[\d\s.,:;()\-–—]+$")
# Хвост-ссылка на страницу в конце заголовка: «(стр. 1)», «— страница 3».
_TITLE_PAGE_TAIL_RE = re.compile(
    r"(?:\s*[([]\s*(?:стр\.?|страница|страницы|page|с\.)\s*\d+.*?[)\]]"
    r"|\s*[—–-]\s*(?:стр\.?|страница|page)\s*\d+.*)$",
    re.IGNORECASE,
)
# Зарезервированные имена Windows (CON, PRN, AUX, NUL, COM1-9, LPT1-9).
_RESERVED_NAME_RE = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE
)
_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TITLE_WORD_RE = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*|\d+(?:[.-]\d+)*")
_TITLE_SENTENCE_START_RE = re.compile(
    r"^(?:мы|вы|они|он|она|это|этот|эта|в\s+том\s+числе|также|кроме\s+того)\b",
    re.IGNORECASE,
)
_TITLE_VERB_END_RE = re.compile(r"(?:ать|ять|еть|ить|аем|яем|уем|или|ели)$", re.IGNORECASE)
_TITLE_VOWELS = frozenset("аеёиоуыэюяaeiouy")
_TITLE_ALLOWED_PUNCT = frozenset(".,:;!?…—–-()[]«»\"'")
_TITLE_BOILERPLATE = frozenset({
    "утверждаю", "согласовано", "приложение", "содержание", "оглавление",
    "межгосударственный стандарт", "стандарт", "издание официальное", "текст",
})


def is_probable_title(s: str) -> bool:
    """Консервативно отличить заголовок от OCR-абракадабры и предложения."""
    s = s.strip()
    if not 3 <= len(s) <= 120:
        return False
    if _TITLE_BAD_END_RE.search(s) or _TITLE_NUMERIC_RE.match(s) or s.isdigit():
        return False
    if s.strip(" .—–-").casefold() in _TITLE_BOILERPLATE:
        return False
    if (
        _TITLE_SENTENCE_START_RE.match(s)
        or "," in s
        or ";" in s
        or re.search(r"[.!?…]\s+\S", s)
    ):
        return False  # два предложения или новостной обрывок — не название
    if any(not (c.isalnum() or c.isspace() or c in _TITLE_ALLOWED_PUNCT) for c in s):
        return False
    words = _TITLE_WORD_RE.findall(s)
    alpha_words = [w for w in words if any(c.isalpha() for c in w)]
    if len(words) > 12 or not any(len(w) >= 3 for w in alpha_words):
        return False
    first_alpha = next((c for c in s if c.isalpha()), "")
    if first_alpha and first_alpha.islower():
        return False
    for word in alpha_words:
        if len(word) > 40:
            return False
        if len(word) > 6 and _TITLE_VERB_END_RE.search(word):
            return False
        for part in re.split(r"[-’']", word):
            if len(part) >= 4 and not part.isupper():
                if any(c.isupper() for c in part[1:]):
                    return False  # SlliSiiaSSSP, ITvr — случайный регистр OCR
                if not any(c.casefold() in _TITLE_VOWELS for c in part):
                    return False  # длинные бессмысленные согласные
    return True


def extract_title(text: str, max_scan: int = 12) -> str | None:
    """Первая похожая на заголовок строка документа (тема).

    Смотрим первые непустые строки: заголовок — короткая строка без маркеров
    страниц/колонтитулов/кодировок, не оглавление, не жанровый маркер, не
    обрыв предложения.  None — уверенного заголовка не нашлось, имя не меняем.
    """
    scanned = 0
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            continue
        scanned += 1
        if scanned > max_scan:
            break
        if not 3 <= len(s) <= 120:
            continue
        if _PAGE_MARK_RE.match(s) or _PAGE_DASH_RE.match(s):
            continue
        if _UDK_HEADER_RE.match(s):
            continue
        if any(r.match(s) for r in _LEGAL_BOILERPLATE_RES):
            continue
        if _TOC_HEADING_RE.match(s) or _TOC_ENTRY_RE.match(s):
            continue
        if _ARTICLE_MARKER_RE.match(s) or _REFS_HEADING_RE.match(s):
            continue
        if _CHAPTER_START_RE.match(s) and len(_CHAPTER_START_RE.sub("", s).strip()) < 3:
            continue  # «Глава 1» без текста — не заголовок документа
        # «Название (стр. 1)», «Название — страница 3» — хвосты-ссылки на
        # страницы в заголовок не включаем.
        s = _TITLE_PAGE_TAIL_RE.sub("", s).strip(" .")
        if is_probable_title(s):
            return s
    return None


def sanitize_filename(title: str, limit: int = 120) -> str:
    """Заголовок -> безопасное имя файла без расширения."""
    name = _INVALID_FILENAME_CHARS_RE.sub("", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rstrip(" .")
    if _RESERVED_NAME_RE.match(name):
        name = f"_{name}"
    return name


def _flatten_line(s: str) -> str:
    s = _BASE64_DATAURI_RE.sub(" ", s).strip()  # встроенная картинка — не текст
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


def _looks_like_spaced_ocr_garbage(s: str) -> bool:
    """Строка преимущественно из одиночных букв/цифр: «М Е Ж...», «2 .1 0 9»."""
    if len(s) < 7 or " " not in s:
        return False
    tokens = [t.strip(".,:;—–-_*()[]") for t in s.split()]
    tokens = [t for t in tokens if t]
    if len(tokens) < 4:
        return False
    singles = sum(len(t) == 1 and t.isalnum() for t in tokens)
    return singles >= 4 and singles / len(tokens) >= 0.45


def to_plain_text(text: str) -> str:
    """Привести markdown/HTML-мусор к чистому plain text."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    # Склейка перенесённых слов («Пери-\нод» -> «Период») — до построчной
    # обработки, иначе части слова пойдут разными строками.
    text = text.replace("\u00ad", "")
    text = _DEHYPHEN_RE.sub("", text)
    # HANDWRITTEN-обёртки: внутри текст, наружу его и выпускаем.
    text = _HANDWRITTEN_RE.sub(r"\1", text)
    # Image-аннотации в середине строки — без содержимого, вырезаем.
    text = _IMAGE_ANN_INLINE_RE.sub("", text)
    text = _strip_admin_header_blocks(text)
    text = _strip_front_matter(text)
    out: list[str] = []
    in_code_fence = False
    head_lines = 0  # содержательные строки сверху (УДК и титульная шапка)
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
        if not in_code_fence and _looks_like_spaced_ocr_garbage(s):
            continue  # «ГО СТ 2 .1 0 9», «М Е Ж Г О С У Д...»
        if not in_code_fence and head_lines < 30 and (
            _COVER_BOILERPLATE_RE.match(s)
            or _STANDARD_CODE_LINE_RE.fullmatch(s)
            or _ADMIN_METADATA_RE.match(s)
            or _PERIODICAL_HEADER_RE.match(s)
        ):
            continue  # реквизиты стандарта, ведомства или журнала в шапке
        if not in_code_fence and head_lines < 15 and _UDK_HEADER_RE.match(s):
            continue  # «УДК 338.245», «ГРНТИ ...», «ББК ...» — только в шапке
        if not in_code_fence and _PAGE_MARK_RE.match(s):
            continue  # «Страница 5 из 12», «Стр. 3»
        if not in_code_fence and _BASE64_BLOB_LINE_RE.match(s):
            continue  # base64-вложение (картинка/шрифт), снятое слоем как текст
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
        head_lines += 1
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
    # Список литературы/источников в конце — тоже не основной текст.
    text = strip_references(text)
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
    * 4+ частей подряд без единого жанрового маркера -> книга, расколотая
      на куски (практикумы/учебники «..._1»-«..._N»), объединяем ВСЕ;
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
    if len(texts) >= 4 and genre == 0:
        return True  # _1.._N без маркеров жанра — одна книга, склеить все
    chapter = sum(1 for s in starts if _CHAPTER_START_RE.match(s))
    if chapter >= max(1, round(0.5 * len(starts))):
        return True  # книга с главами
    avg = sum(len(t) for t in texts) // len(texts)
    return avg > 1500


def merge_parts(parts: list[str]) -> str:
    """Склеить части в один текст (части уже в порядке следования)."""
    texts = [p.strip() for p in parts if p and p.strip()]
    return "\n\n".join(texts)
