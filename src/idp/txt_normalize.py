"""Нормализация txt-результатов в output/txts.

Два входа, один код:
- воркер при старте — все существующие txt сразу в нормализованном виде
  (файлы от старых прогонов чистим и склеиваем автоматически, без ручных
  фаз оркестратора);
- фаза format оркестратора (scripts/fix_results.py) — тем же кодом.

Нормализация = to_plain_text (маркеры «Страница X из Y», колонтитулы,
оглавления, reflow) + безусловная склейка ВСЕХ разбитых файлов
(base.txt, base_2.txt, ...), включая книги, газеты и сборники.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

try:  # обычный путь — внутри пакета idp
    from idp.plaintext import (
        extract_title,
        merge_parts,
        sanitize_filename,
        to_plain_text,
    )
except ModuleNotFoundError:  # standalone-загрузка скриптом по пути (fix_results)
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "idp_plaintext", Path(__file__).resolve().parent / "plaintext.py",
    )
    _m = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    to_plain_text = _m.to_plain_text
    merge_parts = _m.merge_parts
    extract_title = _m.extract_title
    sanitize_filename = _m.sanitize_filename

_DOC_SUFFIX_RE = re.compile(r"_\d+$")
NAMES_MAP_FILENAME = "_names_map.json"  # исходный стебель txt -> имя по теме


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


def _merge_one_group(key: str, paths: list[Path], txts_root: Path) -> str:
    """Безусловно склеить одну группу. Возвращает 'merged' | 'skipped'.

    Тип и длина текста больше не влияют на решение: книги, газеты, сборники
    и короткие части объединяются одинаково. Только ошибка ввода-вывода может
    оставить группу несклеенной; она явно попадает в лог и итоговый счётчик.
    """
    def sort_key(p: Path) -> tuple[int, str]:
        m = _DOC_SUFFIX_RE.search(p.stem)
        return (int(m.group(0)[1:]) if m else 1), p.name

    parts_paths = sorted(paths, key=sort_key)
    try:
        parts = [p.read_text(encoding="utf-8", errors="replace") for p in parts_paths]
        combined = merge_parts(parts)
        tmp = txts_root / f"{key}.merge.tmp"
        tmp.write_text(combined + "\n", encoding="utf-8")
        for p in parts_paths:
            p.unlink()
        tmp.replace(txts_root / f"{key}.txt")
    except OSError as exc:
        logger.warning("merge: группа %s НЕ склеена из-за ошибки ввода-вывода: %s", key, exc)
        return "skipped"
    return "merged"


def merge_split_documents(txts_root: Path, threads: int = 16) -> tuple[int, int]:
    """Склеить ВСЕ разбитые документы. Возвращает (merged, skipped_io).

    Каждая группа вида ``base.txt``, ``base_2.txt`` ... ``base_N.txt``
    объединяется в ``base.txt`` без исключений по жанру, типу или длине.
    Группы обрабатываются пулом потоков; каждые 30 секунд в лог пишется
    абсолютный прогресс, скорость и ориентировочное оставшееся время.
    """
    groups: dict[str, list[Path]] = {}
    for txt in txts_root.glob("*.txt"):
        if not txt.is_file():
            continue  # директория с расширением .txt — не часть документа
        base = txt.stem
        m = _DOC_SUFFIX_RE.search(base)
        key = _DOC_SUFFIX_RE.sub("", base) if m else base
        groups.setdefault(key, []).append(txt)

    multi = {k: v for k, v in groups.items() if len(v) >= 2}
    merged = skipped = 0
    items = sorted(multi.items())
    logger.info(
        "merge: найдено %d групп (%d частей) — склеиваю ВСЕ без исключений",
        len(items), sum(len(paths) for _, paths in items),
    )
    t0 = time.perf_counter()
    last_log = t0
    done = 0
    try:
        from tqdm import tqdm  # noqa: PLC0415

        bar = tqdm(total=len(items), desc="merge", unit="группа")
    except ImportError:
        bar = None
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        futures = {
            pool.submit(_merge_one_group, key, paths, txts_root): key
            for key, paths in items
        }
        for fut in as_completed(futures):
            result = fut.result()
            merged += result == "merged"
            skipped += result == "skipped"
            done += 1
            if bar is not None:
                bar.update(1)
            now = time.perf_counter()
            if now - last_log >= 30:
                remaining = len(items) - done
                eta = remaining * (now - t0) / done
                logger.info(
                    "merge: %d/%d групп, осталось %d | склеено %d, ошибок %d | "
                    "прошло %.0f с, ETA %.0f с | последняя: %s",
                    done, len(items), remaining, merged, skipped,
                    now - t0, eta, futures[fut],
                )
                last_log = now
    if bar is not None:
        bar.close()
    logger.info(
        "merge: %d групп, склеено %d, не склеено из-за ошибок %d (%.0f с)",
        len(items), merged, skipped, time.perf_counter() - t0,
    )
    return merged, skipped


def _read_title(path: Path) -> tuple[Path, str | None]:
    """(путь, заголовок|None) — для пула потоков; не бросает исключений."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("переименование: пропущен %s: %s", path.name, exc)
        return path, None
    try:
        return path, extract_title(content)
    except Exception as exc:  # noqa: BLE001 — один кривой файл не валит фазу
        logger.warning("переименование: тема не извлечена %s: %s", path.name, exc)
        return path, None


