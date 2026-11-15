"""Image preprocessing for poor-quality document scans.

Pipeline is VLM-only: page renders go straight to the VL model, so the input
image quality dominates OCR/table accuracy. Xiaomi camera scans typically
suffer from low resolution, rotation/skew, sensor noise, uneven illumination
(yellowish paper, shadows) and low local contrast.

All operations are implemented on top of PIL + numpy so the air-gapped build
does not require OpenCV. OpenCV is used opportunistically when available
(faster denoise / adaptive threshold).
"""

from __future__ import annotations

import logging
import math
from typing import Iterable, Optional

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:  # pragma: no cover - optional OpenCV acceleration
    import cv2  # type: ignore[import-not-found]

    _HAS_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    _HAS_CV2 = False

logger = logging.getLogger(__name__)

_STEP_NAMES = {
    "upscale",
    "denoise",
    "shadows",
    "clahe",
    "deskew",
    "binarize",
}


def _to_gray_arr(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.uint8)


def _to_rgb(arr: np.ndarray) -> Image.Image:
    if arr.ndim == 2:
        return Image.fromarray(arr, mode="L")
    return Image.fromarray(arr, mode="RGB")


def upscale(img: Image.Image, factor: int = 2) -> Image.Image:
    if factor <= 1:
        return img
    new_size = (img.width * factor, img.height * factor)
    return img.resize(new_size, Image.Resampling.LANCZOS)


def denoise(img: Image.Image) -> Image.Image:
    if _HAS_CV2:
        arr = np.asarray(img.convert("RGB"))
        denoised = cv2.fastNlMeansDenoisingColored(arr, None, 10, 10, 7, 21)
        return Image.fromarray(denoised, mode="RGB")
    # PIL fallback: median removes salt/pepper noise, light gaussian smooths
    return img.filter(ImageFilter.MedianFilter(size=3)).filter(
        ImageFilter.GaussianBlur(radius=0.6)
    )


def remove_shadows(img: Image.Image) -> Image.Image:
    """Flatten uneven illumination / yellowish paper background.

    Estimate the background with a large morphological opening (min then max
    filter via PIL), subtract it from the original, and renormalize so the
    background becomes near-white while text stays dark.
    """
    gray = img.convert("L")
    radius = max(15, min(gray.width, gray.height) // 40)
    background = gray.filter(ImageFilter.MinFilter(size=radius * 2 + 1)).filter(
        ImageFilter.MaxFilter(size=radius * 2 + 1)
    )
    bg_arr = np.asarray(background, dtype=np.float32) + 1.0
    src_arr = np.asarray(gray, dtype=np.float32)
    flat = np.clip((src_arr / bg_arr) * 255.0, 0, 255).astype(np.uint8)
    result = Image.fromarray(flat, mode="L")
    if img.mode != "L":
        result = Image.merge("RGB", (result, result, result))
    return result


def clahe(img: Image.Image, clip_limit: float = 2.0, tiles: int = 8) -> Image.Image:
    """Contrast Limited Adaptive Histogram Equalization on the L channel."""
    if _HAS_CV2:
        arr = np.asarray(img.convert("RGB"))
        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        clahe_obj = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(tiles, tiles))
        l_eq = clahe_obj.apply(l)
        lab_eq = cv2.merge((l_eq, a, b))
        rgb = cv2.cvtColor(lab_eq, cv2.COLOR_LAB2RGB)
        return Image.fromarray(rgb, mode="RGB")
    # PIL fallback: per-tile histogram equalization approximation via global
    # equalize plus local contrast stretch is insufficient; do global equalize
    # on L channel and merge back.
    lab = img.convert("LAB")
    l, a, b = lab.split()
    l_eq = ImageOps.equalize(l)
    return Image.merge("LAB", (l_eq, a, b)).convert("RGB")


def deskew(img: Image.Image, max_angle: float = 10.0, step: float = 0.5) -> Image.Image:
    """Straighten a rotated page using a projection-profile score.

    Binarizes a grayscale copy, rotates over [-max_angle, max_angle] *without*
    expanding the canvas (so extreme angles crop content and score lower), and
    picks the angle with the sharpest vertical text-line projection (largest
    variance between row sums). Rotates the original image with white fill.
    """
    gray = _to_gray_arr(img)
    # Coarse binarization for projection scoring.
    _, binary = _threshold_otsu(gray)
    binary_pil = Image.fromarray(binary, mode="L")

    best_angle = 0.0
    best_score = -1.0
    angles = np.arange(-max_angle, max_angle + step, step)
    for angle in angles:
        if abs(angle) < 1e-6:
            rotated_arr = binary
        else:
            # rotate without expand keeps canvas size; extreme angles crop
            # content which naturally lowers the projection score.
            rotated_pil = binary_pil.rotate(
                angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=0
            )
            rotated_arr = np.asarray(rotated_pil)
        row_sums = rotated_arr.sum(axis=1)
        score = float(row_sums.var())
        if score > best_score:
            best_score = score
            best_angle = float(angle)

    if abs(best_angle) < 1e-6:
        return img
    logger.debug("Deskew: angle=%.2f", best_angle)
    return _rotate_image(img, best_angle, fill=255, resample=Image.Resampling.BICUBIC)


