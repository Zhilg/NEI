"""Append-only statistics writer."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class StatsWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buffer: list[str] = []

    def record(self, **kwargs: Any) -> None:
        self._buffer.append(json.dumps(kwargs, ensure_ascii=False))
        if len(self._buffer) >= 50:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n".join(self._buffer) + "\n")
        self._buffer.clear()


class FileTimer:
    def __init__(self, file_name: str, file_type: str, size_bytes: int, pages: int | None) -> None:
        self.file_name = file_name
        self.file_type = file_type
        self.size_bytes = size_bytes
        self.pages = pages
        self.start = time.perf_counter()
