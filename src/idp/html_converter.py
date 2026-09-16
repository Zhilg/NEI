"""Convert HTML files to text/markdown for VLM processing.

The HTML source contains a lot of UI/navigation garbage. Instead of trying to
pre-clean it with a crude tag-based parser, we extract ALL text content (tags,
attributes discarded) and let the VLM figure out what is relevant.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path


_NOISE_PATTERNS = [
    re.compile(r"^\s*(?:Home|Главная|Menu|Меню|Search|Поиск|Subscribe|Подписаться|Follow|Читать|Login|Войти|Register|Регистрация)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:Copyright|Все права защищены|©)\s*$", re.IGNORECASE),
]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth: int = 0
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []
        self._heading_buf: str | None = None
        self._heading_level: int = 0

    def _flush_cell(self) -> None:
        if self._current_cell:
            cell_text = " ".join(self._current_cell).strip()
            self._current_row.append(cell_text)
            self._current_cell = []

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._current_row:
            self._table_rows.append(self._current_row)
            self._current_row = []

    def _flush_table(self) -> None:
        self._flush_row()
        if self._table_rows:
            cols = max(len(r) for r in self._table_rows)
            normalized = [r + [""] * (cols - len(r)) for r in self._table_rows]
            md_lines = []
            for i, row in enumerate(normalized):
                md_lines.append("| " + " | ".join(row) + " |")
                if i == 0:
                    md_lines.append("| " + " | ".join(["---"] * cols) + " |")
            self._parts.append("\n".join(md_lines) + "\n")
            self._table_rows = []

    def _maybe_emit_heading(self) -> None:
        if self._heading_buf and self._heading_level:
            level = min(max(self._heading_level, 1), 6)
            self._parts.append(f"\n{'#' * level} {self._heading_buf.strip()}\n")
            self._heading_buf = None
            self._heading_level = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript", "iframe", "svg"):
            self._skip_depth += 1
        if tag in ("button", "input", "select", "textarea", "option", "label", "form", "nav", "aside", "header", "footer"):
            self._skip_depth += 1
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._maybe_emit_heading()
            self._heading_level = int(tag[1])
            self._heading_buf = ""
        if tag == "table":
            self._in_table = True
        if tag in ("tr",):
            if self._in_table:
                self._flush_row()
        if tag in ("td", "th"):
            if self._in_table:
                self._flush_cell()
        if tag in ("p", "div", "br", "li",
                    "blockquote", "title", "caption", "pre", "section",
                    "article", "figure", "figcaption",
                    "td", "th", "tr", "tbody", "thead", "tfoot"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "iframe", "svg"):
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in ("button", "input", "select", "textarea", "option", "label", "form", "nav", "aside", "header", "footer"):
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag == "table":
            self._in_table = False
            self._flush_table()
        if tag in ("tr",):
            if self._in_table:
                self._flush_row()
        if tag in ("td", "th"):
            if self._in_table:
                self._flush_cell()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._maybe_emit_heading()
        if tag in ("p", "div", "li",
                    "blockquote", "title", "caption", "pre", "section",
                    "article", "figure", "figcaption",
                    "td", "th", "tr", "tbody", "thead", "tfoot"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = data.strip()
            if not text:
                return
            if self._heading_buf is not None and self._heading_level:
                self._heading_buf = (self._heading_buf + " " + text).strip()
                return
            if self._in_table:
                self._current_cell.append(text)
                return
            self._parts.append(text)

    def close(self) -> None:
        self._maybe_emit_heading()
        super().close()


def _extract_html_text(html_content: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_content)
    parser.close()
    text = "".join(parser._parts)
    lines = text.splitlines()
    cleaned = [line.strip() for line in lines if line.strip()]
    return "\n\n".join(cleaned)


def convert_html_to_markdown(html_path: Path) -> str:
    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    return _extract_html_text(raw)


def convert_html_folder_to_markdown(folder_path: Path) -> str:
    html_files = sorted(folder_path.glob("*.html"))
    if not html_files:
        html_files = sorted(folder_path.glob("*.htm"))
    if not html_files:
        return ""
    parts: list[str] = []
    for html_file in html_files:
        text = convert_html_to_markdown(html_file)
        if text.strip():
            parts.append(f"<!-- file: {html_file.name} -->\n{text}")
    return "\n\n".join(parts)
