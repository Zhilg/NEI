"""Text-only pipeline: convert files to plain TXT and extract rubricator codes.

All Markdown reconstruction, VLM entity extraction, heading/stamp metadata
extraction and document reconstruction (DOCX/PDF/VSX) are removed.  The
worker:

1. Extracts plain text from each input file (PDF text layer, DOCX, PPTX,
   HTML).
2. Detects collection / journal / newspaper issues and splits them into
   individual articles or stories — each becomes a separate document.
3. Extracts rubricator classification codes (УДК, ГРНТИ, ГНТИ) together
   with their numbers from every document's text.
4. Writes one flat TXT file per document (``txts/<original name>.txt``,
   no subfolders and no sidecar metadata files).
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from idp.config import settings
from idp.document_classifier import detect_document_type
from idp.docx_converter import extract_docx_text
from idp.html_converter import extract_html_text, convert_html_folder_to_markdown
from idp.plaintext import to_plain_text, plan_merge, merge_parts
from idp.pptx_converter import extract_pptx_text
from idp.renderer import (
    convert_image_to_png,
    extract_pdf_text,
    extract_pdf_text_and_visual_pages,
)
from idp.rubricator import extract_rubricators
from idp.result_writer import ResultWriter
from idp.stats_writer import FileTimer, StatsWriter
from idp.textlayer import extract_clean_text_layer
from idp.txt_export import TxtExporter
from idp.txt_normalize import clean_and_name_txts
from idp.vlm_client import get_vl_endpoints, reconstruct_markdown, strip_bbox_annotations

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".html", ".htm", ".png", ".jpg", ".jpeg",
    # Текстовые форматы книг: обрабатываются сразу (чтение + нормализация),
    # без VLM.  Если файл не читается как текст — общий фолбэк ниже.
    ".txt", ".md", ".fb2",
}
# CSS and JavaScript are never document content — skip them explicitly even if
# they somehow appear under a supported extension list.
IGNORED_EXTENSIONS = {".css", ".js", ".mjs", ".ts", ".tsx", ".jsx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

# Keywords that hint the source is a collection / journal / newspaper issue
# that should be split into individual articles or stories.
_COLLECTION_KEYWORDS = re.compile(
    r"(?:сборник|журнал|газета|альманах|вестник|выпуск|содержание|оглавление|"
    r"номер|вып\.\s*\d|№\s*\d)",
    re.IGNORECASE,
)

# Lines that look like the start of a new article / story inside a collection.
_ARTICLE_START_RE = re.compile(
    r"^(?:Рассказ|Повесть|Очерк|Статья|Эссе|Поэзия|Стихи|Проза|"
    r"Публицистика|Интервью|Заметка|Документ|Критика|Рецензия|"
    r"Материал|Опубликовано|Рубрика)\b",
    re.IGNORECASE,
)

# A line that looks like a short title (5-80 chars, starts with a capital or digit).
_TITLE_LINE_RE = re.compile(r"^[А-ЯЁA-Z0-9][^\n]{4,78}$")


@dataclass
class FileResult:
    file: str
    type: str
    size_bytes: int
    pages: int | None
    duration_sec: float
    status: str
    error: str | None
    documents: list[dict]
    document_type: str
    # Стебли записанных txt (без .txt) — журнал помнит, какой txt кому
    # принадлежит: переработка перезапишет его на месте, а не плодит _2.
    txts: list[str] | None = None

    def to_dict(self) -> dict:
        record = {
            "file": self.file,
            "type": self.type,
            "size_bytes": self.size_bytes,
            "pages": self.pages,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "error": self.error,
            "documents": self.documents,
            "document_type": self.document_type,
        }
        if self.txts is not None:
            record["txts"] = self.txts
        return record


def _load_names_map(txts_root: Path) -> dict[str, str]:
    """Карта переименования по теме: исходный стебель txt -> новое имя."""
    map_path = txts_root / "_names_map.json"
    if map_path.is_file():
        try:
            data = json.loads(map_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError) as exc:
            logger.warning("карта имён %s не читается: %s", map_path, exc)
    return {}


def _extract_text(file_path: Path) -> tuple[str, int]:
    """Extract plain text from ``file_path``.

    Returns ``(text, page_count)``.  For PDFs the embedded text layer is used;
    pages are separated by the form-feed character (``\\f``).
    """
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        text, pages = extract_pdf_text(file_path)
        return text, pages
    if ext == ".docx":
        return extract_docx_text(file_path), 1
    if ext == ".pptx":
        return extract_pptx_text(file_path), 1
    if ext in (".html", ".htm"):
        text = extract_html_text(file_path)
        if not _has_meaningful_content(text):
            return "", 1
        return text, 1
    if ext in (".txt", ".md"):
        return _read_text_file(file_path), 1
    if ext == ".fb2":
        return _extract_fb2_text(file_path), 1
    # Images and unsupported formats: no OCR available without the VLM,
    # so they produce empty text.
    return "", 0


# Порядок важен: utf-8 до cp1251 (байты ASCII совместимы с обоими, но
# валидный utf-8 декодируется первым).  utf-16 без BOM идёт ПОСЛЕ cp1251
# и только с проверкой правдоподобия — иначе байты cp1251 декодируются
# как utf-16 без ошибок, но в мусор из private-use диапазона.
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "utf-16")

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _looks_like_garbage(text: str) -> bool:
    """Эвристика против ложных декодировок: cp1251-байты, прочитанные как
    utf-16, кишат символами private-use; utf-16-байты, прочитанные как
    cp1251, дают поток управляющих \\x00.  Настоящий текст такого не содержит."""
    if not text:
        return True
    bad = 0
    for ch in text:
        o = ord(ch)
        if ch in "\n\r\t":
            continue
        if o < 32 or 0xE000 <= o <= 0xF8FF or 0xFFF0 <= o <= 0xFFFF:
            bad += 1
    return bad / len(text) > 0.02


def _read_text_file(file_path: Path) -> str:
    """Чтение .txt/.md с детектом кодировки.

    Корпус собирался десятилетиями — txt встречаются в cp1251, utf-16 и
    битом utf-8.  BOM идентифицирует utf-16/utf-8-sig однозначно (иначе
    cp1251 декодировал бы их байты без ошибок и получался бы мусор),
    дальше пробуем остальные кодировки по очереди; если ни одна не подошла —
    читаем с заменой недекодируемых символов, чтобы файл не терялся (мусор
    отсеет эвристика качества ниже по пайплайну).
    """
    raw = file_path.read_bytes()
    if raw.startswith(_UTF16_BOMS):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    for enc in _TEXT_ENCODINGS:
        try:
            decoded = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc in ("utf-16", "cp1251") and _looks_like_garbage(decoded):
            continue
        return decoded
    return raw.decode("utf-8", errors="replace")


def _extract_fb2_text(file_path: Path) -> str:
    """FictionBook (.fb2) — XML: собираем весь текст, теги отбрасываются."""
    from xml.etree import ElementTree  # noqa: PLC0415

    raw = file_path.read_bytes()
    for enc in _TEXT_ENCODINGS:
        try:
            root = ElementTree.fromstring(raw.decode(enc))
            break
        except (UnicodeDecodeError, ElementTree.ParseError):
            continue
    else:
        return ""
    return "".join(root.itertext())


def _has_meaningful_content(text: str, min_chars: int = 200) -> bool:
    """Return True when ``text`` looks like real document content rather than
    a service page (navigation, index, cookie banner, empty shell, etc.).

    Heuristics:
    * At least ``min_chars`` characters of text after stripping whitespace.
    * At least two non-trivial lines (more than a title + a link).
    """
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if len(stripped) < min_chars:
        return False
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    # Count lines that look like actual prose (longer than a typical nav item).
    prose_lines = [ln for ln in lines if len(ln) > 40]
    return len(prose_lines) >= 1


def _is_collection(text: str, file_path: Path) -> bool:
    """Heuristic: does ``text`` look like a collection / journal / newspaper
    issue that contains multiple independent articles or stories?"""
    if _COLLECTION_KEYWORDS.search(text[:3000]):
        return True
    # Multiple form-feed pages with short title-like first lines also hint
    # at a multi-article issue.
    pages = [p for p in text.split("\f") if p.strip()]
    if len(pages) >= 2:
        title_pages = 0
        for page in pages[:5]:
            first_line = page.strip().splitlines()[0].strip() if page.strip() else ""
            if _TITLE_LINE_RE.match(first_line) and len(first_line.split()) <= 12:
                title_pages += 1
        if title_pages >= 2:
            return True
    return False


def _split_into_documents(text: str, file_path: Path) -> list[dict]:
    """Split ``text`` into individual documents (articles/stories).

    If the text is not a collection, the whole text is returned as one
    document.  Otherwise it is split at article-start markers found at the
    beginning of a page or after blank lines.
    """
    if not _is_collection(text, file_path):
        return [{"text": text.strip(), "page": 1}]

    pages = [p for p in text.split("\f")] if "\f" in text else [text]
    documents: list[dict] = []
    current_lines: list[str] = []
    current_page = 1

    def _flush() -> None:
        nonlocal current_lines
        if current_lines:
            joined = "\n".join(current_lines).strip()
            if joined:
                documents.append({"text": joined, "page": current_page})
            current_lines = []

    for page_index, page_text in enumerate(pages, 1):
        if not page_text.strip():
            continue
        current_page = page_index
        for line in page_text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if _ARTICLE_START_RE.match(stripped):
                _flush()
            current_lines.append(stripped)
    _flush()

    if not documents:
        return [{"text": text.strip(), "page": 1}]
    return documents


class Worker:
    def __init__(
        self,
        force: bool = False,
    ) -> None:
        self.force = force
        self._input_root: Path = settings.input_root
        self._processed: set[str] = set()
        self._stats_writer: StatsWriter | None = None
        # Какой txt (стебель) принадлежит какому входному файлу: из поля
        # txts журнала; переработка перезаписывает его, а не плодит _2.
        self._txt_by_rel: dict[str, list[str]] = {}
        # Имена, занятые в ТЕКУЩЕМ прогоне: повторное использование (два
        # файла с одним стеблем из старых записей) → свежее имя, не перезапись.
        self._reused_stems: set[str] = set()
        # Карта переименования по теме (исходный стебель -> новое имя).
        self._names_map: dict[str, str] = {}

    def _find_files(self, input_root: Path) -> list[Path]:
        # ОДИН проход по дереву.  Раньше здесь был is_relative_to() против
        # множества всех коллекций для каждой записи — O(записи × коллекции),
        # на 200k+ файлов воркер молчал часами.  Теперь фильтрация через
        # цепочку родителей (O(глубина) на файл).
        t0 = time.perf_counter()
        files: list[Path] = []
        dirs: set[Path] = set()
        n_scanned = 0
        for path in input_root.rglob("*"):
            n_scanned += 1
            if path.is_dir():
                # Книга-коллекция — это:
                #  - папка с именем *.html/.htm (выгрузки wget: NAME.html/ с
                #    контентом на любой глубине) — всегда, иначе такая папка
                #    молча исчезала из пайплайна без строки в логах;
                #  - папка с html-файлами ПРЯМО внутри (корень книги).
                #  Детектим по прямым детям, не по rglob: иначе папка-
                #  контейнер с несколькими книгами внутри сливала бы их в
                #  один документ.  Вложенные книги детектятся сами, когда
                #  обход до них доходит.
                #  Сами страницы при конвертации собираются рекурсивно
                #  (rglob в convert_html_folder_to_markdown), поэтому
                #  chapters/*.html внутри корня книги не теряются.
                name = path.name.lower()
                is_named = name.endswith(".html") or name.endswith(".htm")
                has_direct_html = False
                if not is_named:
                    try:
                        has_direct_html = any(
                            p.suffix.lower() in (".html", ".htm")
                            for p in path.iterdir()
                            if p.is_file()
                        )
                    except OSError:
                        has_direct_html = False
                if (is_named or has_direct_html) and not any(
                    par in dirs for par in path.parents
                ):
                    dirs.add(path)
            elif path.is_file():
                ext = path.suffix.lower()
                if ext in IGNORED_EXTENSIONS:
                    continue
                if ext in SUPPORTED_EXTENSIONS:
                    files.append(path)
        filtered_files = [f for f in files if not any(par in dirs for par in f.parents)]
        result = sorted(dirs) + sorted(filtered_files)
        logger.info(
            "Сканирование input: %d записей в дереве, %d файлов к обработке, "
            "%d коллекций за %.1f с",
            n_scanned, len(filtered_files), len(dirs), time.perf_counter() - t0,
        )
        return result

    def _load_processed(self) -> set[str]:
        processed: set[str] = set()
        results_path = settings.output_root / "results.jsonl"
        if not results_path.exists():
            return processed
        with open(results_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Владение txt собираем ВСЕГДА (даже с --force): это не
                # «обработано/нет», а «какой txt принадлежит файлу» —
                # без этого полный перепрогон расплодил бы _2 рядом
                # с каждым существующим результатом.
                rec_txts = record.get("txts")
                if isinstance(rec_txts, list):
                    self._txt_by_rel[record["file"]] = [str(t) for t in rec_txts]
                if self.force:
                    continue
                if record.get("status") in ("ok", "skip"):
                    processed.add(record["file"])
        return processed

    def _already_processed(self, file_path: Path) -> bool:
        relative = str(file_path.relative_to(self._input_root))
        return relative in self._processed

    def _skip_result(self, file_path: Path, relative: Path, timer: FileTimer, file_type: str) -> FileResult:
        return FileResult(
            file=str(relative),
            type=file_type,
            size_bytes=timer.size_bytes,
            pages=timer.pages,
            duration_sec=round(time.perf_counter() - timer.start, 3),
            status="skip",
            error=None,
            documents=[],
            document_type="other",
        )

    def _error_result(self, file_path: Path, relative: Path, timer: FileTimer, file_type: str, exc: Exception) -> FileResult:
        return FileResult(
            file=str(relative),
            type=file_type,
            size_bytes=timer.size_bytes,
            pages=timer.pages,
            duration_sec=round(time.perf_counter() - timer.start, 3),
            status="error",
            error=str(exc),
            documents=[],
            document_type="other",
        )

    async def _reconstruct_with_vlm(self, file_path: Path, relative: Path) -> tuple[str, int]:
        """Текст для файла без текстового слоя: рендер страниц -> VLM markdown.

        Возвращает ``(text, page_count)``.  Форматы, для которых VLM-фолбэк
        бессмыслен (DOCX/PPTX/HTML — они born-digital), дают пустой текст.
        Ошибки VLM (модель недоступна и т.п.) не глотаем: пусть файл уйдёт
        в status=error и будет виден в results.jsonl.

        Рендеры страниц живут во временной папке и удаляются сразу после
        запроса к VLM — на диске и в архиве остаются только текстовые
        результаты.
        """
        ext = file_path.suffix.lower()
        if ext not in IMAGE_EXTENSIONS and ext != ".pdf":
            return "", 0
        # Уникальная папка на файл: параллельные воркеры с одинаковыми
        # stem не должны делить общий temp-каталог.
        tmp_dir = Path(tempfile.mkdtemp(prefix="idp_pages_"))
        try:
            if ext == ".pdf":
                _, pngs, _ = await asyncio.to_thread(
                    extract_pdf_text_and_visual_pages, file_path, tmp_dir
                )
            else:
                png = await asyncio.to_thread(
                    convert_image_to_png, file_path, tmp_dir / "page_00001.png"
                )
                pngs = [png]
            if not pngs:
                return "", 0
            logger.info("%s: реконструкция через VLM-OCR (%d стр.)", relative, len(pngs))
            markdown = await reconstruct_markdown(pngs)
            return strip_bbox_annotations(markdown).strip(), len(pngs)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _record_stats(self, timer: FileTimer, result: FileResult | None) -> None:
        duration = time.perf_counter() - timer.start
        stats = self._stats_writer or StatsWriter(settings.output_root / "stats.jsonl")
        stats.record(
            file=timer.file_name,
            type=timer.file_type,
            size_bytes=timer.size_bytes,
            pages=timer.pages,
            duration_sec=round(duration, 3),
            status=result.status if result else "error",
            error=result.error if result else "unknown",
        )

    async def _process_file(
        self,
        file_path: Path,
        result_writer: ResultWriter,
        txt_exporter: TxtExporter,
        pbar: tqdm,
    ) -> tuple[dict, list[Path]]:
        relative = file_path.relative_to(self._input_root)
        file_type = (
            file_path.suffix.lower().lstrip(".") if file_path.is_file() else "html_folder"
        )

        timer = FileTimer(
            file_name=str(relative),
            file_type=file_type,
            size_bytes=file_path.stat().st_size if file_path.is_file() else 0,
            pages=None,
        )

        result: FileResult | None = None
        used_textlayer = False
        try:
            if file_path.is_dir():
                text = convert_html_folder_to_markdown(file_path)
                pages = 1
            elif file_path.suffix.lower() == ".pdf":
                # Текстовый слой PDF: чистый born-digital забираем сразу
                # (секунды CPU), «отравленные» слои (битая кодировка,
                # OCR-мусор) эвристика отсеивает — файл падает в VLM-ветку.
                extracted = await asyncio.to_thread(extract_clean_text_layer, file_path)
                if extracted is not None:
                    text, pages = extracted
                    used_textlayer = True
                else:
                    text, pages = "", 0
            else:
                text, pages = await asyncio.to_thread(_extract_text, file_path)
            timer.pages = pages

            if not text.strip():
                # Скан/PNG/JPEG/отравленный PDF: рендерим страницы и
                # реконструируем текст через VLM — скипать нельзя.
                text, pages = await self._reconstruct_with_vlm(file_path, relative)
                timer.pages = pages

            if not text.strip():
                result = self._skip_result(file_path, relative, timer, file_type)
                self._processed.add(str(relative))
                pbar.set_postfix(file=file_path.name, stage="done")
                return result.to_dict(), []

            # Формат корпуса — чистый plain text: без markdown-разметки
            # (заголовки #, **жирный**, таблицы |...|, ссылки), HTML-сущностей
            # и служебных комментариев.  Имя файла — только в имени txt.
            # Рубрикаторы (УДК/ГРНТИ/ГНТИ) достаём из текста ДО to_plain_text:
            # чистка вырезает коды классификации из шапки как мусор, а в
            # журнал они должны попасть.  Файловые коды отдаём первому
            # документу (шапка относится ко всему файлу целиком).
            file_rubricators = extract_rubricators(text, page=1)
            text = to_plain_text(text)
            if not text.strip():
                result = self._skip_result(file_path, relative, timer, file_type)
                self._processed.add(str(relative))
                pbar.set_postfix(file=file_path.name, stage="done")
                return result.to_dict(), []

            document_type = detect_document_type(file_path, text)
            documents = _split_into_documents(text, file_path)
            # Эвристика коллекций иногда раскалывает одну книгу на главы —
            # склеиваем обратно, если это не газета/сборник статей.
            if len(documents) > 1 and plan_merge([d.get("text", "") for d in documents]):
                merged = merge_parts([d.get("text", "") for d in documents])
                documents = [{"text": merged, "page": 1}]

            doc_results: list[dict] = []
            for doc_idx, doc in enumerate(documents):
                doc_text = doc.get("text", "").strip()
                if not doc_text:
                    continue
                doc_page = int(doc.get("page", 1) or 1)
                rubricators = extract_rubricators(doc_text, page=doc_page)
                if doc_idx == 0 and file_rubricators:
                    seen = {(r.get("type"), r.get("number")) for r in rubricators}
                    rubricators = rubricators + [
                        r for r in file_rubricators
                        if (r.get("type"), r.get("number")) not in seen
                    ]
                # Тексты живут только в txts/ — в results.jsonl (и архиве)
                # записываем только тип и номер рубрикатора.  source=textlayer
                # помечает записи быстрого пути: pdf-requeue НЕ выкидывает их
                # как «старый формат» (по признаку text внутри документов
                # они на него похожи, но это осознанный текстовый слой).
                doc_result: dict = {
                    "rubricators": [
                        {"type": r.get("type", ""), "number": r.get("number", "")}
                        for r in rubricators
                    ]
                }
                if used_textlayer:
                    doc_result["source"] = "textlayer"
                doc_results.append(doc_result)

            result = FileResult(
                file=str(relative),
                type=file_type,
                size_bytes=timer.size_bytes,
                pages=pages,
                duration_sec=round(time.perf_counter() - timer.start, 3),
                status="ok",
                error=None,
                documents=doc_results,
                document_type=document_type,
            )
            self._processed.add(str(relative))
            produced = await self._export_txt_bundle(file_path, relative, documents, txt_exporter)
            result.txts = [p.stem for p in produced]
            pbar.set_postfix(file=file_path.name, stage="done")
            return result.to_dict(), produced
        except Exception as exc:
            pbar.set_postfix(file=file_path.name, stage="error")
            logger.exception("Failed to process %s", relative)
            result = self._error_result(file_path, relative, timer, file_type, exc)
            return result.to_dict(), []
        finally:
            self._record_stats(timer, result)

    def _prev_txt_stems(self, relative: Path) -> list[str]:
        """Имена txt, которые принадлежат этому файлу (для перезаписи).

        Явное владение — поле txts журнала; имена прогоняются через карту
        переименования по теме: переименованный файл перезаписывается ПОД
        ТЕМ ЖЕ новым именем, а не плодит дубликат под старым стеблем.
        Записи старого формата (без поля txts) перезаписываем только когда
        txt реально лежит на диске — иначе это новый файл, и свободное имя
        ему выделит обычная аллокация.  Имена, уже занятые другим файлом в
        этом прогоне (один стебель у двух файлов), отсекаем — чужой результат
        не затираем.
        """
        prev = [str(p) for p in self._txt_by_rel.get(str(relative), [])]
        if not prev:
            stem_txt = settings.output_root / "txts" / f"{relative.stem}.txt"
            if stem_txt.is_file():
                prev = [relative.stem]
        prev = [self._names_map.get(p, p) for p in prev]
        return [p for p in prev if p not in self._reused_stems]

    async def _export_txt_bundle(
        self,
        file_path: Path,
        relative: Path,
        documents: list[dict],
        txt_exporter: TxtExporter,
    ) -> list[Path]:
        """Записать txt документа; вернуть пути записанных файлов (для архива).

        Берёт полные документы (с текстом) из сплиттера — в result.documents
        текста уже нет, там только рубрикаторы.  Переработанный файл
        перезаписывает свой прежний txt (reuse), новый получает свободное имя.
        """
        written: list[Path] = []
        try:
            reuse_queue = self._prev_txt_stems(relative)
            docs = [d for d in documents if d.get("text", "").strip()]
            if not docs:
                # Файл обработан, но документов не вышло — всё равно пишем
                # пустой TXT, чтобы в выходе (и архиве) была запись о файле.
                path = await asyncio.to_thread(
                    txt_exporter.export, "",
                    name=relative.stem,
                    reuse=reuse_queue[0] if reuse_queue else None,
                )
                self._reused_stems.add(path.stem)
                written.append(path)
                self._txt_by_rel[str(relative)] = [path.stem]
                return written
            # Flat output: txts/<original name>.txt, no subfolders, no meta.json.
            # Несколько документов из одной коллекции получают _2, _3 ...
            base_name = relative.stem
            for index, doc in enumerate(docs):
                doc_text = doc.get("text", "").strip()
                name = f"{base_name}_{index + 1}" if len(docs) > 1 else base_name
                path = await asyncio.to_thread(
                    txt_exporter.export, doc_text,
                    name=name,
                    reuse=reuse_queue.pop(0) if reuse_queue else None,
                )
                self._reused_stems.add(path.stem)
                written.append(path)
            self._txt_by_rel[str(relative)] = [p.stem for p in written]
        except Exception as exc:
            logger.warning("Txt export failed for %s: %s", file_path.name, exc)
        return written

    async def run(
        self,
        input_root: Path | None = None,
        watch: bool = False,
        interval: float = 5.0,
        settle: float = 2.0,
    ) -> None:
        root = input_root or settings.input_root
        self._input_root = root
        # Пул потоков для CPU-части (текстовый слой PDF, конвертеры, запись
        # txt): по умолчанию asyncio даёт min(32, cpu+4), поднимаем до
        # worker_threads, чтобы большие пачки не упирались в 16 потоков.
        asyncio.get_running_loop().set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=settings.worker_threads)
        )
        settings.output_root.mkdir(parents=True, exist_ok=True)
        result_writer = ResultWriter(
            settings.output_root / "results.jsonl",
            pretty_path=settings.output_root / "results_readable.json",
        )
        txt_exporter = TxtExporter()
        self._stats_writer = StatsWriter(settings.output_root / "stats.jsonl")
        self._processed = self._load_processed()
        # При старте только удаляем неосновной текст и обновляем названия
        # по теме. Склейка частей и VLM-reflow здесь больше не запускаются.
        txts_root = settings.output_root / "txts"
        if txts_root.is_dir() and any(txts_root.glob("*.txt")):
            await asyncio.to_thread(
                clean_and_name_txts, txts_root, settings.worker_threads
            )
        # Карта переименования по теме (после нормализации — она её пополняет):
        # переработанный файл перезапишет свой txt под ТЕМ ЖЕ новым именем.
        self._names_map = _load_names_map(txts_root)
        # Владельцы txt из последнего verify (scripts/verify_reprocess.py):
        # сброшенные на переработку файлы перезапишут свои txt на месте, а не
        # плодят _2 рядом со старой версией.
        state_path = settings.output_root / "verify_state.json"
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                for rel, stems in (state.get("owners") or {}).items():
                    if isinstance(stems, list):
                        self._txt_by_rel[str(rel)] = [str(s) for s in stems]
            except (OSError, ValueError) as exc:
                logger.warning("состояние verify %s не читается: %s", state_path, exc)
        logger.info(
            "Воркер запущен: модель %s | эндпоинты: %s | потоков: %d | "
            "VLM-конкуренция: %d | таймаут: %.0fс | уже обработано: %d",
            settings.vl_model,
            ", ".join(get_vl_endpoints()),
            settings.worker_threads,
            settings.vl_concurrency,
            settings.vl_timeout_seconds,
            len(self._processed),
        )

        async def _process_batch() -> int:
            batch_start = time.perf_counter()
            files = self._find_files(root)
            pending = [f for f in files if not self._already_processed(f)]
            if len(pending) < len(files):
                # Уже в журнале со статусом ok/skip — не трогаем.  Без этой
                # строки повторно подложенные файлы «молча игнорировались»,
                # и было непонятно, то ли воркер слепой, то ли файл старый.
                logger.info(
                    "Пропущено, уже обработаны: %d из %d найденных",
                    len(files) - len(pending), len(files),
                )
            if watch and settle > 0:
                # Файл, который прямо сейчас копируют в папку, выглядит уже
                # лежащим на месте.  Берём только те, что не менялись дольше
                # settle секунд, — недокопированный файл обработается на
                # следующем проходе целиком.
                cutoff = time.time() - settle
                fresh = [f for f in pending if f.is_file() and f.stat().st_mtime > cutoff]
                if fresh:
                    logger.info("Отложено (ещё копируется?): %s", ", ".join(f.name for f in fresh))
                pending = [f for f in pending if f not in fresh]
            pending.sort(key=lambda p: p.stat().st_size if p.is_file() else 0)
            # Пакет усечён — обработали N, перескан, новые файлы подхватываются
            # быстро, в логах регулярно видно 'Сканирование input'.
            batch_limit = settings.max_batch_size
            if batch_limit and len(pending) > batch_limit:
                logger.info(
                    "Пакет усечён: %d из %d файлов (остальное — следующие циклы)",
                    batch_limit, len(pending),
                )
                pending = pending[:batch_limit]
            if not pending:
                return 0
            logger.info("Новых файлов для обработки: %d", len(pending))
            pbar = tqdm(total=len(pending), desc="Files", unit="file")

            # Очередь + фиксированное число воркеров: на пачке в десятки
            # тысяч файлов не создаём по корутине на файл, память ровная.
            # Реальный параллелизм ограничен пулом потоков (CPU-часть) и
            # семафором VLM (запросы к модели).
            queue: asyncio.Queue[Path | None] = asyncio.Queue()
            for file_path in pending:
                queue.put_nowait(file_path)
            n_workers = min(settings.worker_threads, len(pending))
            for _ in range(n_workers):
                queue.put_nowait(None)

            async def _queue_worker() -> None:
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    result, _produced = await self._process_file(item, result_writer, txt_exporter, pbar)
                    # Строка в лог на каждый файл — чтобы по логам было видно,
                    # что сканер жуёт (без этого только tqdm-мусор в stderr).
                    if result.get("status") == "ok":
                        logger.info(
                            "OK %s: %d стр., %d док., %.1fс",
                            result.get("file"),
                            result.get("pages") or 0,
                            len(result.get("documents") or []),
                            result.get("duration_sec") or 0.0,
                        )
                    elif result.get("status") == "error":
                        logger.error("ERROR %s: %s", result.get("file"), result.get("error"))
                    result_writer.write(result)
                    result_writer.flush()
                    pbar.update(1)

            try:
                await asyncio.gather(*[_queue_worker() for _ in range(n_workers)])
            finally:
                pbar.close()
            self._stats_writer.flush()
            logger.info(
                "Пакет завершён: %d файлов за %.1f с (всего обработано: %d)",
                len(pending), time.perf_counter() - batch_start, len(self._processed),
            )
            return len(pending)

        if not watch:
            await _process_batch()
            result_writer.finalize()
            self._stats_writer.flush()
            return

        logger.info(
            "Режим наблюдения: сканирование %s каждые %.0f сек (пауза записи %.0f сек)",
            root, interval, settle,
        )
        empty_cycles = 0
        try:
            while True:
                try:
                    batch_count = await _process_batch()
                except Exception as exc:  # noqa: BLE001 — keep the watcher alive
                    logger.exception("Пакет сканирования не удался: %s", exc)
                    batch_count = -1
                if batch_count > 0:
                    empty_cycles = 0
                else:
                    empty_cycles += 1
                    # Сразу одна строка-подтверждение, дальше пульс раз
                    # в ~минуту — чтобы лог показывал, что наблюдатель жив,
                    # но не превращался в спам каждые 5 секунд.
                    if empty_cycles == 1 or empty_cycles % 12 == 0:
                        logger.info(
                            "Наблюдатель активен: новых файлов нет (цикл %d, обработано всего: %d)",
                            empty_cycles, len(self._processed),
                        )
                await asyncio.sleep(interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Режим наблюдения остановлен")
        finally:
            result_writer.finalize()
            self._stats_writer.flush()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IDP pipeline: text conversion and rubricator extraction")
    parser.add_argument("--force", action="store_true", help="Ignore processed cache and reprocess all files")
    parser.add_argument("--input", type=Path, default=None, help="Input directory (overrides IDP_INPUT_ROOT)")
    parser.add_argument(
        "--watch",
        action="store_true",
        default=os.getenv("IDP_WATCH", "").lower() in ("1", "true", "yes"),
        help="Режим наблюдения: сканировать input_root и обрабатывать новые файлы сразу после появления",
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=float(os.getenv("IDP_WATCH_INTERVAL", "5")),
        help="Интервал сканирования в режиме наблюдения, сек (по умолчанию 5, env IDP_WATCH_INTERVAL)",
    )
    parser.add_argument(
        "--watch-settle",
        type=float,
        default=float(os.getenv("IDP_WATCH_SETTLE", "2")),
        help="Пауза записи: файл считается новым только если не менялся столько секунд "
             "(защита от обработки недокопированного файла; 0 — отключить, env IDP_WATCH_SETTLE)",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    level = getattr(logging, os.getenv("IDP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = Worker(force=args.force)
    await worker.run(input_root=args.input, watch=args.watch, interval=args.watch_interval, settle=args.watch_settle)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
