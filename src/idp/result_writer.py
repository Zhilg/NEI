"""Unified per-file result writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ResultWriter:
    def __init__(self, path: Path, pretty_path: Path | None = None) -> None:
        self.path = path
        self.pretty_path = pretty_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buffer: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self._buffer.append(record)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.pretty_path is not None:
            self._flush_pretty()

    def _flush_pretty(self) -> None:
        tmp = self.pretty_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._buffer, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp.replace(self.pretty_path)
