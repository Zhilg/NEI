"""Append-only entity store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class EntityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, source_file: str, entities: list[dict[str, Any]]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            for entity in entities:
                record = {"source_file": source_file, **entity}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
