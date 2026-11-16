#!/usr/bin/env python3
"""Сделать results.jsonl читабельным: вывести таблицу или сохранить markdown/JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _fmt(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


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


def print_table(records: list[dict], limit: int | None = None) -> None:
    rows = records if limit is None else records[:limit]
    if not rows:
        print("Нет записей.")
        return

    headers = ["file", "type", "status", "pages", "duration", "error", "annotation"]
    widths = {h: len(h) for h in headers}
    table: list[list[str]] = []
    for r in rows:
        row = [
            r.get("file", "-"),
            r.get("type", "-"),
            r.get("status", "-"),
            str(r.get("pages", "-")),
            str(r.get("duration_sec", "-")),
            (r.get("error") or "-")[:60],
            (r.get("annotation") or "-")[:60],
        ]
        for i, h in enumerate(headers):
            widths[h] = max(widths[h], len(row[i]))
        table.append(row)

    line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(line)
    print("-" * len(line))
    for row in table:
        print(" | ".join(cell.ljust(widths[headers[i]]) for i, cell in enumerate(row)))
    if limit is not None and len(records) > limit:
        print(f"... и ещё {len(records) - limit} записей")


def print_markdown(records: list[dict], out: Path | None = None) -> None:
    parts: list[str] = []
    parts.append("# Результаты обработки документов\n")
    parts.append(f"Всего файлов: {len(records)}\n")
    ok = sum(1 for r in records if r.get("status") == "ok")
    err = sum(1 for r in records if r.get("status") == "error")
    parts.append(f"Успешно: {ok} | Ошибок: {err}\n")

    for r in records:
        parts.append(f"## {r.get('file', 'неизвестно')}\n")
        parts.append(f"- Тип: {r.get('type', '-')}")
        parts.append(f"- Статус: {r.get('status', '-')}")
        parts.append(f"- Страниц: {r.get('pages', '-')}")
        parts.append(f"- Время: {r.get('duration_sec', '-')} сек")
        if r.get("error"):
            parts.append(f"- Ошибка: {r['error']}")
        if r.get("annotation"):
            parts.append(f"- Аннотация: {r['annotation']}")
        entities = r.get("entities") or []
        if entities:
            parts.append(f"- Сущности ({len(entities)}):")
            for e in entities[:10]:
                name = e.get("name", "-")
                etype = e.get("type", "-")
                conf = e.get("confidence", "-")
                parts.append(f"  - {name} ({etype}, confidence={conf})")
        parts.append("")

    text = "\n".join(parts)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"Сохранено в {out}")
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Сделать results.jsonl читабельным")
    parser.add_argument("input", help="Путь к results.jsonl")
    parser.add_argument("--format", choices=["table", "md"], default="table")
    parser.add_argument("--limit", type=int, default=None, help="Сколько записей показать в таблице")
    parser.add_argument("--out", help="Сохранить markdown в файл (тogether с --format md)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Файл не найден: {input_path}", file=sys.stderr)
        sys.exit(1)

    records = read_jsonl(input_path)

    if args.format == "md":
        out = Path(args.out) if args.out else None
        print_markdown(records, out)
    else:
        print_table(records, args.limit)


if __name__ == "__main__":
    main()