def rename_by_theme(txts_root: Path, threads: int = 32) -> int:
    """Переименовать txt по теме документа (первый похожий на заголовок текст).

    Имя файла становится названием темы; исходное имя -> новое запоминается
    в ``_names_map.json`` (его читает verify, иначе ок-записи журнала потеряли
    бы свои txt).  Файлы без уверенного заголовка и уже переименованные
    (значения карты) не трогаем.  Не бросает исключений.
    """
    if not txts_root.is_dir():
        return 0
    map_path = txts_root / NAMES_MAP_FILENAME
    mapping: dict[str, str] = {}
    if map_path.is_file():
        try:
            mapping = json.loads(map_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("карта имён %s не читается, начинаю с чистой: %s", map_path, exc)
            mapping = {}
    renamed_new = {v.lower() for v in mapping.values()}

    files = [p for p in txts_root.glob("*.txt") if p.is_file()]
    taken = {p.stem.lower() for p in files}
    candidates: list[tuple[Path, str]] = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        try:
            from tqdm import tqdm  # noqa: PLC0415

            it = tqdm(
                pool.map(_read_title, files),
                total=len(files), desc="темы txt", unit="файл",
            )
        except ImportError:
            it = pool.map(_read_title, files)
        for path, title in it:
            if title is None:
                continue
            stem = path.stem
            if stem in mapping or stem.lower() in renamed_new:
                continue  # уже переименован (или является новым именем другого)
            new_stem = sanitize_filename(title)
            if not new_stem or new_stem.lower() == stem.lower():
                continue
            candidates.append((path, new_stem))

    renamed = 0
    for path, new_stem in candidates:
        candidate = new_stem
        n = 2
        while candidate.lower() in taken:
            candidate = f"{new_stem} ({n})"
            n += 1
        target = path.with_name(f"{candidate}.txt")
        try:
            path.replace(target)
        except OSError as exc:
            logger.warning("переименование: не записан %s: %s", path.name, exc)
            continue
        taken.add(candidate.lower())
        taken.discard(path.stem.lower())
        mapping[path.stem] = candidate
        renamed += 1

    if candidates or not map_path.exists():
        tmp = map_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(map_path)
    logger.info(
        "Переименование по теме: %d файлов из %d (%.0f с)",
        renamed, len(files), time.perf_counter() - t0,
    )
    return renamed


def clean_all_txts(txts_root: Path, threads: int = 32) -> tuple[int, int]:
    """Только чистка маркеров (без merge): (почищено, всего файлов)."""
    if not txts_root.is_dir():
        return 0, 0
    txts = [p for p in txts_root.glob("*.txt") if p.is_file()]
    t0 = time.perf_counter()
    last_log = t0
    changed = done = 0
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        futures = {pool.submit(normalize_txt_file, p): p for p in txts}
        try:
            from tqdm import tqdm  # noqa: PLC0415

            bar = tqdm(total=len(futures), desc="чистка txt", unit="файл")
        except ImportError:
            bar = None
        for fut in as_completed(futures):
            changed += fut.result()
            done += 1
            if bar is not None:
                bar.update(1)
            now = time.perf_counter()
            if now - last_log >= 30:
                remaining = len(txts) - done
                eta = remaining * (now - t0) / done
                logger.info(
                    "Чистка txt: %d/%d, осталось %d, изменено %d | прошло %.0f с, ETA %.0f с",
                    done, len(txts), remaining, changed, now - t0, eta,
                )
                last_log = now
        if bar is not None:
            bar.close()
    logger.info(
        "Чистка txt: %d файлов, %d почищено (%.0f с)",
        len(txts), changed, time.perf_counter() - t0,
    )
    return changed, len(txts)


def normalize_all_txts(txts_root: Path, threads: int = 32) -> tuple[int, int, int, int]:
    """Полная нормализация каталога: (почищено, склеено, переименовано, всего).

    Порядок важен: чистка -> безусловная склейка всех частей -> переименование
    по теме (тема берётся из полного текста, а не из отдельной части).
    """
    changed, total = clean_all_txts(txts_root, threads)
    merged, skipped = merge_split_documents(txts_root, threads)
    logger.info(
        "Нормализация txt: %d групп склеено, %d не склеено из-за ошибок",
        merged, skipped,
    )
    renamed = rename_by_theme(txts_root, threads)
    return changed, merged, renamed, total
