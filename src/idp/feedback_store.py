"""Read and write operator corrections used as VLM few-shot feedback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from idp.config import settings


class FeedbackStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(settings.operator_corrections_path)

    def corrections_path(self) -> Path:
        return self.path

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        records: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        print(f"Feedback store: skipping malformed line in {self.path}", file=sys.stderr)
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError as exc:
            print(f"Feedback store: cannot read {self.path}: {exc}", file=sys.stderr)
            return []
        return records

    def recent(self, limit: int | None = None) -> list[dict]:
        if limit is None:
            limit = settings.finetune_max_feedback_examples
        if limit <= 0:
            return []
        records = self.read()
        return records[-limit:]

    def append(self, record: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def build_feedback_block(self, limit: int | None = None) -> str:
        records = self.recent(limit=limit)
        if not records:
            return ""
        blocks = []
        for i, record in enumerate(records, start=1):
            md = record.get("markdown", "")
            block = f"<EXAMPLE {i}>\n"
            if md:
                text = str(md or "").strip()
                if len(text) <= 800:
                    block += f"Corrected markdown:\n{text}\n"
                else:
                    block += f"Corrected markdown:\n{text[:800]}\n... [truncated]\n"
            block += f"</EXAMPLE {i}>"
            blocks.append(block)
        return (
            "Recent corrections from operator (VLM was wrong, operator corrected it — "
            "follow the operator's values, they are authoritative):\n"
            + "\n\n".join(blocks)
        )


_default_store = FeedbackStore()


def corrections_path() -> Path:
    return _default_store.corrections_path()


def read_corrections(path: Path | None = None) -> list[dict]:
    return FeedbackStore(path).read()


def recent_corrections(limit: int | None = None, path: Path | None = None) -> list[dict]:
    return FeedbackStore(path).recent(limit=limit)


def append_correction(
    page_image: str,
    markdown: str,
    vlm_entities: list[dict],
    operator_corrected_entities: list[dict],
    timestamp: float,
    path: Path | None = None,
) -> dict:
    record = {
        "page_image": page_image,
        "markdown": markdown,
        "vlm_entities": vlm_entities,
        "operator_corrected_entities": operator_corrected_entities,
        "timestamp": timestamp,
    }
    return FeedbackStore(path).append(record)


def build_feedback_block(limit: int | None = None) -> str:
    return _default_store.build_feedback_block(limit=limit)
