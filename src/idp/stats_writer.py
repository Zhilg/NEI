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
        record = {**kwargs}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class FileTimer:
    def __init__(self, stats_writer: StatsWriter, file_name: str, file_type: str, size_bytes: int, pages: int | None) -> None:
        self.stats_writer = stats_writer
        self.file_name = file_name
        self.file_type = file_type
        self.size_bytes = size_bytes
        self.pages = pages
        self.start = time.perf_counter()

    def finish(self, status: str, error: str | None = None) -> None:
        duration = time.perf_counter() - self.start
        self.stats_writer.record(
            file=self.file_name,
            type=self.file_type,
            size_bytes=self.size_bytes,
            pages=self.pages,
            duration_sec=round(duration, 3),
            status=status,
            error=error,
        )
