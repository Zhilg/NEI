"""Восстановление битых PDF через pikepdf (движок qpdf).

Тот самый ремонт, который молча делают Acrobat/Foxit при открытии:
восстановление xref-таблицы и трейлера, перезапись битых потоков,
игнорирование неконсистентных объектов.  MuPDF валидирует строго и отказывается
открывать такие файлы — чиним копию и работаем с ней.

Модуль намеренно не импортирует пакет idp: его подгружает напрямую
scripts/fix_results.py (через importlib), где зависимости idp недоступны.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Ремонт сериализован намеренно: qpdf восстанавливает xref, сканируя файл
# целиком — на многосотнемегабайтном битом PDF это минуты и гигабайты RAM.
# Параллельные ремонты в 64 потоках воркера укладывались в ~115 ГБ памяти
# и блокировали весь прогон.  Один ремонт за раз, остальные ждут.
_repair_lock = threading.Lock()


def repair_pdf(path: Path) -> Path | None:
    """Вернуть путь к починенной копии ``path`` или None, если не починилось.

    Копия лежит во временном файле — вызывающий код отвечает за очистку.
    Пустые файлы и отсутствующий pikepdf чинить нечем — сразу None.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None  # пустой файл чинить нечем
    try:
        import pikepdf  # noqa: PLC0415
    except ImportError:
        logger.info("repair: pikepdf не установлен, ремонт недоступен")
        return None
    with _repair_lock:
        logger.warning("repair: чиню %s (%d МБ), остальные ремонты ждут...", path.name, size // 2**20)
        t0 = time.perf_counter()
        try:
            pdf = pikepdf.open(path)
        except Exception as exc:  # noqa: BLE001 — битый есть битый, режем тихо
            logger.info("repair: pikepdf не открыл %s: %s", path.name, exc)
            return None
        try:
            fd, tmp_name = tempfile.mkstemp(suffix=".repaired.pdf", prefix="idp_repair_")
            import os

            os.close(fd)
            pdf.save(tmp_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("repair: не удалось сохранить починенную копию %s: %s", path.name, exc)
            return None
        finally:
            pdf.close()
    logger.warning(
        "repair: %s починен через pikepdf (%d байт -> %d байт, %.0f с)",
        path.name, size, Path(tmp_name).stat().st_size, time.perf_counter() - t0,
    )
    return Path(tmp_name)
