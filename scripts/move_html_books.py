"""Перенести HTML-книги из input в later.

Книга-коллекция — та же логика, что у воркера при сканировании:
  - папка с именем *.html / *.htm (выгрузки wget), или
  - папка с html-файлами ПРЯМО внутри (корень книги).

Переносятся только верхние папки-книги (вложенные уезжают вместе с родителем),
с сохранением относительной структуры в data/later. Никаких файлов не удаляет:
mv в пределах одного диска — мгновенный и обратимый (переназад — тем же mv).

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

BOOK_EXTS = (".html", ".htm")


def _is_book_dir(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(BOOK_EXTS):
        return True
    try:
        return any(
            p.suffix.lower() in BOOK_EXTS
            for p in path.iterdir()
            if p.is_file()
        )
    except OSError:
        return False


def find_books(root: Path) -> list[Path]:
    """Все папки-книги; вложенные книги внутри уже найденной родителя отсекаем."""
    books: list[Path] = []
    for dirpath, dirnames, _filenames in root.walk():
        p = Path(dirpath)
        # не лезем внутрь найденной книги — она уедет целиком
        if any(p.is_relative_to(b) and p != b for b in books):
            dirnames[:] = []
            continue
        if _is_book_dir(p):
            books.append(p)
            dirnames[:] = []  # вложенное не сканируем
    return books


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

    books = find_books(src_root)
    if not books:
        print("HTML-книг в input не нашлось.")
        return 0

    moved = 0
    for b in sorted(books):
        rel = b.relative_to(src_root)
        dst = dst_root / rel
        print(f"{'[dry-run] ' if args.dry_run else ''}{rel}")
        if args.dry_run:
            moved += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            print(f"  ПРОПУСК: в later уже есть {dst}", file=sys.stderr)
            continue
        shutil.move(str(b), str(dst))
        # подчищаем пустые родительские папки
        parent = b.parent
        while parent != src_root and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        moved += 1

    print(f"\nГотово: {moved} из {len(books)} книг {'были бы перенесены' if args.dry_run else 'перенесены'} в {dst_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
