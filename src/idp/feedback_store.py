"""Read and write operator corrections used as VLM few-shot feedback."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from idp.config import settings

CORRECTION_FIELDS = (
    "page_image",
    "markdown",
    "vlm_entities",
    "operator_corrected_entities",
    "timestamp",
)


def corrections_path() -> Path:
    return Path(settings.operator_corrections_path)


def _iter_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    print(f"Feedback store: skipping malformed line in {path}", file=sys.stderr)
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError as exc:
        print(f"Feedback store: cannot read {path}: {exc}", file=sys.stderr)
        return []
    return records


def read_corrections(path: Path | None = None) -> list[dict]:
    return _iter_records(path or corrections_path())


def recent_corrections(limit: int | None = None, path: Path | None = None) -> list[dict]:
    if limit is None:
        limit = settings.finetune_max_feedback_examples
    if limit <= 0:
        return []
    records = read_corrections(path)
    return records[-limit:]


def append_correction(
    page_image: str,
    markdown: str,
    vlm_entities: list[dict],
    operator_corrected_entities: list[dict],
    timestamp: float,
    path: Path | None = None,
) -> dict:
    target = path or corrections_path()
    record = {
        "page_image": page_image,
        "markdown": markdown,
        "vlm_entities": vlm_entities,
        "operator_corrected_entities": operator_corrected_entities,
        "timestamp": timestamp,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _format_markdown(markdown: str, limit: int = 800) -> str:
    text = str(markdown or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def format_feedback_examples(records: list[dict]) -> str:
    blocks = []
    for i, record in enumerate(records, start=1):
        md = record.get("markdown", "")
        block = f"<EXAMPLE {i}>\n"
        if md:
            block += f"Corrected markdown:\n{_format_markdown(md)}\n"
        block += f"</EXAMPLE {i}>"
        blocks.append(block)
    return "\n\n".join(blocks)


def build_feedback_block(limit: int | None = None) -> str:
    records = recent_corrections(limit=limit)
    if not records:
        return ""
    return (
        "Recent corrections from operator (VLM was wrong, operator corrected it — "
        "follow the operator's values, they are authoritative):\n"
        + format_feedback_examples(records)
    )