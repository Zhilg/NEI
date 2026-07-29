"""Convert DOCX files to Markdown using mammoth."""

from __future__ import annotations

from pathlib import Path

import mammoth


def convert_docx_to_markdown(docx_path: Path) -> str:
    with open(docx_path, "rb") as f:
        result = mammoth.convert_to_markdown(f)
    return result.value
