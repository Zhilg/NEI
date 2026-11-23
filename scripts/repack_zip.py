"""Перепаковка zip архива штатным zipfile: чинит архивы, которые Linux
открывает, а Проводник Windows отказывается (нестандартные ручные заголовки,
zip64-разносы и т.п.).

    python scripts/repack_zip.py G:\\results.zip
"""

from __future__ import annotations

import shutil
import sys
import time
import zipfile
from pathlib import Path


def repack(src: Path) -> Path:
    dst = src.with_name(src.stem + "_repacked" + src.suffix)
    t0 = time.perf_counter()
    done = 0
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        dst, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as zout:
        for info in zin.infolist():
            # Переносим только имя и данные: свежие даты/атрибуты ставит zipfile.
            with zin.open(info) as fsrc:
                with zout.open(info.filename, mode="w", force_zip64=True) as fdst:
                    shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
            done += 1
            if done % 2000 == 0:
                print(f"{done} entries, {time.perf_counter() - t0:.0f}s", flush=True)
    print(f"repack done: {done} entries in {time.perf_counter() - t0:.0f}s -> {dst}", flush=True)
    return dst


def main() -> int:
    src = Path(sys.argv[1])
    dst = repack(src)
    with zipfile.ZipFile(dst) as zf:
        bad = zf.testzip()
        print("verify:", "OK" if bad is None else f"BAD: {bad}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
