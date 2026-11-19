#!/usr/bin/env python3
"""Спасти данные из обрезанной/битой копии zip-архива.

Сценарий: архив скопировали, пока его дописывал воркер, — в копии нет
оглавления в конце файла (EOCD), стандартные unzip/проводник ругаются
«invalid format».  Сами записи при этом целы: идут подряд от начала
файла, каждая начинается с сигнатуры PK\\x03\\x04 с полными размерами
(воркер пишет через zipfile без data descriptor).  Скрипт сканирует
локальные заголовки и вытаскивает всё, что читается; хвост, который
дописывался в момент копирования, пропускается с предупреждением.

Использование:
    python salvage_zip.py битый.zip папка_куда

Нужен только стандартный Python 3.8+, никаких зависимостей.
"""

import mmap
import struct
import sys
import zlib
from pathlib import Path

LHF = b"PK\x03\x04"


def _decode_name(raw: bytes, flag: int) -> str:
    if flag & 0x800:  # UTF-8
        return raw.decode("utf-8", "replace")
    return raw.decode("cp437", "replace")


def salvage(src: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    recovered = 0
    damaged = 0
    skipped = 0
    with src.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        pos = 0
        size = mm.size()
        while True:
            i = mm.find(LHF, pos)
            if i < 0:
                break
            try:
                if i + 30 > size:
                    break
                (_sig, _ver, flag, method, _t, _d,
                 crc, csize, usize, nlen, elen) = struct.unpack_from("<IHHHHHIIIHH", mm, i)
                name_raw = bytes(mm[i + 30:i + 30 + nlen])
                name = _decode_name(name_raw, flag)
                start = i + 30 + nlen + elen
                if csize == 0xFFFFFFFF:
                    # ZIP64: реальные размеры в extra-поле. Копия до 4 ГБ
                    # такого не содержит — если попалось, пропускаем честно.
                    print(f"  !! ZIP64-запись (архив > 4 ГБ) — пропускаю: {name}")
                    skipped += 1
                    pos = i + 4
                    continue
                if start + csize > size:
                    print(f"  .. обрезана на хвосте копии, не добрались: {name}")
                    break
                comp = bytes(mm[start:start + csize])
                if method == 0:
                    raw = comp
                elif method == 8:
                    d = zlib.decompressobj(-15)
                    raw = d.decompress(comp)
                else:
                    print(f"  ?? метод сжатия {method} не поддержан: {name}")
                    skipped += 1
                    pos = start + csize
                    continue
                if zlib.crc32(raw) != crc:
                    print(f"  !! CRC не сошёлся (запись битая): {name}")
                    damaged += 1
                    pos = start + csize
                    continue
                target = out_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                recovered += 1
                pos = start + csize
            except Exception as exc:  # noqa: BLE001 — спасаем максимум
                print(f"  !! ошибка записи на смещении {i}: {exc}")
                damaged += 1
                pos = i + 4
    print(f"\nГотово: восстановлено {recovered}, повреждено {damaged}, "
          f"пропущено {skipped}. Результат: {out_dir.resolve()}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    salvage(Path(sys.argv[1]), Path(sys.argv[2]))
