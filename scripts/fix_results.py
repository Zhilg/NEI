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
import json
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
plan_merge = _plaintext.plan_merge
merge_parts = _plaintext.merge_parts

_DOC_SUFFIX_RE = re.compile(r"_\d+$")


def _silence_mupdf() -> None:
    """MuPDF печатает некритичные syntax-ошибки битых PDF прямо в stderr.

    На корпусе из тысяч кривых сканов фаза pdf-requeue превращала лог в
    простыню «MuPDF error: ...».  Глушим; фатальные проблемы всё равно
    приходят исключениями, диагностика доступна через fitz.TOOLS.mupdf_warnings().
    """
    try:
        import fitz  # noqa: PLC0415

        fitz.TOOLS.mupdf_display_errors(False)
        fitz.TOOLS.mupdf_display_warnings(False)
    except Exception:  # noqa: BLE001 — глушение опционально, не должно ломать фазу
        pass


_silence_mupdf()


def _normalize_txt_star(path: Path) -> int:
    t0 = time.perf_counter()
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        cleaned, did = _strip_markers(content)
        if did:
            path.write_text(cleaned, encoding="utf-8")
        return int(did)
    except OSError as exc:
        logger.warning("чистка txt: пропущен %s: %s", path.name, exc)
        return 0
    finally:
        dt = time.perf_counter() - t0
        if dt > 30:
            logger.warning(
                "медленный txt %s: %.1f с (%d байт) — проверь, не повис ли он",
                path.name, dt, path.stat().st_size if path.exists() else -1,
            )


def _strip_markers(text: str) -> tuple[str, bool]:
    """Полная нормализация к plain text; True — текст реально изменился."""
    cleaned = to_plain_text(text)
    return cleaned, cleaned != text


