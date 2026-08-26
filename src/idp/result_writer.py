"""Unified per-file result writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ResultWriter:
    def __init__(self, path: Path, pretty: bool = False) -> None:
        self.path = path
        self.pretty = pretty
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            if self.pretty:
                f.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            else:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
