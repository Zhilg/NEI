"""Convert operator corrections into a LoRA-ready fine-tuning dataset.

Input:  operator_corrections.jsonl (see idp.feedback_store)
Output: finetune_data.jsonl — one sample per line:

    {"images": ["<path>"], "text": "<system prompt>", "target": "<corrected JSON>"}

Training itself (QLoRA on top of the Qwen weights) is out of scope; see
docs/handwriting-finetune.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from idp.config import settings
from idp.feedback_store import read_corrections
from idp.prompts import combined_system_prompt, entity_system_prompt


def _target_text(record: dict) -> str:
    payload = {
        "markdown": str(record.get("markdown", "")),
        "entities": record.get("operator_corrected_entities") or [],
    }
    return json.dumps(payload, ensure_ascii=False)


def _prompt_text(document_type: str = "other", visual: bool = True) -> str:
    return (
        combined_system_prompt(document_type=document_type, test=True)
        + "\n\n"
        + "ENTITY EXTRACTION RULES:\n"
        + entity_system_prompt(document_type=document_type, visual=visual, test=True)
    )


def _is_corrected(record: dict) -> bool:
    corrected = record.get("operator_corrected_entities")
    return isinstance(corrected, list) and bool(corrected)


def build_samples(
    document_type: str = "other",
    corrections_path: Path | None = None,
    include_images: bool = True,
) -> list[dict]:
    prompt = _prompt_text(document_type=document_type)
    samples: list[dict] = []
    for record in read_corrections(corrections_path):
        if not _is_corrected(record):
            continue
        image = str(record.get("page_image", ""))
        sample: dict = {
            "images": [image] if include_images else [],
            "text": prompt,
            "target": _target_text(record),
        }
        samples.append(sample)
    return samples


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a fine-tuning dataset from operator corrections")
    parser.add_argument("--input", default="", help="Path to operator_corrections.jsonl")
    parser.add_argument("--output", default="", help="Path to write finetune_data.jsonl")
    parser.add_argument("--document-type", default="other", help="Document type used to build the training prompt")
    parser.add_argument("--no-images", action="store_true", help="Emit text-only samples (no image paths)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    corrections = Path(args.input) if args.input else Path(settings.operator_corrections_path)
    output = Path(args.output) if args.output else Path(settings.finetune_dataset_path)

    samples = build_samples(
        document_type=args.document_type,
        corrections_path=corrections,
        include_images=not args.no_images,
    )
    if not samples:
        print(f"No operator corrections with entities found in {corrections}", file=sys.stderr)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")
    print(f"Wrote {len(samples)} samples -> {output}")


if __name__ == "__main__":
    main()