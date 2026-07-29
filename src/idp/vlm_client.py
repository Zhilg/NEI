"""Client for local vLLM VL model."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from idp.config import settings


def _encode_image(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def _chunked(images: list[Path], size: int) -> list[list[Path]]:
    return [images[i : i + size] for i in range(0, len(images), size)]


SYSTEM_PROMPT = (
    "You are a document reconstruction engine. "
    "Ignore the embedded text layer of PDFs. "
    "Recognize content purely from the visual representation of pages. "
    "Convert tables to correct Markdown. "
    "Describe images by their meaning and insert the description exactly where the image appears in the original document. "
    "Preserve reading order and page structure."
)


def _build_user_content(images: list[Path]) -> list[dict]:
    return [
        {
            "type": "text",
            "text": "Reconstruct the document page(s) shown in the following image(s). Output only Markdown.",
        },
        *(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{_encode_image(img)}"},
            }
            for img in images
        ),
    ]


async def reconstruct_markdown_from_images(images: list[Path]) -> str:
    if not images:
        return ""
    chunks = _chunked(images, settings.max_images_per_request)
    parts: list[str] = []
    async with httpx.AsyncClient(timeout=settings.vl_timeout_seconds) as client:
        for chunk in chunks:
            payload = {
                "model": settings.vl_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_content(chunk)},
                ],
                "temperature": 0.1,
            }
            response = await client.post(
                f"{settings.vl_endpoint}/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            parts.append(content)
    return "\n".join(parts)
