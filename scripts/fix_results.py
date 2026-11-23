"""Переработка накопленных результатов под новый пайплайн.

Два шага (порядок важен):

1. ``pdf-requeue`` — найти в input PDF с текстовым слоем (именно они были
   обработаны через отравленный слой), выкинуть их ok/skip-записи из
   results.jsonl и удалить их txt.  Воркер после рестарта переработает их
   заново — теперь все PDF идут через VLM-OCR.  Сканы (без слоя) не трогаем:
   они уже шли через VLM.  Архив results.zip после чистки удаляется — воркер
   пересоберёт его при старте.

2. ``format`` — привести оставшееся к финальному формату:
   * txt на диске — через нормализатор в чистый plain text (без
     markdown-разметки и служебных маркеров);
   * results.jsonl — тексты документов выкинуты совсем, остаются только
     рубрикаторы (журнал схлопывается с ~5 ГБ до десятков МБ).

Запуск на сервере (внутри контейнера воркера, pymupdf уже там):

    docker compose -f infra/compose/local.yml stop worker
    docker compose -f infra/compose/local.yml run --rm --no-deps \
        -v "$PWD/data:/data" worker \
        python /workspace/scripts/fix_results.py all --input /data/input --output /data/output
    docker compose -f infra/compose/local.yml up -d worker

(пути по умолчанию берутся из IDP_* env контейнера, --input/--output — override)
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("fix_results")

# Тот же нормализатор, что и в пайплайне.  Грузим файл напрямую — пакет idp
# тянет за собой настройки и зависимости, скрипту они не нужны.
_spec = importlib.util.spec_from_file_location(
    "idp_plaintext",
    Path(__file__).resolve().parents[1] / "src" / "idp" / "plaintext.py",
)
_plaintext = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plaintext)
to_plain_text = _plaintext.to_plain_text

_DOC_SUFFIX_RE = re.compile(r"_\d+$")


def _strip_markers(text: str) -> tuple[str, bool]:
    """Полная нормализация к plain text; True — текст реально изменился."""
    cleaned = to_plain_text(text)
    return cleaned, cleaned != text


def _pdf_has_text_layer(pdf_path: Path, sample_pages: int = 3) -> bool:
    import fitz  # noqa: PLC0415 — pymupdf есть в контейнере воркера

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 — битый pdf просто пропускаем
        logger.warning("не открылся %s: %s", pdf_path.name, exc)
        return False
    try:
        for page in list(doc)[:sample_pages]:
            if page.get_text().strip():
                return True
        return False
    finally:
        doc.close()


def phase_pdf_requeue(input_root: Path, output_root: Path) -> None:
    t0 = time.perf_counter()
    results_path = output_root / "results.jsonl"
    txts_root = output_root / "txts"

    # 1. Какие PDF в input вообще имеют текстовый слой.
    logger.info("Шаг 1/3: скан input на PDF с текстовым слоем...")
    stems: set[str] = set()
    n_pdfs = 0
    for p in sorted(input_root.rglob("*.pdf")):
        if not p.is_file():
            continue
        n_pdfs += 1
        if _pdf_has_text_layer(p):
            stems.add(p.stem)
    logger.info(
        "Шаг 1/3 готов: %d pdf в input, из них с текстовым слоем (на переработку): %d (%.1f с)",
        n_pdfs, len(stems), time.perf_counter() - t0,
    )
    if not stems:
        logger.info("Перерабатывать нечего, фаза pdf-requeue завершена.")
        return

    # 2. Выкинуть их записи из results.jsonl (только ok/skip — error и так
    #    переработаются, processed-сет строится только из ok/skip).
    logger.info("Шаг 2/3: фильтрация results.jsonl (это 5ГБ, займёт минуты)...")
    t1 = time.perf_counter()
    tmp = results_path.with_suffix(".jsonl.new")
    kept = dropped = 0
    with open(results_path, encoding="utf-8") as f, open(tmp, "w", encoding="utf-8") as g:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                rec = __import__("json").loads(s)
            except ValueError:
                g.write(line if line.endswith("\n") else line + "\n")
                kept += 1
                continue
            fpath = str(rec.get("file", ""))
            if (
                rec.get("status") in ("ok", "skip")
                and fpath.lower().endswith(".pdf")
                and Path(fpath).stem in stems
            ):
                dropped += 1
                continue
            g.write(line if line.endswith("\n") else line + "\n")
            kept += 1
    os.replace(tmp, results_path)
    logger.info("Шаг 2/3 готов: выкинуто %d записей, осталось %d (%.1f с)", dropped, kept, time.perf_counter() - t1)

    # 3. Удалить их txt.
    logger.info("Шаг 3/3: удаление txt перерабатываемых pdf...")
    t2 = time.perf_counter()
    removed = 0
    if txts_root.is_dir():
        for txt in txts_root.glob("*.txt"):
            base = txt.stem
            if base in stems or (
                _DOC_SUFFIX_RE.search(base)
                and _DOC_SUFFIX_RE.sub("", base) in stems
            ):
                txt.unlink()
                removed += 1
    logger.info("Шаг 3/3 готов: удалено %d txt (%.1f с)", removed, time.perf_counter() - t2)

    # 4. Архив теперь рассинхронизирован с журналом — проще пересобрать с нуля.
    archive = output_root / "results.zip"
    if archive.exists():
        archive.unlink()
        logger.info("Архив %s удалён — воркер пересоберёт его при старте", archive)
    for shard in output_root.glob("*.zipshard"):
        shard.unlink()


def phase_format(input_root: Path, output_root: Path) -> None:
    t0 = time.perf_counter()
    results_path = output_root / "results.jsonl"
    txts_root = output_root / "txts"

    # 1. Почистить txt на диске.
    logger.info("Шаг 1/2: чистка маркеров в txts/...")
    t1 = time.perf_counter()
    changed = 0
    n_txts = 0
    if txts_root.is_dir():
        for txt in txts_root.glob("*.txt"):
            n_txts += 1
            content = txt.read_text(encoding="utf-8", errors="replace")
            cleaned, did = _strip_markers(content)
            if did:
                txt.write_text(cleaned, encoding="utf-8")
                changed += 1
    logger.info("Шаг 1/2 готов: %d txt просмотрено, %d почищено (%.1f с)", n_txts, changed, time.perf_counter() - t1)

    # 2. Схлопнуть встроенные тексты документов в results.jsonl: оставить
    #    только рубрикаторы.  5-гигабайтный журнал ужимается до десятков МБ.
    logger.info("Шаг 2/2: схлопывание текстов в results.jsonl...")
    t2 = time.perf_counter()
    import json  # noqa: PLC0415

    tmp = results_path.with_suffix(".jsonl.new")
    n_rec = n_rec_changed = 0
    with open(results_path, encoding="utf-8") as f, open(tmp, "w", encoding="utf-8") as g:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except ValueError:
                g.write(line if line.endswith("\n") else line + "\n")
                continue
            n_rec += 1
            docs = rec.get("documents")
            if not isinstance(docs, list):
                g.write(line if line.endswith("\n") else line + "\n")
                continue
            slim_docs = [
                {"rubricators": doc.get("rubricators") or []}
                for doc in docs
                if isinstance(doc, dict)
            ]
            if any(set(d.keys()) - {"rubricators"} or "text" in d for d in docs if isinstance(d, dict)):
                rec["documents"] = slim_docs
                n_rec_changed += 1
                g.write(json.dumps(rec, ensure_ascii=False) + "\n")
            else:
                g.write(line if line.endswith("\n") else line + "\n")
    os.replace(tmp, results_path)
    logger.info(
        "Шаг 2/2 готов: %d записей, %d схлопнуто (%.1f с)",
        n_rec, n_rec_changed, time.perf_counter() - t2,
    )
    logger.info("Фаза format завершена за %.1f с", time.perf_counter() - t0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["pdf-requeue", "format", "all"])
    parser.add_argument("--input", type=Path, default=None, help="input root (default: IDP_INPUT_ROOT)")
    parser.add_argument("--output", type=Path, default=None, help="output root (default: IDP_OUTPUT_ROOT)")
    args = parser.parse_args()

    input_root = args.input
    output_root = args.output
    if input_root is None or output_root is None:
        sys.path.insert(0, "/workspace/src")
        from idp.config import settings  # noqa: PLC0415

        input_root = input_root or settings.input_root
        output_root = output_root or settings.output_root

    if args.phase in ("pdf-requeue", "all"):
        phase_pdf_requeue(input_root, output_root)
    if args.phase in ("format", "all"):
        phase_format(input_root, output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