def _load_repair_module():
    """pdf_repair грузим напрямую по пути — как plaintext выше."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "idp_pdf_repair",
        Path(__file__).resolve().parents[1] / "src" / "idp" / "pdf_repair.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pdf_has_text_layer(pdf_path: Path, sample_pages: int = 3) -> bool:
    """True, если в первых страницах есть текстовый слой.

    НИКОГДА не бросает исключений: корпус полон файлов, которые открываются
    fitz'ом, но падают при обращении к страницам (битый page tree и т.п.) —
    для pdf-requeue/wait это просто «не текстовослойный».
    """
    import fitz  # noqa: PLC0415 — pymupdf есть в контейнере воркера

    repaired: Path | None = None
    doc = None
    try:
        try:
            doc = fitz.open(str(pdf_path))
        except Exception as exc:  # noqa: BLE001 — пробуем починить, иначе пропускаем
            try:
                size = pdf_path.stat().st_size
            except OSError:
                size = -1
            if size == 0:
                kind = "пустой файл"
            elif size > 0 and not pdf_path.open("rb").read(5).startswith(b"%PDF"):
                kind = "не PDF (нет заголовка %PDF)"
            else:
                kind = "битая структура (truncated/xref/encrypt)"
            logger.warning("не открылся %s: %s [%s, %d байт]", pdf_path.name, exc, kind, size)
            repaired = _load_repair_module().repair_pdf(pdf_path)
            if repaired is None:
                return False
            try:
                doc = fitz.open(str(repaired))
            except Exception:  # noqa: BLE001 — и после ремонта не открылся
                return False
        try:
            page_count = doc.page_count
        except Exception:  # noqa: BLE001 — открылся, но page tree битый
            logger.warning("не открылся %s: page_count упал — битый page tree", pdf_path.name)
            return False
        for i in range(min(sample_pages, page_count)):
            try:
                if doc.load_page(i).get_text().strip():
                    return True
            except Exception:  # noqa: BLE001 — одна битая страница не решает за файл
                continue
        return False
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass
        if repaired is not None:
            repaired.unlink(missing_ok=True)


def _has_text_layer_star(pdf_path: Path) -> tuple[Path, bool]:
    return pdf_path, _pdf_has_text_layer(pdf_path)


# Быстрый префильтр: достаём значение корневого "file" без полного парсинга
# JSON.  Работаем с байтами — memchr/re по C, в разы дешевле json.loads на
# строках по 20–200 КБ (старый формат журнала).
_QUICK_FILE_RE = re.compile(rb'"file"\s*:\s*"([^"]*)"')


def _classify_journal_line(line: bytes, stems: frozenset) -> bytes | None:
    """Решение по одной строке журнала: None — выкинуть, иначе байты с \\n.

    Логика фильтрации — точная копия бывшего однопоточного шага 2/3
    (идемпотентность: дропаем ТОЛЬКО записи старого формата со встроенными
    текстами по текстовослойным pdf).  Дешёвые отсевы (нет '"text"' / не pdf /
    stem не в списке) гарантированно keep — окончательное решение по дропу
    принимает только полный json.loads.
    """
    if b'"text"' not in line:
        return line if line.endswith(b"\n") else line + b"\n"
    m = _QUICK_FILE_RE.search(line)
    if m is not None:
        fpath_b = m.group(1)
        if not fpath_b.lower().endswith(b".pdf"):
            return line if line.endswith(b"\n") else line + b"\n"
        stem_b = fpath_b[:-4].rsplit(b"/", 1)[-1].rsplit(b"\\", 1)[-1]
        if stem_b.decode("utf-8", errors="replace") not in stems:
            return line if line.endswith(b"\n") else line + b"\n"
    s = line.decode("utf-8", errors="replace").strip()
    try:
        rec = json.loads(s)
    except ValueError:
        return line if line.endswith(b"\n") else line + b"\n"
    fpath = str(rec.get("file", ""))
    # Идемпотентность: выкидываем ТОЛЬКО записи старого формата (тексты
    # документов внутри).  Схлопнутые записи после фазы format
    # ({"rubricators": ...}) и записи быстрого пути (source=textlayer —
    # чистый слой забран осознанно) не трогаем, иначе каждый перезапуск
    # гонял бы одни и те же pdf по кругу.
    has_text = any(
        isinstance(d, dict) and "text" in d and d.get("source") != "textlayer"
        for d in (rec.get("documents") or [])
    )
    if (
        has_text
        and rec.get("status") in ("ok", "skip")
        and fpath.lower().endswith(".pdf")
        and Path(fpath).stem in stems
    ):
        return None
    return line if line.endswith(b"\n") else line + b"\n"


def _classify_batch(
    batch: list[bytes], stems: frozenset
) -> tuple[list[bytes | None], int, int]:
    out: list[bytes | None] = []
    kept = dropped = 0
    for ln in batch:
        # Пустые строки — как в старом цикле: не пишутся и не считаются
        # ни в kept, ни в dropped.
        if not ln.strip():
            out.append(None)
            continue
        res = _classify_journal_line(ln, stems)
        out.append(res)
        if res is None:
            dropped += 1
        else:
            kept += 1
    return out, kept, dropped


def _filter_journal(
    results_path: Path, stems: set, out, threads: int = 32
) -> tuple[int, int]:
    """Параллельная фильтрация results.jsonl с полоской по байтам.

    Читаем бинарными чанками, сплитим на строки, json.loads жуётся пулом
    потоков (порядок строк в журнале не важен для семантики, но мы его
    сохраняем — батчи пишутся строго в исходном порядке).  На 5ГБ журнале
    даёт ~10x против однопоточного цикла.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    from tqdm import tqdm  # noqa: PLC0415

    frozen = frozenset(stems)
    size = results_path.stat().st_size
    total_kept = total_dropped = 0
    with open(results_path, "rb") as f, ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {}
        buf = b""
        batch: list[bytes] = []
        idx = 0
        with tqdm(total=size, unit="B", unit_scale=True, desc="фильтр jsonl") as bar:
            while True:
                chunk = f.read(64 * 1024 * 1024)
                if not chunk:
                    break
                bar.update(len(chunk))
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines.pop()  # хвост без \n — ждём следующий чанк
                for ln in lines:
                    batch.append(ln)
                    if len(batch) >= 2000:
                        futures[pool.submit(_classify_batch, batch, frozen)] = idx
                        idx += 1
                        batch = []
            if buf:
                batch.append(buf)
            if batch:
                futures[pool.submit(_classify_batch, batch, frozen)] = idx
                idx += 1
        results: dict[int, tuple] = {}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
        write_buf: list[bytes] = []
        for i in range(idx):
            out_lines, kept, dropped = results[i]
            total_kept += kept
            total_dropped += dropped
            write_buf.extend(ln for ln in out_lines if ln is not None)
            if len(write_buf) >= 4096:
                out.writelines(write_buf)
                write_buf.clear()
        if write_buf:
            out.writelines(write_buf)
    return total_kept, total_dropped