def binarize(img: Image.Image) -> Image.Image:
    """Adaptive Gaussian threshold for clean black-on-white text.

    Grayscale output: text near 0, background near 255. Not applied by default
    because thin table dividers can collapse into 1px artifacts.
    """
    gray = _to_gray_arr(img)
    if _HAS_CV2:
        th = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 11
        )
        return Image.fromarray(th, mode="L")
    # PIL fallback: simple Otsu-like global threshold via mean of local stats
    # is expensive; use a fixed mid threshold as acceptable fallback.
    threshold = int(gray.mean())
    arr = np.where(gray > threshold, 255, 0).astype(np.uint8)
    return Image.fromarray(arr, mode="L")


def _rotate_image(
    img: Image.Image, angle: float, fill: int = 255, resample=Image.Resampling.BICUBIC
) -> Image.Image:
    return img.rotate(angle, resample=resample, expand=True, fillcolor=fill)


def _threshold_otsu(arr: np.ndarray) -> tuple[float, np.ndarray]:
    """Otsu threshold on a grayscale numpy array."""
    hist, _ = np.histogram(arr.ravel(), bins=256, range=(0, 256))
    total = arr.size
    sum_total = int(np.dot(np.arange(256), hist))
    sum_b = 0
    w_b = 0
    best_thr = 0
    best_var = 0.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        mean_b = sum_b / w_b
        mean_f = (sum_total - sum_b) / w_f
        var_between = w_b * w_f * (mean_b - mean_f) ** 2
        if var_between > best_var:
            best_var = var_between
            best_thr = t
    binary = np.where(arr > best_thr, 255, 0).astype(np.uint8)
    return float(best_thr), binary


_PRESET_XIAOMI_STEPS = ["upscale", "denoise", "shadows", "clahe", "deskew"]


def _parse_steps(steps: str) -> list[str]:
    if isinstance(steps, str):
        raw = steps
    else:
        raw = ",".join(steps)
    parsed = [s.strip().lower() for s in raw.split(",") if s.strip()]
    invalid = [s for s in parsed if s not in _STEP_NAMES]
    if invalid:
        raise ValueError(f"Unknown preprocess steps: {invalid}. Allowed: {_STEP_NAMES}")
    # Deterministic safe order regardless of user-provided order.
    order = ["upscale", "denoise", "shadows", "clahe", "deskew", "binarize"]
    return [s for s in order if s in parsed]


def preprocess_for_ocr(
    img: Image.Image,
    steps: Iterable[str] = (),
    upscale_factor: int = 2,
    binarize_enabled: bool = False,
) -> Image.Image:
    """Run a configurable preprocessing chain on a page/scan image.

    Default order: upscale → denoise → shadows → clahe → deskew → (optional)
    binarize. Binarize is off by default: thin table dividers survive better
    in grayscale for the VLM to recognize table structure.
    """
    result = img
    step_list = list(_parse_steps(steps)) if steps else []
    if not step_list:
        step_list = ["upscale", "denoise", "shadows", "clahe", "deskew"]

    if "upscale" in step_list:
        result = upscale(result, factor=upscale_factor)
    if "denoise" in step_list:
        result = denoise(result)
    if "shadows" in step_list:
        result = remove_shadows(result)
    if "clahe" in step_list:
        result = clahe(result)
    if "deskew" in step_list:
        result = deskew(result)
    if binarize_enabled or "binarize" in step_list:
        result = binarize(result)
        # VLM expects RGB; keep single-channel as grayscale is fine.
    return result


def preset_xiaomi_scan(img: Image.Image) -> Image.Image:
    """Recommended preset for low-res rotated Xiaomi camera scans.

    Upscales 2x, denoises, flattens shadows, boosts local contrast, deskews.
    No binarization so table grid lines stay intact for the VLM.
    """
    return preprocess_for_ocr(
        img,
        steps=_PRESET_XIAOMI_STEPS,
        upscale_factor=2,
        binarize_enabled=False,
    )
