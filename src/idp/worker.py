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
import struct
import tempfile
import time
import zlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from idp.config import settings
from idp.document_classifier import detect_document_type
from idp.docx_converter import extract_docx_text
from idp.html_converter import extract_html_text, convert_html_folder_to_markdown
from idp.plaintext import to_plain_text
from idp.pptx_converter import extract_pptx_text
from idp.renderer import (
    convert_image_to_png,
    extract_pdf_text,
    extract_pdf_text_and_visual_pages,
)
from idp.rubricator import extract_rubricators
from idp.result_writer import ResultWriter
from idp.stats_writer import FileTimer, StatsWriter
from idp.txt_export import TxtExporter
from idp.vlm_client import get_vl_endpoints, reconstruct_markdown, strip_bbox_annotations

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".htm", ".png", ".jpg", ".jpeg"}
# CSS and JavaScript are never document content — skip them explicitly even if
# they somehow appear under a supported extension list.
IGNORED_EXTENSIONS = {".css", ".js", ".mjs", ".ts", ".tsx", ".jsx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

# Top-level journals that keep changing while the worker runs.  The archive
# adds new files incrementally, but these must be re-synced (full repack)
# whenever their content changed, otherwise the zip holds a stale first copy.
LIVE_FILES = ("results.jsonl", "results_readable.json", "stats.jsonl")

ARCHIVE_NAME = "results.zip"


# --- Параллельная пересборка архива ---------------------------------------
#
# Каждый файл жмётся в собственный одноразовый шард (zlib отпускает GIL —
# реальный параллелизм), затем швы склеиваются СЫРЫМИ сжатыми байтами без
# пережатия.  Оглавление пишем сами, в том числе ZIP64: на больших прогонах
# записей больше 65535 и архив больше 4 ГБ — оба случая обязаны читаться
# любым zip-ридером.  Раньше пересборка 80k файлов занимала ~14 минут
# одним потоком; в 64 потока — считанные минуты.

_ZIP64_EXTRA_ID = 0x0001
_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")          # 30 байт
_CENTRAL_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")  # 46 байт
_ZIP64_EOCD = struct.Struct("<IQHHIIQQQQ")
_ZIP64_LOCATOR = struct.Struct("<IIQI")
_EOCD = struct.Struct("<IHHHHIIH")


def _dos_datetime(dt: tuple[int, ...]) -> tuple[int, int]:
    year, month, day, hour, minute, sec = (list(dt) + [0] * 6)[:6]
    year = max(1980, min(year, 2107))
    return (
        (hour << 11) | (minute << 5) | (sec // 2),
        ((year - 1980) << 9) | (month << 5) | day,
    )


def _pack_shard(item: tuple[str, Path], shard_dir: Path) -> tuple[zipfile.ZipInfo, Path]:
    """Сжать один файл в отдельный zip-шард (на диске, не в памяти)."""
    arcname, path = item
    fd, name = tempfile.mkstemp(suffix=".zipshard", dir=shard_dir)
    os.close(fd)
    shard = Path(name)
    with zipfile.ZipFile(shard, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(path, arcname)
    with zipfile.ZipFile(shard, "r") as zf:
        info = zf.infolist()[0]
    return info, shard


class _ZipAssembler:
    """Склейка шардов в один zip: локальные заголовки копируются как есть,
    центральное оглавление (с ZIP64 при необходимости) пишется один раз в
    самом конце — поэтому добавлять записи можно чанками."""

    def __init__(self, tmp: Path) -> None:
        self._out = open(tmp, "wb")
        self._offset = 0
        self._centrals: list[tuple[zipfile.ZipInfo, int]] = []

    def add(self, info: zipfile.ZipInfo, shard: Path) -> None:
        with open(shard, "rb") as sf:
            head = sf.read(_LOCAL_HEADER.size)
            (_sig, _ver, _flag, _method, _mt, _md, _crc,
             _cs, _us, nlen, xlen) = _LOCAL_HEADER.unpack(head)
            self._out.write(head)
            self._out.write(sf.read(nlen + xlen))
            # ТОЛЬКО сжатые данные: length в copyfileobj — это размер БУФЕРА,
            # копируется до EOF, включая оглавление и EOCD шарда.  Мусор между
            # записями ломал Проводник Windows (Linux читал терпеливо).
            remaining = info.compress_size
            while remaining > 0:
                buf = sf.read(min(1 << 20, remaining))
                if not buf:
                    raise RuntimeError(
                        f"шард {shard.name}: оборвался после "
                        f"{info.compress_size - remaining} из {info.compress_size} байт"
                    )
                self._out.write(buf)
                remaining -= len(buf)
        self._centrals.append((info, self._offset))
        self._offset = self._out.tell()

    def finish(self) -> int:
        out, offset = self._out, self._offset
        cd_start = offset
        for info, local_off in self._centrals:
            name_b = info.filename.encode("utf-8" if (info.flag_bits & 0x800) else "cp437")
            big_size = info.file_size > 0xFFFFFFFF or info.compress_size > 0xFFFFFFFF
            big_off = local_off > 0xFFFFFFFF
            vals: list[int] = []
            if big_size:
                vals.extend((info.file_size, info.compress_size))
            if big_off:
                vals.append(local_off)
            extra = b""
            if vals:
                payload = struct.pack(f"<{len(vals)}Q", *vals)
                extra = struct.pack("<HH", _ZIP64_EXTRA_ID, len(payload)) + payload
            mtime, mdate = _dos_datetime(info.date_time)
            out.write(_CENTRAL_HEADER.pack(
                0x02014B50,
                ((info.create_system & 0xFF) << 8) | (info.create_version & 0xFF),
                info.extract_version, info.flag_bits, info.compress_type,
                mtime, mdate, info.CRC,
                0xFFFFFFFF if big_size else info.compress_size,
                0xFFFFFFFF if big_size else info.file_size,
                len(name_b), len(extra), 0, 0, 0, info.external_attr & 0xFFFFFFFF,
                0xFFFFFFFF if big_off else local_off,
            ))
            out.write(name_b)
            out.write(extra)
            offset = out.tell()
        cd_size = offset - cd_start
        count = len(self._centrals)
        # Больше 65535 записей или оглавление за 4 ГБ — только через ZIP64 EOCD.
        need_z64 = count > 0xFFFF or cd_start > 0xFFFFFFFF or cd_size > 0xFFFFFFFF
        if need_z64:
            z64_off = offset
            out.write(_ZIP64_EOCD.pack(
                0x06064B50, 44, 45, 45, 0, 0, count, count, cd_size, cd_start))
            out.write(_ZIP64_LOCATOR.pack(0x07064B50, 0, z64_off, 1))
        out.write(_EOCD.pack(
            0x06054B50,
            45 if need_z64 else 20,
            45 if need_z64 else 20,
            min(count, 0xFFFF), min(count, 0xFFFF),
            min(cd_size, 0xFFFFFFFF), min(cd_start, 0xFFFFFFFF),
            0,
        ))
        return count

    def close(self) -> None:
        self._out.close()


def _rebuild_zip_parallel(
    items: list[tuple[str, Path]],
    tmp: Path,
    workers: int = 64,
    chunk: int = 512,
) -> int:
    """Пересобрать zip из items в tmp; вернуть число записей.

    Шарды пишутся на диск (не в память): многогигабайтный журнал не
    должен целиком лезть в RAM.  Страй-шарды с прошлых прерванных попыток
    убираем сразу — иначе каждый рестарт посреди пересборки плодил новые.
    """
    for stray in tmp.parent.glob("*.zipshard"):
        stray.unlink(missing_ok=True)
    asm = _ZipAssembler(tmp)
    shards: list[Path] = []
    total = len(items)
    t0 = time.perf_counter()
    done = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for i in range(0, total, chunk):
                packed = list(pool.map(
                    lambda it: _pack_shard(it, tmp.parent),
                    items[i:i + chunk],
                ))
                shards.extend(shard for _, shard in packed)
                for info, shard in packed:
                    asm.add(info, shard)
                    done += 1
                if done % 5000 < chunk or done == total:
                    logger.info(
                        "Пересборка архива: %d/%d файлов (%.0f с)",
                        done, total, time.perf_counter() - t0,
                    )
        return asm.finish()
    finally:
        asm.close()
        for shard in shards:
            shard.unlink(missing_ok=True)

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

    def to_dict(self) -> dict:
        return {
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
    # Images and unsupported formats: no OCR available without the VLM,
    # so they produce empty text.
    return "", 0


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
        # Архивируется между вызовами _archive_results, чтобы не перечитывать
        # оглавление zip после каждого файла.  None — ещё не читали.
        self._archived: set[str] | None = None
        # Полная сверка архива (CRC журналов + поиск недостающих файлов)
        # не чаще, чем раз в archive_sync_interval секунд.
        self._archive_sync_interval = settings.archive_sync_interval
        self._last_archive_sync = 0.0
        # Сигнатуры (size, mtime_ns) живых журналов на момент последней
        # успешной CRC-сверки: совпала сигнатура — пропускаем перечитывание
        # многогигабайтного файла ради CRC.
        self._live_sig: dict[str, tuple[int, int]] = {}

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
        if self.force:
            return processed
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
        try:
            if file_path.is_dir():
                text = convert_html_folder_to_markdown(file_path)
                pages = 1
            elif file_path.suffix.lower() == ".pdf":
                # Текстовый слой в этом корпусе отравлен (битая кодировка,
                # мусор) — игнорируем его ПОЛНОСТЬЮ и всегда идём через
                # VLM-OCR по отрендеренным страницам.
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
            text = to_plain_text(text)
            if not text.strip():
                result = self._skip_result(file_path, relative, timer, file_type)
                self._processed.add(str(relative))
                pbar.set_postfix(file=file_path.name, stage="done")
                return result.to_dict(), []

            document_type = detect_document_type(file_path, text)
            documents = _split_into_documents(text, file_path)

            doc_results: list[dict] = []
            for doc in documents:
                doc_text = doc.get("text", "").strip()
                if not doc_text:
                    continue
                doc_page = int(doc.get("page", 1) or 1)
                rubricators = extract_rubricators(doc_text, page=doc_page)
                # Тексты живут только в txts/ — в results.jsonl (и архиве)
                # записываем только тип и номер рубрикатора.
                doc_results.append({
                    "rubricators": [
                        {"type": r.get("type", ""), "number": r.get("number", "")}
                        for r in rubricators
                    ]
                })

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
            pbar.set_postfix(file=file_path.name, stage="done")
            return result.to_dict(), produced
        except Exception as exc:
            pbar.set_postfix(file=file_path.name, stage="error")
            logger.exception("Failed to process %s", relative)
            result = self._error_result(file_path, relative, timer, file_type, exc)
            return result.to_dict(), []
        finally:
            self._record_stats(timer, result)

    async def _export_txt_bundle(
        self,
        file_path: Path,
        relative: Path,
        documents: list[dict],
        txt_exporter: TxtExporter,
    ) -> list[Path]:
        """Записать txt документа; вернуть пути записанных файлов (для архива).

        Берёт полные документы (с текстом) из сплиттера — в result.documents
        текста уже нет, там только рубрикаторы.
        """
        written: list[Path] = []
        try:
            docs = [d for d in documents if d.get("text", "").strip()]
            if not docs:
                # Файл обработан, но документов не вышло — всё равно пишем
                # пустой TXT, чтобы в выходе (и архиве) была запись о файле.
                path = await asyncio.to_thread(txt_exporter.export, "", name=relative.stem)
                written.append(path)
                return written
            # Flat output: txts/<original name>.txt, no subfolders, no meta.json.
            # Several documents from one collection get _2, _3 ... suffixes.
            base_name = relative.stem
            for index, doc in enumerate(docs):
                doc_text = doc.get("text", "").strip()
                name = f"{base_name}_{index + 1}" if len(docs) > 1 else base_name
                path = await asyncio.to_thread(txt_exporter.export, doc_text, name=name)
                written.append(path)
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

        # Разовая сверка при запуске: докинуть в архив всё, что лежит в output,
        # но ещё не попало в results.zip — результаты прогонов до появления
        # архива, восстановленные вручную файлы и т.п.  На больших output
        # (десятки тысяч txt) пересборка занимает минуты — без этих строк
        # воркер выглядит мёртвым между 'Воркер запущен' и первым циклом.
        logger.info("Сверка архива при запуске (может занять минуты)...")
        sync_start = time.perf_counter()
        self._archive_results(settings.output_root, full_sync=True)
        logger.info(
            "Сверка архива при запуске завершена за %.1f с",
            time.perf_counter() - sync_start,
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
                    result, produced = await self._process_file(item, result_writer, txt_exporter, pbar)
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
                    # Точечная дозапись в архив только что созданных txt —
                    # без сканирования всего output после каждого файла.
                    self._archive_file_results(produced)
                    pbar.update(1)

            try:
                await asyncio.gather(*[_queue_worker() for _ in range(n_workers)])
            finally:
                pbar.close()
            self._stats_writer.flush()
            # Свежий pretty-JSON после каждой пачки, а не только при остановке
            # watch-режима.  Полная сверка архива (CRC журналов + недостающие
            # файлы) — не чаще archive_sync_interval: на больших output
            # перепаковка zip после каждой пачки слишком дорогая.
            result_writer.finalize()
            now = time.monotonic()
            if now - self._last_archive_sync >= self._archive_sync_interval:
                self._archive_results(settings.output_root, full_sync=True)
                self._last_archive_sync = now
            else:
                self._archive_results(settings.output_root)
            logger.info(
                "Пакет завершён: %d файлов за %.1f с (всего обработано: %d)",
                len(pending), time.perf_counter() - batch_start, len(self._processed),
            )
            return len(pending)

        if not watch:
            await _process_batch()
            result_writer.finalize()
            self._stats_writer.flush()
            self._archive_results(settings.output_root, full_sync=True)
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
            self._archive_results(settings.output_root, full_sync=True)

    @staticmethod
    def _archivable(path: Path, output_root: Path) -> bool:
        """В архив идут только текстовые результаты: ``txts/**/*.txt`` и журналы.

        Рендеры страниц, бандлы, картинки и прочие промежуточные артефакты —
        мусор, они в results.zip не попадают.
        """
        if path.name == ARCHIVE_NAME:
            return False
        rel = path.relative_to(output_root)
        if len(rel.parts) == 1:
            return rel.name in LIVE_FILES
        return rel.parts[0] == "txts" and path.suffix.lower() == ".txt"

    def _archive_index(self, archive: Path) -> set[str]:
        """Оглавление архива (лениво читается один раз и кэшируется)."""
        if self._archived is None:
            self._archived = set()
            if archive.exists():
                try:
                    with zipfile.ZipFile(archive, "r") as zf:
                        self._archived = set(zf.namelist())
                except (zipfile.BadZipFile, OSError) as exc:
                    logger.warning("Архив %s не читается (%s) — будет пересобран при полной сверке", archive, exc)
                    self._archived = set()
        return self._archived

    def _archive_file_results(self, paths: list[Path]) -> None:
        """Дозаписать в архив txt одного обработанного документа.

        Быстрый путь после каждого файла: пакуются только что созданные
        файлы, без rglob по всему output.  Журналы и всё пропущенное
        докидывает периодическая :meth:`_archive_results`.
        """
        if not paths:
            return
        output_root = settings.output_root
        archive = output_root / ARCHIVE_NAME
        archived = self._archive_index(archive)
        new_entries: list[tuple[Path, str]] = []
        for path in paths:
            if not path.is_file():
                continue
            arcname = path.relative_to(output_root).as_posix()
            if arcname not in archived:
                new_entries.append((path, arcname))
        if not new_entries:
            return
        mode = "a" if archive.exists() else "w"
        with zipfile.ZipFile(archive, mode, zipfile.ZIP_DEFLATED) as zf:
            for path, arcname in new_entries:
                zf.write(path, arcname)
                archived.add(arcname)

    def _archive_results(self, output_root: Path, full_sync: bool = False) -> None:
        """Обновить сжатый архив ``results.zip`` с результатами.

        Обычный вызов инкрементально дозаписывает файлы, которых ещё нет
        в архиве (результат каждого файла попадает в zip сразу после записи,
        так что при аварийной остановке обработанное не теряется).

        ``full_sync=True`` дополнительно сверяет «живые» журналы
        (``results.jsonl``, ``results_readable.json``, ``stats.jsonl``): они
        растут по ходу прогона, поэтому при расхождении CRC архив
        пересобирается целиком.  Без этого в zip осталась бы первая версия
        журнала.
        """
        archive = output_root / ARCHIVE_NAME
        current: dict[str, Path] = {}
        for path in sorted(output_root.rglob("*")):
            if path.is_file() and self._archivable(path, output_root):
                # as_posix: zip хранит имена с "/", иначе на Windows один и тот
                # же файл выглядит то как "txts\a.txt" (на диске), то как
                # "txts/a.txt" (в архиве) и дозаписывается второй раз.
                current[path.relative_to(output_root).as_posix()] = path

        self._archive_index(archive)

        stale = False
        if full_sync:
            if archive.exists():
                try:
                    with zipfile.ZipFile(archive, "r") as zf:
                        infos = {info.filename: info for info in zf.infolist()}
                    for name in LIVE_FILES:
                        path = current.get(name)
                        if path is None:
                            continue
                        info = infos.get(name)
                        # Быстрая проверка по сигнатуре (size, mtime_ns):
                        # CRC журнала в 5 ГБ читать каждые 60 секунд —
                        # это лишний постоянный I/O на больших прогонах.
                        try:
                            st = path.stat()
                            sig = (st.st_size, st.st_mtime_ns)
                        except OSError:
                            continue
                        if self._live_sig.get(name) == sig:
                            continue
                        if info is None or info.CRC != zlib.crc32(path.read_bytes()):
                            stale = True
                            break
                        self._live_sig[name] = sig
                except (zipfile.BadZipFile, OSError):
                    stale = True
            elif self._archived:
                # Архив удалили снаружи, а результаты на диске остались.
                stale = True

        if stale:
            # Атомарно: пишем во временный файл и переименовываем.  Прямая
            # запись рвалась при рестарте/остановке посреди пересборки —
            # тогда следующий запуск снова видел битый zip и пересобирал
            # (бесконечный цикл «не зип файла» при каждом старте).
            # Ошибка сборки НЕ роняет воркер: честный лог, tmp подчищен,
            # следующая сверка через archive_sync_interval попробует снова.
            tmp = archive.with_name(archive.name + ".tmp")
            t0 = time.perf_counter()
            try:
                count = _rebuild_zip_parallel(
                    sorted(current.items()), tmp,
                    workers=settings.archive_threads,
                )
                os.replace(tmp, archive)
            except Exception as exc:  # noqa: BLE001 — архив не должен ронять воркер
                logger.exception("Пересборка архива не удалась: %s", exc)
                return
            finally:
                tmp.unlink(missing_ok=True)
            self._archived = set(current)
            logger.info(
                "Архив %s пересобран (%d файлов, %.1f с, %d потоков)",
                archive, count, time.perf_counter() - t0, settings.archive_threads,
            )
            return

        if full_sync and archive.exists():
            logger.info(
                "Сверка архива: журналы совпадают, актуально %d файлов",
                len(self._archived or ()),
            )

        new_names = [arcname for arcname in current if arcname not in self._archived]
        if not new_names:
            return
        if not archive.exists():
            # Архива нет, а файлов много (удалили zip, txt остались) —
            # та же атомарная параллельная сборка, иначе ZipFile(mode="w")
            # пишет 80k файлов одним потоком и рвётся каждым рестартом.
            # Ошибка не роняет воркер — попробуем снова на следующей сверке.
            tmp = archive.with_name(archive.name + ".tmp")
            t0 = time.perf_counter()
            try:
                count = _rebuild_zip_parallel(
                    [(name, current[name]) for name in new_names],
                    tmp, workers=settings.archive_threads,
                )
                os.replace(tmp, archive)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Создание архива не удалось: %s", exc)
                return
            finally:
                tmp.unlink(missing_ok=True)
            self._archived.update(new_names)
            logger.info(
                "Архив %s создан (%d файлов, %.1f с, %d потоков)",
                archive, count, time.perf_counter() - t0, settings.archive_threads,
            )
            return
        with zipfile.ZipFile(archive, "a", zipfile.ZIP_DEFLATED) as zf:
            for arcname in new_names:
                zf.write(current[arcname], arcname)
        self._archived.update(new_names)
        logger.info("В архив %s добавлено %d файлов", archive, len(new_names))


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
