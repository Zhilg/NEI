"""Entity store written as a single JSON file grouped by source file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class EntityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False

    def append(self, source_file: str, paragraphs: list[dict[str, Any]], entities: list[dict[str, Any]]) -> None:
        seen: set[tuple] = set()
        unique_entities: list[dict[str, Any]] = []
        for entity in entities:
            key = (
                str(entity.get("type", "")),
                str(entity.get("value", "")).strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            unique_entities.append(entity)
        self._data[source_file] = {"paragraphs": paragraphs, "entities": unique_entities}
        self._dirty = True
        self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        self._dirty = False

    def update_schema(self, new_types: list[dict]) -> None:
        from idp.vlm_client import update_entity_schema

        update_entity_schema(new_types)

    def __del__(self) -> None:
        if self._dirty:
            self.flush()