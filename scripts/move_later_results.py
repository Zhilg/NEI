"""Перекинуть txt перенесённых книг из output/txts в data/later/results.

Логика: папки из data/input уже переехали в data/later (move_html_books.py).
По results.jsonl находим записи, чьи исходники лежали внутри этих папок
(совпадение по первому компоненту относительного пути), и переносим
соответствующие txt из data/output/txts в data/later/results (вместе с
частями вида stem_2.txt). Журнал НЕ трогаем.

Внимание: после этого в основном журнале останутся ok-записи без txt — verify
при следующем прогоне отправит такие файлы в карантин (исходников нет в input).
Если это нежелательно, верни записи назад или не гони verify по ним.

Использование:
    python scripts/move_later_results.py            # перенести
    python scripts/move_later_results.py --dry-run  # только показать объёмы
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

_QUICK_FILE_RE = re.compile(rb'"file"\s*:\s*"([^"]*)"')
_PART_RE = re.compile(r"_\d+$")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--later", type=Path, default=Path("data/later"))
    ap.add_argument("--output", type=Path, default=Path("data/output"))
    ap.add_argument("--dst", type=Path, default=None,
                    help="куда класть txt (default: <later>/results)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    later_root: Path = args.later
    output_root: Path = args.output
    dst_root: Path = args.dst or (later_root / "results")
    results_path = output_root / "results.jsonl"
    txts_root = output_root / "txts"

    if not later_root.is_dir():
        print(f"Нет {later_root}", file=sys.stderr)
        return 1
    if not results_path.is_file():
        print(f"Нет {results_path}", file=sys.stderr)
        return 1
    dst_root.mkdir(parents=True, exist_ok=True)

    # Имена перенесённых папок (то, что раньше было data/input/<name>).
    later_names = {p.name for p in later_root.iterdir() if p.is_dir()}
    if not later_names:
        print(f"В {later_root} нет папок — нечего сопоставлять.")
        return 0

    # Стебли txt по записям журнала, относящимся к перенесённым книгам.
    stems: set[str] = set()
    n_matched = 0
    with open(results_path, "rb") as f:
        for line in f:
            m = _QUICK_FILE_RE.search(line)
            rel = m.group(1).decode("utf-8", errors="replace") if m else ""
            first = re.split(r"[/\\]", rel, maxsplit=1)[0]
            if rel and first in later_names:
                n_matched += 1
                last = re.split(r"[/\\]", rel)[-1]
                stem = re.sub(r"\.[a-z0-9]+$", "", last, flags=re.IGNORECASE)
                stems.add(stem)

    # Индекс txt: stem -> пути (части вида base_2 кладём и на base).
    on_disk: dict[str, list[Path]] = {}
    if txts_root.is_dir():
        for p in txts_root.glob("*.txt"):
            on_disk.setdefault(p.stem, []).append(p)
            base = _PART_RE.sub("", p.stem)
            if base != p.stem:
                on_disk.setdefault(base, []).append(p)

    move_txts: list[Path] = []
    missing: list[str] = []
    for stem in stems:
        paths = on_disk.get(stem) or []
        if not paths:
            missing.append(stem)
        move_txts.extend(paths)

    print(f"Записей по перенесённым книгам: {n_matched} (стеблей: {len(stems)})")
    print(f"txt на перенос: {len(move_txts)}, без txt на диске: {len(missing)}")

    if args.dry_run:
        for s in sorted(missing)[:20]:
            print(f"  [dry-run] нет txt: {s}")
        return 0

    # Перенос (коллизии в dst получают _2, _3 ... — ничего не затираем).
    taken: set[str] = {p.name for p in dst_root.glob("*.txt")}
    moved = 0
    for p in move_txts:
        name = p.name
        if name in taken:
            base, i = p.stem, 2
            while f"{base}_{i}.txt" in taken:
                i += 1
            name = f"{base}_{i}.txt"
        taken.add(name)
        shutil.move(str(p), str(dst_root / name))
        moved += 1

    print(f"\nГотово: {moved} txt -> {dst_root}. Журнал не тронут.")
    if missing:
        print(f"Не найдено txt для {len(missing)} стеблей (список выше).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
