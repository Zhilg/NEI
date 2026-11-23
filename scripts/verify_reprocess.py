"""Сверка: все ли PDF с текстовым слоем переработаны новым пайплайном.

После прогона оркестратора (pdf-requeue -> воркер -> merge -> format) гоняет
ту же самую проверку текстового слоя, что фаза pdf-requeue, и сопоставляет
с results.jsonl и txts/:

  - pdf с текстовым слоем, записи нет / status error          -> НЕ ГОТОВО
  - запись есть, но documents ещё с встроенными текстами      -> НЕ ГОТОВО
    (старый формат: выкидывала pdf-requeue, значит воркер не переработал)
  - запись схлопнута (новый формат, {"rubricators": ...})    -> ГОТОВО
    + требуется живой txt в output/txts/<stem>.txt|_<N>.txt

Использование (в контейнере воркера пути те же, что у фаз):

    python /workspace/scripts/verify_reprocess.py \
        --input /input --output /output

Полный список недоделанных пишется в <output>/verify_missing.txt.
Exit code 0 — всё переработано, 1 — есть недоделанные (удобно для автоматики).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# fix_results лежит рядом — переиспользуем его _pdf_has_text_layer
# (гарантированно не падает на битых файлах и умеет repair-фолбэк).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fix_results import _has_text_layer_star, _silence_mupdf  # noqa: E402

_DOC_SUFFIX_RE = re.compile(r"_\d+$")


def _find_pdfs(input_root: Path) -> list[Path]:
    """Все pdf независимо от регистра расширения (в корпусе встречаются .PDF)."""
    return sorted(
        p for p in input_root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
    )


def _load_journal(results_path: Path) -> dict[str, dict]:
    """stem -> последняя валидная запись из results.jsonl."""
    records: dict[str, dict] = {}
    if not results_path.is_file():
        return records
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except ValueError:
                continue
            fpath = str(rec.get("file", ""))
            if fpath.lower().endswith(".pdf"):
                records[Path(fpath).stem] = rec
    return records


def _txt_exists(txts_root: Path, stem: str) -> bool:
    if (txts_root / f"{stem}.txt").is_file():
        return True
    if _DOC_SUFFIX_RE.search(stem):
        return False
    # несколько документов из одного pdf -> <stem>_2.txt, _3 ...
    return any(txts_root.glob(f"{stem}_*.txt"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=16)
    args = parser.parse_args()

    _silence_mupdf()
    t0 = time.perf_counter()

    pdfs = _find_pdfs(args.input)
    print(f"pdf в input: {len(pdfs)}, проверяю текстовый слой ({args.threads} потоков)...")

    textlayer: dict[str, Path] = {}  # stem -> полный путь
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = {pool.submit(_has_text_layer_star, p): p for p in pdfs}
        done = 0
        for fut in as_completed(futures):
            done += 1
            if done % 2000 == 0:
                print(f"  скан: {done}/{len(futures)}")
            path, has = fut.result()
            if has:
                textlayer[path.stem] = path

    print(
        f"скан готов за {time.perf_counter() - t0:.1f} с: "
        f"с текстовым слоем (должны быть переработаны): {len(textlayer)}"
    )

    journal = _load_journal(args.output / "results.jsonl")
    txts_root = args.output / "txts"

    ready, missing = 0, []
    for stem, path in sorted(textlayer.items()):
        rec = journal.get(stem)
        reason = None
        if rec is None:
            reason = "нет записи в results.jsonl"
        elif rec.get("status") not in ("ok", "skip"):
            reason = f"status={rec.get('status')}"
        elif any(
            isinstance(d, dict) and "text" in d for d in (rec.get("documents") or [])
        ):
            reason = "запись старого формата (тексты внутри) — не переработан"
        elif not _txt_exists(txts_root, stem):
            reason = "нет txt в output/txts"
        if reason is None:
            ready += 1
        else:
            missing.append((str(path), reason))

    print(f"\nИТОГ: готово {ready}/{len(textlayer)}")
    if missing:
        report = args.output / "verify_missing.txt"
        report.write_text(
            "\n".join(f"{p}\t{r}" for p, r in missing) + "\n", encoding="utf-8"
        )
        print(f"НЕ ГОТОВО: {len(missing)} — полный список: {report}")
        for p, r in missing[:20]:
            print(f"  {p}: {r}")
        if len(missing) > 20:
            print(f"  ... и ещё {len(missing) - 20}")
        return 1
    print("Все текстовослойные pdf переработаны.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
