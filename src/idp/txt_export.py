"""Export plain-text files for processed documents.

Every processed document gets its own flat text file in ``<output_root>/txts/``,
named after the original file:

    txts/<original name>.txt   plain document content

For sources that split into several documents (collections, journals, almanacs),
each article gets ``<original name>_2.txt``, ``<original name>_3.txt`` and so on.

There are no subfolders and no sidecar metadata files — only the ``.txt`` file
with the document text. Rubricator codes and document metadata remain in
``results.jsonl`` / ``results_readable.json``.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from idp.config import settings

logger = logging.getLogger(__name__)

_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _sanitize_name(name: str) -> str:
    cleaned = _UNSAFE_NAME_RE.sub("_", name).strip().strip(".")
    return cleaned or "unnamed"


class TxtExporter:
    """Writes one flat ``txts/<original name>.txt`` per document."""

    def __init__(self, output_root: Path | None = None) -> None:
        self.root = (output_root or settings.output_root) / "txts"
        self.root.mkdir(parents=True, exist_ok=True)
        # Занятые имена предзагружаем с диска: экспортер создаётся заново
        # при каждом запуске воркера, и без этого одноимённый файл из другой
        # папки (API/a.pdf и API/API/a.pdf — в корпусе часто) после рестарта
        # перезаписал бы чужой txt.
        self._used: set[str] = {p.stem for p in self.root.glob("*.txt")}
        self._used.discard("")
        # export вызывается из потоков (asyncio.to_thread) параллельно —
        # выбор свободного имени должен быть атомарным.
        self._lock = threading.Lock()

    def export(self, text: str, *, name: str) -> Path:
        """Write ``text`` to ``txts/<name>.txt``.

        ``name`` is the original file stem.  Name collisions (the same stem in
        different folders, or several documents from one collection) get a
        ``_2``, ``_3`` ... suffix so nothing is overwritten.
        """
        base = _sanitize_name(name)
        with self._lock:
            candidate = base
            counter = 2
            while candidate in self._used:
                candidate = f"{base}_{counter}"
                counter += 1
            self._used.add(candidate)
        out_path = self.root / f"{candidate}.txt"
        out_path.write_text((text or "").strip() + "\n", encoding="utf-8")
        return out_path