def phase_pdf_requeue(input_root: Path, output_root: Path) -> None:
    t0 = time.perf_counter()
    results_path = output_root / "results.jsonl"
    txts_root = output_root / "txts"

    # 1. Какие PDF в input вообще имеют текстовый слой (16 потоков —
    #    fitz-извлечение текста C-bound и отпускает GIL, потоков достаточно;
    #    ProcessPool на Windows ломался под spawn — не используем).
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    logger.info("Шаг 1/3: скан input на PDF с текстовым слоем (16 потоков)...")
    from tqdm import tqdm  # noqa: PLC0415

    pdfs = [p for p in sorted(input_root.rglob("*.pdf")) if p.is_file()]
    stems: set[str] = set()
    from concurrent.futures import as_completed  # noqa: PLC0415

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(_has_text_layer_star, p): p for p in pdfs}
        bar = tqdm(total=len(futures), desc="скан pdf", unit="файл")
        for fut in as_completed(futures):
            bar.update(1)
            path, has = fut.result()
            if has:
                stems.add(path.stem)
        bar.close()
    n_pdfs = len(pdfs)
    logger.info(
        "Шаг 1/3 готов: %d pdf в input, из них с текстовым слоем (на переработку): %d (%.1f с)",
        n_pdfs, len(stems), time.perf_counter() - t0,
    )
    if not stems:
        logger.info("Перерабатывать нечего, фаза pdf-requeue завершена.")
        return

    # 2. Выкинуть их записи из results.jsonl (только ok/skip — error и так
    #    переработаются, processed-сет строится только из ok/skip).
    logger.info("Шаг 2/3: фильтрация results.jsonl (32 потока, полоска по байтам)...")
    t1 = time.perf_counter()
    tmp = results_path.with_suffix(".jsonl.new")
    with open(tmp, "wb") as g:
        kept, dropped = _filter_journal(results_path, stems, g)
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


def phase_archive(input_root: Path, output_root: Path) -> None:
    """Пересобрать results.zip с нуля из текущего output.

    Только текстовые результаты (txts/**) и живые журналы — по правилам
    _archivable воркера.  Старый архив и шарды убираются, сборка идёт во
    временный файл и атомарно подменяет готовый.
    """
    from idp.worker import ARCHIVE_NAME, Worker, _rebuild_zip_parallel  # noqa: PLC0415

    logger.info("Фаза archive: сборка %s из %s...", ARCHIVE_NAME, output_root)
    t0 = time.perf_counter()
    items: list[tuple[str, Path]] = []
    for p in sorted(output_root.rglob("*")):
        if p.is_file() and Worker._archivable(p, output_root):
            arcname = p.relative_to(output_root).as_posix()
            items.append((arcname, p))
    logger.info("В архив пойдёт %d файлов", len(items))

    archive = output_root / ARCHIVE_NAME
    tmp = output_root / (ARCHIVE_NAME + ".tmp")
    count = _rebuild_zip_parallel(items, tmp, workers=64)
    for shard in output_root.glob("*.zipshard"):
        shard.unlink()
    os.replace(tmp, archive)
    logger.info(
        "Фаза archive готова: %d записей, %.2f ГБ за %.0f с -> %s",
        count, archive.stat().st_size / 2**30, time.perf_counter() - t0, archive,
    )


