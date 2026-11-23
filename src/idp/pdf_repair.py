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
from pathlib import Path

logger = logging.getLogger(__name__)


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
    logger.warning("repair: %s починен через pikepdf (%d байт -> %d байт)",
                   path.name, size, Path(tmp_name).stat().st_size)
    return Path(tmp_name)
