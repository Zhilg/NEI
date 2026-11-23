"""Ждать, пока воркер переработает все PDF с текстовым слоем.

Опрашивает results.jsonl раз в 2 минуты: все ли текстовослойные pdf из input
снова имеют запись со статусом ok/skip.  Пишет прогресс в лог, по таймауту
(24 ч) уходит с кодом 1.

Запуск (внутри контейнера, как и fix_results.py):

    python /workspace/scripts/wait_pdf_reprocess.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wait_pdf")

sys.path.insert(0, "/workspace/scripts")
sys.path.insert(0, "/workspace/src")

from fix_results import _pdf_has_text_layer  # noqa: E402
from idp.config import settings  # noqa: E402

POLL_SEC = 120
TIMEOUT_SEC = 24 * 3600


def main() -> int:
    input_root: Path = settings.input_root
    results_path: Path = settings.output_root / "results.jsonl"

    logger.info("Сканирую input: ищу pdf с текстовым слоем...")
    stems: set[str] = set()
    n_pdfs = 0
    for p in sorted(input_root.rglob("*.pdf")):
        if not p.is_file():
            continue
        n_pdfs += 1
        if _pdf_has_text_layer(p):
            stems.add(p.stem)
    logger.info("Всего pdf: %d, с текстовым слоем (ждём переработки): %d", n_pdfs, len(stems))
    if not stems:
        logger.info("Перерабатывать нечего.")
        return 0

    deadline = time.time() + TIMEOUT_SEC
    offset = 0  # журнал дополняется только append'ом — дочитываем хвост,
    # а не перечитываем 5 ГБ каждые 2 минуты (сжигало целое ядро CPU).
    while True:
        done: set[str] = set()
        if results_path.exists():
            size = results_path.stat().st_size
            if size < offset:
                offset = 0  # журнал пересоздали (pdf-requeue) — читаем с нуля
            with open(results_path, encoding="utf-8") as f:
                f.seek(offset)
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    fpath = rec.get("file", "")
                    if (
                        rec.get("status") in ("ok", "skip")
                        and isinstance(fpath, str)
                        and fpath.lower().endswith(".pdf")
                    ):
                        done.add(Path(fpath).stem)
                offset = f.tell()
        missing = stems - done
        logger.info(
            "Переработано %d/%d текстовослойных pdf, осталось: %d",
            len(stems) - len(missing), len(stems), len(missing),
        )
        if not missing:
            logger.info("Все текстовослойные pdf переработаны.")
            return 0
        if time.time() > deadline:
            logger.error("Таймаут 24 ч. Не дождались: %d файлов", len(missing))
            return 1
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
