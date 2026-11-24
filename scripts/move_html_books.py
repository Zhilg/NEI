"""Перенести ВСЕ папки из data/input в data/later.

Каждая непосредственная подпапка data/input уезжает в data/later с сохранением
имени; файлы, лежащие прямо в корне input, не трогаются. Никаких файлов не
удаляет: mv в пределах одного диска — мгновенный и обратимый (назад — тем же mv).

Использование:
    python scripts/move_html_books.py            # перенести
    python scripts/move_html_books.py --dry-run  # только показать, что бы поехало
    python scripts/move_html_books.py --input /path/in --later /path/later
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, default=Path("data/input"))
    ap.add_argument("--later", type=Path, default=Path("data/later"))
    ap.add_argument("--dry-run", action="store_true", help="ничего не двигать, только показать")
    args = ap.parse_args()

    src_root: Path = args.input
    dst_root: Path = args.later
    if not src_root.is_dir():
        print(f"Нет {src_root}", file=sys.stderr)
        return 1
    dst_root.mkdir(parents=True, exist_ok=True)

    dirs = sorted(p for p in src_root.iterdir() if p.is_dir())
    if not dirs:
        print("Подпапок в input нет.")
        return 0

    moved = skipped = 0
    for d in dirs:
        dst = dst_root / d.name
        print(f"{'[dry-run] ' if args.dry_run else ''}{d.name}")
        if args.dry_run:
            moved += 1
            continue
        if dst.exists():
            print(f"  ПРОПУСК: в later уже есть {dst}", file=sys.stderr)
            skipped += 1
            continue
        shutil.move(str(d), str(dst))
        moved += 1

    print(f"\nГотово: {moved} {'были бы перенесены' if args.dry_run else 'перенесены'}, "
          f"{skipped} пропущено (уже есть в later) -> {dst_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
