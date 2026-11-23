"""Нормализация txt-результатов в output/txts.

Два входа, один код:
- воркер при старте — все существующие txt сразу в нормализованном виде
  (файлы от старых прогонов чистим и склеиваем автоматически, без ручных
  фаз оркестратора);
- фаза format оркестратора (scripts/fix_results.py) — тем же кодом.

Нормализация = to_plain_text (маркеры «Страница X из Y», колонтитулы,
оглавления, reflow) + склейка книг, разбитых на части (base.txt,
base_2.txt, ...), если это не газета/сборник (plan_merge).
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

try:  # обычный путь — внутри пакета idp
    from idp.plaintext import merge_parts, plan_merge, to_plain_text
except ModuleNotFoundError:  # standalone-загрузка скриптом по пути (fix_results)
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "idp_plaintext", Path(__file__).resolve().parent / "plaintext.py",
    )
    _m = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    to_plain_text = _m.to_plain_text
    plan_merge = _m.plan_merge
    merge_parts = _m.merge_parts

_DOC_SUFFIX_RE = re.compile(r"_\d+$")


def normalize_txt_file(path: Path) -> bool:
    """Нормализовать один txt (in-place).  True — файл реально изменился.

    Не бросает исключений: проблемный файл пропускается с записью в лог.
    """
    t0 = time.perf_counter()
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("нормализация txt: пропущен %s: %s", path.name, exc)
        return False
    cleaned = to_plain_text(content)
    if cleaned != content:
        try:
            path.write_text(cleaned, encoding="utf-8")
        except OSError as exc:
            logger.warning("нормализация txt: не записан %s: %s", path.name, exc)
            return False
    dt = time.perf_counter() - t0
    if dt > 30:
        logger.warning(
            "медленный txt %s: %.1f с (%d байт) — проверь, не повис ли он",
            path.name, dt, path.stat().st_size if path.exists() else -1,
        )
    return cleaned != content


def merge_split_documents(txts_root: Path) -> tuple[int, int]:
    """Склеить книги, разбитые на части.  Возвращает (merged, kept_split).

    Группы вида ``base.txt``, ``base_2.txt`` ... ``base_N.txt``: если
    plan_merge говорит, что это одна книга (не газета/сборник) — части
    объединяются в ``base.txt``.  Не бросает исключений.
    """
    groups: dict[str, list[Path]] = {}
    for txt in txts_root.glob("*.txt"):
        if not txt.is_file():
            continue  # директория с расширением .txt — не часть документа
        base = txt.stem
        m = _DOC_SUFFIX_RE.search(base)
        key = _DOC_SUFFIX_RE.sub("", base) if m else base
        groups.setdefault(key, []).append(txt)

    def sort_key(p: Path) -> tuple[int, str]:
        m = _DOC_SUFFIX_RE.search(p.stem)
        return (int(m.group(0)[1:]) if m else 1), p.name

    merged = kept_split = 0
    items = sorted(groups.items())
    try:
        from tqdm import tqdm  # noqa: PLC0415

        items = tqdm(items, desc="merge", unit="группа")
    except ImportError:
        pass
    for key, paths in items:
        if len(paths) < 2:
            continue
        parts_paths = sorted(paths, key=sort_key)
        try:
            parts = [p.read_text(encoding="utf-8", errors="replace") for p in parts_paths]
        except OSError as exc:
            # Одна недочитанная часть не валит фазу — группа остаётся как есть.
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
        else:
            kept_split += 1
    return merged, kept_split


def clean_all_txts(txts_root: Path, threads: int = 32) -> tuple[int, int]:
    """Только чистка маркеров (без merge): (почищено, всего файлов)."""
    if not txts_root.is_dir():
        return 0, 0
    txts = [p for p in txts_root.glob("*.txt") if p.is_file()]
    t0 = time.perf_counter()
    changed = 0
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        futures = {pool.submit(normalize_txt_file, p): p for p in txts}
        try:
            from tqdm import tqdm  # noqa: PLC0415

            bar = tqdm(total=len(futures), desc="чистка txt", unit="файл")
        except ImportError:
            bar = None
        for fut in as_completed(futures):
            changed += fut.result()
            if bar is not None:
                bar.update(1)
        if bar is not None:
            bar.close()
    logger.info(
        "Чистка txt: %d файлов, %d почищено (%.0f с)",
        len(txts), changed, time.perf_counter() - t0,
    )
    return changed, len(txts)


def normalize_all_txts(txts_root: Path, threads: int = 32) -> tuple[int, int, int]:
    """Полная нормализация каталога: (почищено, склеено, всего файлов)."""
    changed, total = clean_all_txts(txts_root, threads)
    merged, kept = merge_split_documents(txts_root)
    logger.info(
        "Нормализация txt: %d книг склеено, %d разбитых оставлено",
        merged, kept,
    )
    return changed, merged, total
