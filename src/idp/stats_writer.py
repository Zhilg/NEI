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

    def record(self, **kwargs: Any) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(kwargs, ensure_ascii=False) + "\n")


class FileTimer:
    def __init__(self, file_name: str, file_type: str, size_bytes: int, pages: int | None) -> None:
        self.file_name = file_name
        self.file_type = file_type
        self.size_bytes = size_bytes
        self.pages = pages
        self.start = time.perf_counter()
