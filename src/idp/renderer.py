"""Render PDF pages to PNG images without reading the text layer."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import fitz
from PIL import Image, ImageEnhance

from idp.config import settings
from idp.image_preprocess import preprocess_for_ocr

# MuPDF печатает некритичные syntax-ошибки битых PDF (кривые XObject, битые
# словари, non-page object in page tree — типично для сканов) прямо в stderr:
# сотни строк спама в логах воркера, хотя текст извлекается нормально.
# Глушим поток; предупреждения при необходимости читаются через
# fitz.TOOLS.mupdf_warnings(), а фатальные проблемы ловятся исключениями.
fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)

logger = logging.getLogger(__name__)

_local = threading.local()

# Qwen3-VL processor отклоняет изображения с вырожденной стороной (400 Bad
# Request «Failed to apply Qwen3VLProcessor»).  В корпусе встречаются
# страницы-«полосы» (медиабокс в единицах пунктов) — при 300 dpi они дают
# картинки вида 4x1250 px и убивают весь пакет запросов на всех репликах.
# Гарантируем минимальную сторону после рендера.
_MIN_PX = 64
# Ограничения Qwen3VLProcessor (vllm) не задокументированы и меняются между
# версиями: падал и на 4x1250, и на 125x313.  Угадывать точный порог
# бессмысленно — «подозрительные» картинки кладём на белый квадратный канвас
# со стороной, кратной 64: aspect ровно 1, стороны чётные, min >= 64.
# Обычные страницы книг критериям проходят и возвращаются без изменений.
_VLM_SAFE_MIN_SIDE = 256
_VLM_SAFE_ASPECT = 6.0
_VLM_SAFE_CANVAS_LONG = 2048


def ensure_vlm_safe_image(img: Image.Image, force: bool = False) -> Image.Image:
    """Картинка, гарантированно проходящая Qwen3VLProcessor.

    Обычные страницы (чётные стороны, min >= 256, aspect <= 6) возвращаются
    без изменений.  Нечётные стороны у крупных картинок — мягкий ресайз до
    чётных.  Мелкие/вытянутые (полосы, мини-страницы) — белый квадратный
    канвас со стороной, кратной 64.  ``force=True`` — канвас для любой
    картинки (деградационный retry на уровне VLM-клиента).
    """
    w, h = img.size
    odd = (w % 2) or (h % 2)
    small = min(w, h) < _VLM_SAFE_MIN_SIDE
    wild = not (1.0 / _VLM_SAFE_ASPECT <= w / h <= _VLM_SAFE_ASPECT)
    if not force and not (odd or small or wild):
        return img
    if not force and not (small or wild):
        # только нечётные стороны — ресайз до чётных, канвас не нужен
        return img.resize((w + (w % 2), h + (h % 2)), Image.Resampling.LANCZOS)
    scale = min(1.0, _VLM_SAFE_CANVAS_LONG / max(w, h))
    nw = max(64, round(w * scale))
    nh = max(64, round(h * scale))
    nw += nw % 2
    nh += nh % 2
    side = ((max(nw, nh) + 63) // 64) * 64
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    if (nw, nh) != (w, h):
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas.paste(img, ((side - nw) // 2, (side - nh) // 2))
    logger.debug("Renderer: letterbox %dx%d -> %dx%d (безопасный канвас VLM)", w, h, side, side)
    return canvas


def open_pdf(pdf_path: Path):
    """fitz.open с фолбэком на pikepdf-ремонт битых файлов.

    MuPDF строго валидирует PDF и отказывается открывать повреждённые
    xref/трейлер/потоки — то, что читалки чинят молча.  Починенную копию
    скармливаем fitz из памяти, временный файл удаляем сразу.
    """
    try:
        return fitz.open(str(pdf_path))
    except Exception:
        from idp.pdf_repair import repair_pdf  # noqa: PLC0415

        repaired = repair_pdf(pdf_path)
        if repaired is None:
            raise
        try:
            data = repaired.read_bytes()
        finally:
            repaired.unlink(missing_ok=True)
        logger.warning("PDF открыт после pikepdf-ремонта: %s", pdf_path.name)
        return fitz.open(stream=data, filetype="pdf")


def _open_document(pdf_path: Path):
    """One PyMuPDF document per worker thread (the API is not thread-safe)."""
    doc = getattr(_local, "doc", None)
    if doc is None or getattr(_local, "doc_path", None) != str(pdf_path):
        if doc is not None:
            doc.close()
        doc = open_pdf(pdf_path)
        _local.doc = doc
        _local.doc_path = str(pdf_path)
    return doc


def _close_thread_document() -> None:
    doc = getattr(_local, "doc", None)
    if doc is not None:
        doc.close()
    _local.doc = None
    _local.doc_path = None


def _render_single_page(
    page_index: int,
    pdf_path: Path,
    output_dir: Path,
    dpi: int,
    upscale_factor: int,
    max_image_dimension: int,
) -> tuple[Path, int]:
    doc = _open_document(pdf_path)
    page = doc.load_page(page_index)
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
    if min(pix.width, pix.height) < _MIN_PX:
        boost = _MIN_PX / min(pix.width, pix.height)
        mat = fitz.Matrix(zoom * boost, zoom * boost)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        logger.debug(
            "Renderer: page %d boost x%.1f (вырожденная страница) -> %dx%d",
            page_index + 1, boost, pix.width, pix.height,
        )
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    logger.debug("Renderer: page %d raw size: %dx%d", page_index + 1, pix.width, pix.height)
    img = _upscale_image(img, scale=upscale_factor)
    img = _resize_to_max_dimension(img, max_image_dimension)
    # Квадратный канвас для вырожденных страниц — иначе процессор VLM
    # (Qwen3VLProcessor) отклонит запрос целиком, 400 на всех репликах.
    img = ensure_vlm_safe_image(img)
    if settings.preprocess_enabled:
        img = preprocess_for_ocr(
            img,
            steps=settings.preprocess_steps,
            upscale_factor=1,
            binarize_enabled=settings.preprocess_binarize,
        )
        logger.debug(
            "Renderer: page %d preprocessed size: %dx%d",
            page_index + 1,
            img.width,
            img.height,
        )
    logger.debug("Renderer: page %d final size: %dx%d", page_index + 1, img.width, img.height)
    out_path = output_dir / f"page_{page_index + 1:05d}.png"
    img.save(out_path, "PNG")
    return out_path, page_index


def _render_all_pages(pdf_path: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = open_pdf(pdf_path)
    num_pages = len(doc)
    doc.close()
    if num_pages == 0:
        # Битый page tree: fitz открывает документ, но в нём 0 страниц.
        # Раньше падало тут же с непонятным «max_workers must be greater than 0».
        raise ValueError("пустой документ: 0 страниц (битый page tree)")
    max_workers = max(1, min(os.cpu_count() or 4, num_pages, 8))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _render_single_page,
                i, pdf_path, output_dir,
                settings.render_dpi,
                settings.upscale_factor,
                settings.max_image_dimension,
            )
            for i in range(num_pages)
        ]
        done_pages = 0
        t_render = time.perf_counter()
        results = []
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            done_pages += 1
            # Документы с тысячами страниц рендерятся долго — без этого
            # «зависание» неотличимо от обычной работы в логах.
            if num_pages > 200 and (done_pages % 200 == 0 or done_pages == num_pages):
                logger.info(
                    "Рендеринг %s: %d/%d страниц (%.0f с)",
                    pdf_path.name, done_pages, num_pages, time.perf_counter() - t_render,
                )
    results.sort(key=lambda x: x[1])
    return [path for path, _ in results]


def render_pdf_to_pngs(pdf_path: Path, output_dir: Path) -> list[Path]:
    pngs = _render_all_pages(pdf_path, output_dir)
    logger.debug("Renderer: produced %d images", len(pngs))
    return pngs


def convert_image_to_png(source: Path, target: Path) -> Path:
    """Write an image as PNG (converted when it is not one already).

    The VLM is fed PNG data, so a JPEG scan has to be converted first. When
    image preprocessing is enabled (poor-quality scans), the page is
    denoised, shadow-flattened, contrast-boosted and deskewed before saving.
    The written path is returned - it differs from ``target`` when the
    conversion fails and the file is copied as it is.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            img = image.convert("RGB")
            if settings.preprocess_enabled:
                img = preprocess_for_ocr(
                    img,
                    steps=settings.preprocess_steps,
                    upscale_factor=settings.preprocess_upscale,
                    binarize_enabled=settings.preprocess_binarize,
                )
                logger.debug(
                    "Renderer: preprocessed scan %s -> %dx%d",
                    source.name,
                    img.width,
                    img.height,
                )
            img = ensure_vlm_safe_image(img)
            img.save(target, "PNG")
        return target
    except Exception as exc:  # noqa: BLE001 - keep the original file
        logger.warning("Cannot convert %s to PNG: %s", source, exc)
        fallback = target.with_suffix(source.suffix.lower() or ".img")
        shutil.copyfile(source, fallback)
        return fallback


