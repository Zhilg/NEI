"""Сверка результатов из output: что переработать, что отправить в карантин.

Принципиальное отличие от старого pdf-requeue/wait_pdf_reprocess: input НЕ
сканируется целиком.  Источник истины — output (results.jsonl + txts/):

  verify — один проход по журналу (параллельный, байтовый):
    * запись ok без txt на диске  -> файл на переработку (записи выкидываются,
      воркер при старте переработает, т.к. processed-сет строится из ok/skip);
    * запись error                -> открываем ТОЛЬКО этот файл из input:
        - живой                   -> на переработку (записи выкидываются,
          воркер ретраит файл сам, т.к. processed-сет строится из ok/skip);
        - мёртвый (0 байт / не PDF / не открывается даже после ремонта)
                                  -> карантин: записи заменяются одним skip с
                                    пометкой, файл больше никого не блокирует;
    * здоровые записи не трогаем вообще.

  wait — ждать, пока воркер добьёт список из verify (опрос хвоста журнала,
    input не открывается НИКОГДА).  Таймаут 24 ч, код 1 по таймауту.

Запуск (внутри контейнера воркера, как fix_results.py):

    python /workspace/scripts/verify_reprocess.py verify
    python /workspace/scripts/verify_reprocess.py wait
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("verify_reprocess")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fix_results  # noqa: E402

_QUICK_FILE_RE = fix_results._QUICK_FILE_RE
_filter_journal = fix_results._filter_journal
_silence_mupdf = fix_results._silence_mupdf

import re  # noqa: E402

_QUICK_STATUS_RE = re.compile(rb'"status"\s*:\s*"([^"]+)"')

_silence_mupdf()

STATE_NAME = "verify_state.json"
QUARANTINE_NAME = "quarantine.txt"
POLL_SEC = 120
TIMEOUT_SEC = 24 * 3600

_DOC_SUFFIX_RE = fix_results._DOC_SUFFIX_RE


_QUICK_TXTS_RE = re.compile(rb'"txts"\s*:\s*\[([^\]]*)\]')
_QUICK_STR_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"')


def _extract_file_status(line: bytes) -> tuple[str, str] | None:
    """(file, status) из строки журнала без полного json.loads, если можно."""
    mf = _QUICK_FILE_RE.search(line)
    ms = _QUICK_STATUS_RE.search(line)
    if mf is not None and ms is not None:
        return (
            mf.group(1).decode("utf-8", errors="replace"),
            ms.group(1).decode("utf-8", errors="replace"),
        )
    try:
        rec = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    f = rec.get("file")
    s = rec.get("status")
    if isinstance(f, str) and isinstance(s, str):
        return f, s
    return None


def _scan_statuses(results_path: Path) -> dict[str, str]:
    """Последний статус по каждому файлу (порядок строк = хронология)."""
    last: dict[str, str] = {}
    if not results_path.exists():
        return last
    with open(results_path, "rb") as f:
        for line in f:
            fs = _extract_file_status(line)
            if fs is not None:
                last[fs[0]] = fs[1]
    return last


def _scan_txt_owners(results_path: Path) -> dict[str, list[str]]:
    """Последние значения поля txts (стебли записанных txt) по файлу.

    Быстрый байтовый проход — полный json.loads на многогигабайтном журнале
    не нужен: поле txts короткое и всегда на одной строке с записью.
    """
    owners: dict[str, list[str]] = {}
    if not results_path.exists():
        return owners
    with open(results_path, "rb") as f:
        for line in f:
            fs = _extract_file_status(line)
            if fs is None:
                continue
            rel = fs[0]
            mt = _QUICK_TXTS_RE.search(line)
            if mt is None:
                continue
            stems = []
            for m in _QUICK_STR_RE.finditer(mt.group(1)):
                try:
                    stems.append(json.loads(b'"' + m.group(1) + b'"'))
                except ValueError:
                    stems.append(m.group(1).decode("utf-8", errors="replace"))
            owners[rel] = stems
    return owners


def _txt_bases(txts_root: Path) -> set[str]:
    """Стебли всех txt: base для book.txt и book_2.txt — book.

    Переименованные по теме файлы сопоставляются с журналом через
    ``_names_map.json`` (исходный стебель -> новое имя): ок-записи живут
    под исходными именами входных файлов."""
    bases: set[str] = set()
    if not txts_root.is_dir():
        return bases
    for p in txts_root.glob("*.txt"):
        stem = p.stem
        bases.add(stem)
        m = _DOC_SUFFIX_RE.search(stem)
        if m:
            bases.add(_DOC_SUFFIX_RE.sub("", stem))
    map_path = txts_root / "_names_map.json"
    if map_path.is_file():
        try:
            for orig in json.loads(map_path.read_text(encoding="utf-8")):
                bases.add(orig)
        except (OSError, ValueError) as exc:
            logger.warning("карта имён %s не читается: %s", map_path, exc)
    return bases


def _input_dead_reason(input_root: Path, rel: str) -> str | None:
    """None — файл жив; иначе причина, по которой его не обработать.

    input открываем только здесь и только для записей с проблемами.
    """
    p = input_root / rel
    if not p.is_file():
        return "нет в input"
    if p.stat().st_size == 0:
        return "пустой файл (0 байт)"
    head = p.open("rb").read(1024)
    if b"%PDF" not in head:
        return "не PDF (нет заголовка %PDF)"
    try:
        import fitz  # noqa: PLC0415

        doc = fitz.open(str(p))
        try:
            if doc.page_count == 0:
                return "0 страниц"
        finally:
            doc.close()
        return None
    except Exception as exc:  # noqa: BLE001 — чиним и пробуем ещё раз
        try:
            from idp.pdf_repair import repair_pdf  # noqa: PLC0415

            repaired = repair_pdf(p)
        except Exception:  # noqa: BLE001 — ремонт недоступен/не помог
            repaired = None
        if repaired is not None:
            try:
                import fitz  # noqa: PLC0415

                doc = fitz.open(str(repaired))
                doc.close()
                return None
            except Exception as exc2:  # noqa: BLE001
                return f"не открывается: {exc2}"
        return f"не открывается: {exc}"


def _quarantine_record(rel: str, reason: str) -> str:
    rec = {
        "file": rel,
        "type": "pdf",
        "status": "skip",
        "error": None,
        "documents": [],
        "document_type": "other",
        "quarantine": reason,
    }
    return json.dumps(rec, ensure_ascii=False) + "\n"


def cmd_verify(input_root: Path, output_root: Path) -> int:
    t0 = time.perf_counter()
    results_path = output_root / "results.jsonl"
    txts_root = output_root / "txts"
    state_path = output_root / STATE_NAME

    logger.info("Сканирую журнал (output-only)...")
    last = _scan_statuses(results_path)
    owners = _scan_txt_owners(results_path)
    logger.info("Записей в журнале: %d (%.1f с)", len(last), time.perf_counter() - t0)

    bases = _txt_bases(txts_root)
    problems: dict[str, str] = {}
    for rel, status in last.items():
        if status == "error":
            problems[rel] = status
        elif status == "ok" and Path(rel).stem not in bases:
            problems[rel] = "ok без txt"

    if not problems:
        logger.info("Проблем нет: каждая ok-запись имеет txt, error-записей нет.")
        state_path.write_text(
            json.dumps(
                {
                    "retry": [],
                    "quarantined": {},
                    "journal_offset": results_path.stat().st_size
                    if results_path.exists()
                    else 0,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        return 0

    logger.info("Проблемных записей: %d — проверяю их в input...", len(problems))
    retry: set[str] = set()
    quarantined: dict[str, str] = {}
    for i, (rel, why) in enumerate(sorted(problems.items()), 1):
        reason = _input_dead_reason(input_root, rel)
        if reason is None:
            retry.add(rel)
            logger.info("  [%d/%d] жив, на переработку: %s (%s)", i, len(problems), rel, why)
        else:
            quarantined[rel] = f"{why}; {reason}"
            logger.info("  [%d/%d] КАРАНТИН: %s (%s; %s)", i, len(problems), rel, why, reason)

    # Переписываем журнал: выкидываем все записи перерабатываемых и
    # карантинных файлов, в конец добавляем одну skip-запись по каждому
    # карантинному (skip входит в processed-сет воркера — ретрая не будет).
    drop = retry | set(quarantined)
    if drop:
        logger.info("Переписываю журнал: выкидываю %d файлов...", len(drop))

        def _classify(line: bytes) -> bytes | None:
            m = _QUICK_FILE_RE.search(line)
            if m is None:
                return line if line.endswith(b"\n") else line + b"\n"
            f = m.group(1).decode("utf-8", errors="replace")
            if f in drop:
                return None
            return line if line.endswith(b"\n") else line + b"\n"

        tmp = results_path.with_suffix(".jsonl.new")
        with open(tmp, "wb") as g:
            _filter_journal(results_path, frozenset(), g, classifier=_classify)
        if quarantined:
            with open(tmp, "ab") as g:
                for rel, reason in sorted(quarantined.items()):
                    g.write(_quarantine_record(rel, reason).encode("utf-8"))
        # txt карантинных НЕ трогаем: карантин отключает только повторную
        # обработку из input, а имеющийся в output текст остаётся в корпусе
        # и улучшается наравне со всеми (чистка, склейка, reflow).
        import os  # noqa: PLC0415

        os.replace(tmp, results_path)
    new_size = results_path.stat().st_size if results_path.exists() else 0

    # Владельцы txt для retry-списка: воркер при переработке ПЕРЕЗАПИШЕТ эти
    # файлы на месте (в т.ч. под переименованным по теме именем), а не плодит
    # _2 рядом со старой версией.  Явное поле txts журнала; для старых
    # записей без него — стебель файла через карту переименования.
    names_map: dict[str, str] = {}
    map_path = txts_root / "_names_map.json"
    if map_path.is_file():
        try:
            names_map = json.loads(map_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("карта имён %s не читается: %s", map_path, exc)
    retry_owners = {
        rel: owners.get(rel) or [names_map.get(Path(rel).stem, Path(rel).stem)]
        for rel in sorted(retry)
    }

    # Отчёт о карантине — рядом с журналом (в архив не попадает:
    # не входит в LIVE_FILES воркера).
    q_path = output_root / QUARANTINE_NAME
    with open(q_path, "a", encoding="utf-8") as q:
        for rel, reason in sorted(quarantined.items()):
            q.write(f"{rel}\t{reason}\n")

    state_path.write_text(
        json.dumps(
            {
                "retry": sorted(retry),
                "quarantined": quarantined,
                "owners": retry_owners,
                "journal_offset": new_size,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    logger.info(
        "verify готов за %.1f с: на переработку %d, карантин %d, состояние: %s",
        time.perf_counter() - t0,
        len(retry),
        len(quarantined),
        state_path,
    )
    return 0


def cmd_wait(output_root: Path, timeout_sec: int = TIMEOUT_SEC) -> int:
    state_path = output_root / STATE_NAME
    results_path = output_root / "results.jsonl"
    if not state_path.exists():
        logger.error("Нет %s — сначала: verify_reprocess.py verify", state_path)
        return 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    retry: set[str] = set(state.get("retry") or [])
    offset: int = int(state.get("journal_offset") or 0)
    if not retry:
        logger.info("Перерабатывать нечего.")
        return 0

    deadline = time.time() + timeout_sec
    logger.info("Жду переработки %d файлов (до %.0f ч)...", len(retry), timeout_sec / 3600)
    stale_polls = 0
    last_count: int | None = None
    errors: dict[str, int] = {}  # файл -> число error-записей за ожидание
    while retry:
        if results_path.exists():
            size = results_path.stat().st_size
            if size < offset:
                offset = 0
            with open(results_path, "rb") as f:
                f.seek(offset)
                for line in f:
                    fs = _extract_file_status(line)
                    if fs is None or fs[0] not in retry:
                        continue
                    if fs[1] in ("ok", "skip"):
                        retry.discard(fs[0])
                    elif fs[1] == "error":
                        errors[fs[0]] = errors.get(fs[0], 0) + 1
                offset = f.tell()
        if len(retry) == last_count:
            stale_polls += 1
        else:
            stale_polls = 0
            last_count = len(retry)
        if stale_polls and stale_polls % 5 == 0:
            logger.warning(
                "Прогресса нет уже %d мин: файлы либо жуются через VLM (текст "
                "появится по завершении), либо падают с error — смотри лог "
                "воркера: docker compose -f infra/compose/local.yml logs worker --tail 50 | grep ERROR",
                stale_polls * POLL_SEC // 60,
            )
        err_hint = ""
        if errors:
            top = sorted(errors.items(), key=lambda kv: -kv[1])[:5]
            err_hint = " | ошибки: " + ", ".join(f"{Path(f).name}x{n}" for f, n in top)
        logger.info(
            "Осталось: %d из %d (%s)%s",
            len(retry),
            len(state.get("retry") or []),
            ", ".join(sorted(retry)[:10]) + ("..." if len(retry) > 10 else ""),
            err_hint,
        )
        if not retry:
            logger.info("Всё переработано.")
            return 0
        if time.time() > deadline:
            logger.error("Таймаут. Не дождались: %d файлов", len(retry))
            return 1
        time.sleep(POLL_SEC)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["verify", "wait"])
    parser.add_argument("--input", type=Path, default=None, help="input root (default: IDP_INPUT_ROOT)")
    parser.add_argument("--output", type=Path, default=None, help="output root (default: IDP_OUTPUT_ROOT)")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SEC, help="таймаут wait, секунд")
    args = parser.parse_args()

    input_root, output_root = args.input, args.output
    if input_root is None or output_root is None:
        sys.path.insert(0, "/workspace/src")
        from idp.config import settings  # noqa: PLC0415

        input_root = input_root or settings.input_root
        output_root = output_root or settings.output_root

    if args.cmd == "verify":
        return cmd_verify(input_root, output_root)
    return cmd_wait(output_root, timeout_sec=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
