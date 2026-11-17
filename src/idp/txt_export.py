"""Export plain-text bundles for processed documents.

Every processed document gets its own folder inside ``<output_root>/txts/``,
mirroring the document's path relative to the input root:

    txts/
      <relative dir>/<file name>/
        text.txt   plain document content (no Markdown, no headings)
        meta.json  source info, document type and extracted rubricators

Only the recognised text content is written — no tables, no images, no
Markdown artefacts.  Rubricator codes (УДК, ГРНТИ, ГНТИ with numbers) are
stored in ``meta.json``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from idp.config import settings

logger = logging.getLogger(__name__)

_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _sanitize_name(name: str) -> str:
    cleaned = _UNSAFE_NAME_RE.sub("_", name).strip().strip(".")
    return cleaned or "unnamed"


class TxtExporter:
    """Writes one ``txts/<relative dir>/<file name>/`` bundle per document."""

    def __init__(self, output_root: Path | None = None) -> None:
        self.root = (output_root or settings.output_root) / "txts"
        self._used: set[str] = set()

    def export(
        self,
        source: Path,
        file_type: str,
        text: str,
        *,
        relative: Path,
        pages: int | None = None,
        document_type: str = "other",
        rubricators: list[dict] | None = None,
    ) -> Path:
        bundle_dir = self._bundle_dir(relative)
        shutil.rmtree(bundle_dir, ignore_errors=True)
        bundle_dir.mkdir(parents=True, exist_ok=True)

        text = (text or "").strip()
        (bundle_dir / "text.txt").write_text(f"{text}\n", encoding="utf-8")

        meta = {
            "source": str(relative),
            "type": file_type,
            "size_bytes": source.stat().st_size if source.is_file() else None,
            "pages": pages,
            "document_type": document_type,
            "text_file": "text.txt",
            "rubricators": rubricators or [],
        }
        (bundle_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return bundle_dir

    def _bundle_dir(self, relative: Path) -> Path:
        parts = [_sanitize_name(part) for part in relative.parts if part not in (".", "..")]
        directory = self.root.joinpath(*parts[:-1]) if len(parts) > 1 else self.root
        base = parts[-1] if parts else "unnamed"
        candidate = base
        counter = 2
        while self._bundle_key(directory, candidate) in self._used:
            candidate = f"{base}_{counter}"
            counter += 1
        self._used.add(self._bundle_key(directory, candidate))
        return directory / candidate

    @staticmethod
    def _bundle_key(directory: Path, name: str) -> str:
        return str(directory / name).lower()
