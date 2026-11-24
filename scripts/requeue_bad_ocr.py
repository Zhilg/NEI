"""Найти явно битый OCR и вернуть его источники воркеру на переработку.

Удаляются только TXT с очевидной абракадаброй, для которых найдена запись в
results.jsonl и живой исходник в input. Сначала атомарно удаляются старые записи
журнала, затем негодные TXT. Несопоставленные файлы остаются на месте.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("requeue_bad_ocr")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_reprocess import _QUICK_FILE_RE, _filter_journal, _scan_txt_owners  # noqa: E402

_WORD_RE = re.compile(r"[^\W\d_]+")
_COMMON_WORDS = frozenset(
    "не что он на как то все она так его но да ты же вы за бы по только было "
    "вот от меня еще нет из ему теперь когда даже если уже или быть был него "
    "до вас нибудь опять вам ведь там потом себя ничего может они тут где есть "
    "надо для мы тебя их чем была сам чтоб без будто чего раз тоже себе под "
    "будет тогда кто этот того потому этого какой совсем здесь этом один почти "
    "мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец "
    "два об другой хоть после над больше тот через эти нас про всего них какая "
    "много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть "
    "том нельзя такой более всегда конечно всю между также".split()
)
_ORDINARY_PUNCT = frozenset(".,:;!?…—–-()[]«»\"'№_%/\\+=*")
_SAMPLE_BYTES = 128 * 1024


def is_obviously_bad_ocr(text: str) -> bool:
    """Консервативный детектор: таблицы и короткие документы не трогаем.

    Тяжёлый мусор в присланном образце отличается одновременно огромной долей
    одиночных «слов», очень маленькой средней длиной слова и символьным мусором.
    Одного признака недостаточно, чтобы отправить документ на переработку.
    """
    if len(text) < 4000:
        return False
    words = _WORD_RE.findall(text.casefold())
    if len(words) < 300:
        return False
    single_ratio = sum(len(word) == 1 for word in words) / len(words)
    average_length = sum(map(len, words)) / len(words)
    if single_ratio < 0.25 or average_length > 4.5:
        return False
    long_words = [word for word in words if len(word) >= 2]
    common_ratio = sum(word in _COMMON_WORDS for word in long_words) / max(1, len(long_words))
    weird_ratio = sum(
        not (char.isalnum() or char.isspace() or char in _ORDINARY_PUNCT)
        for char in text
    ) / len(text)
    return weird_ratio >= 0.015 or common_ratio <= 0.06


def _check_txt(path: Path) -> tuple[Path, bool]:
    """Проверить начало, середину и конец TXT, не читая гигабайтный файл целиком."""
    try:
        size = path.stat().st_size
        offsets = sorted({0, max(0, size // 2 - _SAMPLE_BYTES // 2), max(0, size - _SAMPLE_BYTES)})
        chunks: list[bytes] = []
        with path.open("rb") as f:
            for offset in offsets:
                f.seek(offset)
                chunks.append(f.read(_SAMPLE_BYTES))
        return path, is_obviously_bad_ocr(b"\n".join(chunks).decode("utf-8", errors="replace"))
    except OSError as exc:
        logger.warning("OCR-проверка: пропущен %s: %s", path.name, exc)
        return path, False


def _load_name_mapping(txts_root: Path) -> dict[str, str]:
    path = txts_root / "_names_map.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("Карта имён не читается: %s", exc)
        return {}


def requeue_bad_ocr(
    input_root: Path, output_root: Path, threads: int = 32, dry_run: bool = False,
) -> tuple[int, int, int]:
    """Вернуть (поставлено на переработку, несопоставлено, нет исходника)."""
    results_path = output_root / "results.jsonl"
    txts_root = output_root / "txts"
    txts = [path for path in txts_root.glob("*.txt") if path.is_file()]
    logger.info("OCR-проверка: %d TXT", len(txts))
    bad: list[Path] = []
    started = last_log = time.perf_counter()
    try:
        from tqdm import tqdm  # noqa: PLC0415

        bar = tqdm(total=len(txts), desc="проверка OCR", unit="файл")
    except ImportError:
        bar = None
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        futures = [pool.submit(_check_txt, path) for path in txts]
        for done, future in enumerate(as_completed(futures), 1):
            path, is_bad = future.result()
            if is_bad:
                bad.append(path)
            if bar is not None:
                bar.update(1)
            now = time.perf_counter()
            if now - last_log >= 30:
                eta = (len(txts) - done) * (now - started) / done
                logger.info(
                    "OCR-проверка: %d/%d, битых %d, прошло %.0f с, ETA %.0f с",
                    done, len(txts), len(bad), now - started, eta,
                )
                last_log = now
    if bar is not None:
        bar.close()
    logger.info("OCR-проверка: явно битых TXT %d", len(bad))
    if not bad:
        return 0, 0, 0

    owners = _scan_txt_owners(results_path)
    rels_by_stem: dict[str, set[str]] = defaultdict(set)
    for rel, stems in owners.items():
        for stem in {*stems, Path(rel).stem}:
            rels_by_stem[stem.casefold()].add(rel)
    mapping = _load_name_mapping(txts_root)
    original_by_new = {value.casefold(): key for key, value in mapping.items()}

    queued: list[tuple[Path, set[str]]] = []
    unmatched = missing_source = 0
    for path in bad:
        rels = set(rels_by_stem.get(path.stem.casefold(), ()))
        original = original_by_new.get(path.stem.casefold())
        if original is not None:
            rels.update(rels_by_stem.get(original.casefold(), ()))
        live_rels = {rel for rel in rels if (input_root / rel).is_file()}
        if not rels:
            unmatched += 1
            logger.warning("Битый OCR без записи в журнале, НЕ удаляю: %s", path.name)
            continue
        if not live_rels:
            missing_source += 1
            logger.warning("Битый OCR без живого исходника, НЕ удаляю: %s", path.name)
            continue
        queued.append((path, live_rels))
        logger.info("На переработку: %s <- %s", path.name, ", ".join(sorted(live_rels)))

    if dry_run:
        logger.info("Пробный режим: журнал и TXT не изменены")
        return len(queued), unmatched, missing_source
    if not queued:
        return 0, unmatched, missing_source

    drop_rels = set().union(*(rels for _, rels in queued))

    def classify(line: bytes) -> bytes | None:
        match = _QUICK_FILE_RE.search(line)
        if match is not None:
            rel = match.group(1).decode("utf-8", errors="replace")
            if rel in drop_rels:
                return None
        return line if line.endswith(b"\n") else line + b"\n"

    tmp_path = results_path.with_suffix(".jsonl.requeue.tmp")
    try:
        with tmp_path.open("wb") as out:
            kept, dropped = _filter_journal(
                results_path, set(), out, threads=threads, classifier=classify,
            )
            out.flush()
            os.fsync(out.fileno())
        if dropped == 0:
            raise RuntimeError("журнал не изменился: TXT оставлены на месте")
        os.replace(tmp_path, results_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info("Журнал обновлён: удалено %d записей, оставлено %d", dropped, kept)

    deleted = 0
    for path, _ in queued:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            logger.error("Не удалился негодный TXT %s: %s", path.name, exc)
    logger.info(
        "Готово: удалено %d TXT, источники будут переработаны воркером; "
        "несопоставлено %d, без исходника %d",
        deleted, unmatched, missing_source,
    )
    return deleted, unmatched, missing_source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--threads", type=int, default=int(os.environ.get("IDP_WORKER_THREADS", "32")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    input_root, output_root = args.input, args.output
    if input_root is None or output_root is None:
        sys.path.insert(0, "/workspace/src")
        from idp.config import settings  # noqa: PLC0415

        input_root = input_root or settings.input_root
        output_root = output_root or settings.output_root
    requeue_bad_ocr(input_root, output_root, args.threads, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
