"""Entity schema: runtime read/write copy plus the packaged read-only template.

The writable copy lives in the output directory (``IDP_ENTITY_SCHEMA_PATH``,
default ``<output_root>/entity_schema.json``) because the application source is
mounted read-only at runtime. The JSON shipped next to this module is only a
template that seeds the first copy.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from idp.config import settings

logger = logging.getLogger(__name__)

_PACKAGED_SCHEMA_PATH = Path(__file__).parent / "entity_schema.json"


def _schema_paths() -> tuple[Path, Path]:
    """(writable schema path, packaged template)."""
    resolved = settings.entity_schema_path or settings.output_root / "entity_schema.json"
    return resolved, _PACKAGED_SCHEMA_PATH


def _fallback_schema() -> dict:
    return {
        "version": "entity-v1",
        "entity_types": [
            {"name": "person", "description": "ФИО физического лица"},
            {"name": "organization", "description": "Название организации"},
            {"name": "date", "description": "Дата"},
            {"name": "address", "description": "Адрес"},
            {"name": "identifier", "description": "Идентификатор, номер документа"},
            {"name": "amount", "description": "Сумма, число с единицами"},
            {"name": "sender", "description": "Отправитель"},
            {"name": "recipient", "description": "Получатель, адресат"},
        ],
    }


def _read_schema_file(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("entity_types"):
            return data
    except Exception:
        pass
    return {}


@lru_cache(maxsize=1)
def load_entity_schema() -> dict:
    """Runtime schema: writable output copy when present, else the packaged template."""
    resolved, template = _schema_paths()
    schema = _read_schema_file(resolved) or _read_schema_file(template)
    return schema or _fallback_schema()


def build_entity_type_descriptions() -> str:
    schema = load_entity_schema()
    types = schema.get("entity_types", [])
    if not types:
        return "- other: прочие значимые сущности"
    lines = []
    for item in types:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        description = str(item.get("description", "")).strip()
        lines.append(f"- {name}: {description}" if description else f"- {name}")
    return "\n".join(lines) if lines else "- other: прочие значимые сущности"


def update_entity_schema(new_types: list[dict]) -> None:
    """Append newly seen entity types to the writable schema; never fatal on failure."""
    resolved, template = _schema_paths()
    schema = _read_schema_file(resolved) or _read_schema_file(template) or _fallback_schema()
    existing_names = {
        t["name"] for t in schema.get("entity_types", []) if isinstance(t, dict) and t.get("name")
    }
    changed = False
    for new_type in new_types:
        name = new_type.get("name") if isinstance(new_type, dict) else None
        if name and name not in existing_names:
            schema.setdefault("entity_types", []).append(new_type)
            existing_names.add(name)
            changed = True
    if not changed:
        return
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as exc:
        logger.warning("Cannot write entity schema to %s: %s", resolved, exc)
    load_entity_schema.cache_clear()