def phase_merge(input_root: Path, output_root: Path) -> None:
    """Объединить разбитые на части документы, если это не газета/сборник.

    Группы вида ``base.txt``, ``base_2.txt`` ... ``base_N.txt`` проверяются
    эвристикой plan_merge: книга, расколотая на главы, склеивается в
    ``base.txt``; сборники статей и газеты остаются разбитыми.
    """
    logger.info("Фаза merge: поиск разбитых на части документов...")
    t0 = time.perf_counter()
    txts_root = output_root / "txts"
    if not txts_root.is_dir():
        logger.info("txts/ нет — нечего объединять.")
        return

    groups: dict[str, list[Path]] = {}
    for txt in txts_root.glob("*.txt"):
        if not txt.is_file():
            continue  # директория с расширением .txt — не часть документа
        base = txt.stem
        m = _DOC_SUFFIX_RE.search(base)
        key = _DOC_SUFFIX_RE.sub("", base) if m else base
        groups.setdefault(key, []).append(txt)

    merged = kept_split = 0
    from tqdm import tqdm  # noqa: PLC0415

    for key, paths in tqdm(sorted(groups.items()), desc="merge", unit="группа"):
        if len(paths) < 2:
            continue
        # Части в порядке следования: base, base_2, base_3 ...
        def sort_key(p: Path) -> tuple[int, str]:
            m = _DOC_SUFFIX_RE.search(p.stem)
            return (int(m.group(0)[1:]) if m else 1), p.name

        parts_paths = sorted(paths, key=sort_key)
        try:
            parts = [p.read_text(encoding="utf-8", errors="replace") for p in parts_paths]
        except OSError as exc:
            # Одна недочитанная часть (пропала/занята/права) не должна валить
            # фазу целиком — группа остаётся как есть, проблема видна в логе.
            logger.warning("merge: группа %s пропущена, часть не читается: %s", key, exc)
            continue
        if plan_merge(parts):
            combined = merge_parts(parts)
            tmp = txts_root / f"{key}.merge.tmp"
            tmp.write_text(combined + "\n", encoding="utf-8")
            for p in parts_paths:
                p.unlink()
            tmp.replace(txts_root / f"{key}.txt")
            merged += 1
            if merged % 500 == 0:
                logger.info("Объединено %d групп (%.0f с)...", merged, time.perf_counter() - t0)
        else:
            kept_split += 1
    logger.info(
        "Фаза merge готова: %d групп объединено, %d оставлены разбитыми (сборники/газеты) за %.0f с",
        merged, kept_split, time.perf_counter() - t0,
    )


def phase_format(input_root: Path, output_root: Path) -> None:
    t0 = time.perf_counter()
    results_path = output_root / "results.jsonl"
    txts_root = output_root / "txts"

    # 1. Почистить txt на диске (32 потока — чтение/запись мелких файлов).
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    logger.info("Шаг 1/2: чистка маркеров в txts/ (32 потока)...")
    from tqdm import tqdm  # noqa: PLC0415

    t1 = time.perf_counter()
    txt_paths = [p for p in txts_root.glob("*.txt") if p.is_file()] if txts_root.is_dir() else []
    changed = 0
    from concurrent.futures import as_completed  # noqa: PLC0415

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = {pool.submit(_normalize_txt_star, p): p for p in txt_paths}
        bar = tqdm(total=len(futures), desc="чистка txt", unit="файл")
        for fut in as_completed(futures):
            bar.update(1)
            changed += fut.result()
        bar.close()
    logger.info("Шаг 1/2 готов: %d txt просмотрено, %d почищено (%.1f с)", len(txt_paths), changed, time.perf_counter() - t1)

    # 2. Схлопнуть встроенные тексты документов в results.jsonl: оставить
    #    только рубрикаторы.  5-гигабайтный журнал ужимается до десятков МБ.
    logger.info("Шаг 2/2: схлопывание текстов в results.jsonl...")
    t2 = time.perf_counter()
    import json  # noqa: PLC0415

    tmp = results_path.with_suffix(".jsonl.new")
    n_rec = n_rec_changed = 0
    total_bytes = results_path.stat().st_size
    with open(results_path, encoding="utf-8") as f, open(tmp, "w", encoding="utf-8") as g:
        bar = tqdm(total=total_bytes, desc="журнал", unit="Б", unit_scale=True)
        for line in f:
            bar.update(len(line.encode("utf-8")))
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
    parser.add_argument("phase", choices=["pdf-requeue", "merge", "format", "archive", "all"])
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
    if args.phase in ("merge", "all"):
        phase_merge(input_root, output_root)
    if args.phase in ("format", "all"):
        phase_format(input_root, output_root)
    if args.phase == "archive":
        phase_archive(input_root, output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
