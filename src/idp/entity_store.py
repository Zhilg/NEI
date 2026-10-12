"""Entity store written as a single JSON file grouped by source file."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The whole file is rewritten on flush, so a long run must not flush per file.
_FLUSH_EVERY = 50


class EntityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = self._load()
        self._dirty = False
        self._appends_since_flush = 0

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def append(self, source_file: str, paragraphs: list[dict[str, Any]], entities: list[dict[str, Any]], annotation: str = "") -> None:
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
        self._data[source_file] = {"paragraphs": paragraphs, "entities": unique_entities, "annotation": annotation}
        self._dirty = True
        self._appends_since_flush += 1
        if self._appends_since_flush >= _FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            self._dirty = False
            self._appends_since_flush = 0
        except OSError as exc:
            logger.warning("Cannot write entity store %s: %s", self.path, exc)

    def update_schema(self, new_types: list[dict]) -> None:
        from idp.entity_schema import update_entity_schema

        update_entity_schema(new_types)

    def __del__(self) -> None:
        if self._dirty:
            try:
                self.flush()
            except Exception:
                pass
