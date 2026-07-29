"""Client for local vLLM LLM entity extraction."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from idp.config import settings


ENTITY_SYSTEM_PROMPT = (
    "Extract entities from the provided Markdown document. "
    "Return a JSON array of entities with fields: type, value, page, evidence, confidence. "
    "page is 1-based page number if available, otherwise 0. "
    "evidence is the exact text snippet from the document. "
    "confidence is a float between 0 and 1. "
    "Supported types: person, organization, date, address, identifier, amount, other. "
    "Output ONLY valid JSON array, no explanations."
)


async def extract_entities(markdown_path: Path) -> list[dict]:
    text = markdown_path.read_text(encoding="utf-8")
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": ENTITY_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(
            f"{settings.llm_endpoint}/chat/completions",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            entities = parsed.get("entities", parsed.get("result", []))
        else:
            entities = parsed
        if not isinstance(entities, list):
            return []
        return [e for e in entities if isinstance(e, dict)]
    except json.JSONDecodeError:
        return []
