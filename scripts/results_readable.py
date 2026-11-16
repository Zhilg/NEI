#!/usr/bin/env python3
"""Конвертировать results.jsonl в читабельный results_readable.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({"_raw": line, "_parse_error": True})
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert results.jsonl to pretty results_readable.json")
    parser.add_argument("input", help="Путь к results.jsonl")
    parser.add_argument("--out", help="Путь к выходному pretty JSON (по умолчанию рядом с input)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Файл не найден: {input_path}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out) if args.out else input_path.with_name("results_readable.json")

    records = read_jsonl(input_path)

    out_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Записей: {len(records)}")
    print(f"Сохранено: {out_path}")


if __name__ == "__main__":
    main()