def _upscale_image(img: Image.Image, scale: int = 2) -> Image.Image:
    if scale <= 1:
        return img
    new_size = (img.width * scale, img.height * scale)
    upscaled = img.resize(new_size, Image.Resampling.LANCZOS)
    enhancer = ImageEnhance.Sharpness(upscaled)
    upscaled = enhancer.enhance(1.15)
    enhancer = ImageEnhance.Contrast(upscaled)
    upscaled = enhancer.enhance(1.05)
    return upscaled


def _resize_to_max_dimension(img: Image.Image, max_dim: int) -> Image.Image:
    current_max = max(img.width, img.height)
    if current_max <= max_dim:
        return img
    scale = max_dim / current_max
    new_size = (int(img.width * scale), int(img.height * scale))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def extract_pdf_text_and_visual_pages(
    pdf_path: Path,
    output_dir: Path | None = None,
) -> tuple[str, list[Path], list[int]]:
    """Render the PDF to page PNGs. The text layer is intentionally never read.

    When ``output_dir`` is given, renders live there (the pipeline's
    ``rendered/`` area); otherwise a per-stem temp directory is used and the
    caller is responsible for cleanup.
    """
    doc = open_pdf(pdf_path)
    num_pages = len(doc)
    doc.close()

    if output_dir is not None:
        target_dir = output_dir
    else:
        target_dir = Path(tempfile.gettempdir()) / "idp_pdf_pages" / pdf_path.stem

    pngs = _render_all_pages(pdf_path, target_dir)

    visual_page_indices = list(range(num_pages))
    logger.debug("Renderer: produced %d images", len(pngs))
    return "", pngs, visual_page_indices


def extract_pdf_text(pdf_path: Path) -> tuple[str, int]:
    """Extract plain text from all pages of a PDF using the embedded text layer.

    Returns ``(text, page_count)``.  Pages are separated by a form-feed
    character so callers can split by page when needed.
    """
    try:
        doc = open_pdf(pdf_path)
    except Exception:
        return "", 0
    parts: list[str] = []
    try:
        for page in doc:
            parts.append(page.get_text())
    finally:
        doc.close()
    text = "\f".join(parts)
    return text, len(parts)
