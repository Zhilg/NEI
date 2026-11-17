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
4. Writes one TXT bundle per document (``text.txt`` + ``meta.json``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from idp.config import settings
from idp.document_classifier import detect_document_type
from idp.docx_converter import extract_docx_text
from idp.html_converter import extract_html_text, convert_html_folder_to_markdown
from idp.pptx_converter import extract_pptx_text
from idp.renderer import extract_pdf_text
from idp.rubricator import extract_rubricators
from idp.result_writer import ResultWriter
from idp.stats_writer import FileTimer, StatsWriter
from idp.txt_export import TxtExporter

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".htm", ".png", ".jpg", ".jpeg"}
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

    def _find_files(self, input_root: Path) -> list[Path]:
        files: list[Path] = []
        dirs: set[Path] = set()
        for path in sorted(input_root.rglob("*")):
            if path.is_dir():
                html_files = list(path.glob("*.html")) + list(path.glob("*.htm"))
                if html_files:
                    dirs.add(path)
            elif path.is_file():
                ext = path.suffix.lower()
                if ext in IGNORED_EXTENSIONS:
                    continue
                if ext in SUPPORTED_EXTENSIONS:
                    files.append(path)
        filtered_files = [f for f in files if not any(f.is_relative_to(d) for d in dirs)]
        return sorted(dirs) + sorted(filtered_files)

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
    ) -> dict:
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
            else:
                text, pages = _extract_text(file_path)
            timer.pages = pages

            if not text.strip():
                result = self._skip_result(file_path, relative, timer, file_type)
                await self._export_txt_bundle(file_path, relative, result, txt_exporter)
                pbar.set_postfix(file=file_path.name, stage="done")
                return result.to_dict()

            document_type = detect_document_type(file_path, text)
            documents = _split_into_documents(text, file_path)

            doc_results: list[dict] = []
            for doc in documents:
                doc_text = doc.get("text", "").strip()
                if not doc_text:
                    continue
                doc_page = int(doc.get("page", 1) or 1)
                rubricators = extract_rubricators(doc_text, page=doc_page)
                doc_results.append({
                    "text": doc_text,
                    "rubricators": rubricators,
                    "page": doc_page,
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
            await self._export_txt_bundle(file_path, relative, result, txt_exporter)
            pbar.set_postfix(file=file_path.name, stage="done")
            return result.to_dict()
        except Exception as exc:
            pbar.set_postfix(file=file_path.name, stage="error")
            logger.exception("Failed to process %s", relative)
            result = self._error_result(file_path, relative, timer, file_type, exc)
            return result.to_dict()
        finally:
            self._record_stats(timer, result)

    async def _export_txt_bundle(
        self,
        file_path: Path,
        relative: Path,
        result: FileResult,
        txt_exporter: TxtExporter,
    ) -> None:
        if result.status != "ok":
            return
        try:
            documents = result.documents
            if not documents:
                return
            # Export each document as its own bundle.  For multi-document
            # sources (collections/journals) every article gets its own
            # txts/ folder so each article is a separate document.
            for index, doc in enumerate(documents):
                suffix = f"_{index + 1}" if len(documents) > 1 else ""
                doc_relative = relative.parent / f"{relative.stem}{suffix}"
                await asyncio.to_thread(
                    txt_exporter.export,
                    file_path,
                    result.type,
                    doc.get("text", ""),
                    relative=doc_relative,
                    pages=result.pages,
                    document_type=result.document_type,
                    rubricators=doc.get("rubricators", []),
                )
        except Exception as exc:
            logger.warning("Txt export failed for %s: %s", file_path.name, exc)

    async def run(self, input_root: Path | None = None) -> None:
        root = input_root or settings.input_root
        self._input_root = root
        settings.output_root.mkdir(parents=True, exist_ok=True)
        files = self._find_files(root)
        result_writer = ResultWriter(
            settings.output_root / "results.jsonl",
            pretty_path=settings.output_root / "results_readable.json",
        )
        txt_exporter = TxtExporter()
        self._stats_writer = StatsWriter(settings.output_root / "stats.jsonl")
        self._processed = self._load_processed()

        pending: list[Path] = []
        for file_path in files:
            if self._already_processed(file_path):
                continue
            pending.append(file_path)
        pending.sort(key=lambda p: p.stat().st_size if p.is_file() else 0)

        logger.info("Found %d file(s), %d pending after cache check", len(files), len(pending))

        if not pending:
            logger.warning("No pending files to process in %s", root)
            result_writer.finalize()
            self._stats_writer.flush()
            return

        pbar = tqdm(total=len(pending), desc="Files", unit="file")

        async def _process_with_semaphore(file_path: Path) -> dict:
            return await self._process_file(file_path, result_writer, txt_exporter, pbar)

        tasks = [_process_with_semaphore(file_path) for file_path in pending]
        try:
            for coro in asyncio.as_completed(tasks):
                result = await coro
                pbar.update(1)
                result_writer.write(result)
                result_writer.flush()
        finally:
            pbar.close()
            result_writer.finalize()
            self._stats_writer.flush()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IDP pipeline: text conversion and rubricator extraction")
    parser.add_argument("--force", action="store_true", help="Ignore processed cache and reprocess all files")
    parser.add_argument("--input", type=Path, default=None, help="Input directory (overrides IDP_INPUT_ROOT)")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    level = getattr(logging, os.getenv("IDP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = Worker(force=args.force)
    await worker.run(input_root=args.input)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
