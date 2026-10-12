"""Unified per-file result writer."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ResultWriter:
    def __init__(self, path: Path, pretty_path: Path | None = None) -> None:
        self.path = path
        self.pretty_path = pretty_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buffer: list[dict[str, Any]] = []
        self._existing = self._load_existing()

    def _load_existing(self) -> list[dict[str, Any]]:
        """Previous JSONL records, so the pretty file survives reruns."""
        records: list[dict[str, Any]] = []
        if not self.path.exists():
            return records
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.warning("Cannot read %s: %s", self.path, exc)
        return records

    def write(self, record: dict[str, Any]) -> None:
        self._buffer.append(record)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def finalize(self) -> None:
        """Rewrite the pretty JSON once per run instead of once per file."""
        if self.pretty_path is None:
            return
        merged: dict[str, dict[str, Any]] = {}
        for record in self._existing + self._buffer:
            key = str(record.get("file", id(record)))
            merged[key] = record
        tmp = self.pretty_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(list(merged.values()), f, ensure_ascii=False, indent=2)
                f.write("\n")
            tmp.replace(self.pretty_path)
        except OSError as exc:
            logger.warning("Cannot write %s: %s", self.pretty_path, exc)
