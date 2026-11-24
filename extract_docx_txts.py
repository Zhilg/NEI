"""Вытащить из распакованного архива (G:/results) все txt, полученные из Word."""
import json
import re
import shutil
from pathlib import Path

SRC = Path("G:/results")
TXTS = SRC / "txts"
OUT = Path("G:/NEI/docx_txts")
OUT.mkdir(parents=True, exist_ok=True)

quick_file = re.compile(rb'"file"\s*:\s*"([^"]*)"')
quick_type = re.compile(rb'"type"\s*:\s*"([^"]*)"')
quick_status = re.compile(rb'"status"\s*:\s*"([^"]*)"')


def _norm(s: str) -> str:
    """Windows режет точки/пробелы в конце имён файлов — сравниваем нормализованно."""
    return s.rstrip(". ")

docx_stems: dict[str, str] = {}  # stem -> status
with open(SRC / "results.jsonl", "rb") as f:
    for line in f:
        mt = quick_type.search(line)
        if mt is None or mt.group(1) != b"docx":
            continue
        mf = quick_file.search(line)
        ms = quick_status.search(line)
        if mf is None:
            continue
        rel = mf.group(1).decode("utf-8", errors="replace")
        stem = rel.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        stem = re.sub(r"\.docx$", "", stem, flags=re.IGNORECASE)
        stem = _norm(stem)
        status = ms.group(1).decode() if ms else "?"
        # последняя запись побеждает
        docx_stems[stem] = status

print(f"docx-записей в журнале: {len(docx_stems)}")

# индекс txt на диске: stem -> [paths]; у частей вида base_2.txt база тоже индексируется
on_disk: dict[str, list[Path]] = {}
suffix_re = re.compile(r"_\d+$")
for p in TXTS.glob("*.txt"):
    on_disk.setdefault(_norm(p.stem), []).append(p)
    base = suffix_re.sub("", p.stem)
    if base != p.stem:
        on_disk.setdefault(_norm(base), []).append(p)

copied = 0
missing = 0
missing_ok: list[str] = []
with_status: dict[str, int] = {}
for stem, status in sorted(docx_stems.items()):
    with_status[status] = with_status.get(status, 0) + 1
    paths = on_disk.get(stem)
    if not paths:
        missing += 1
        if status == "ok":
            missing_ok.append(stem)
        continue
    for p in paths:
        shutil.copy2(p, OUT / p.name)
        copied += 1

print(f"скопировано txt: {copied}, без txt на диске: {missing}")
print(f"по статусам записей: {with_status}")
print(f"из них ok без txt (реально потерянные): {len(missing_ok)}")
for s in missing_ok[:20]:
    print("  -", s)
(OUT / "_missing_ok.txt").write_text("\n".join(missing_ok), encoding="utf-8")
print(f"итого в {OUT}")
