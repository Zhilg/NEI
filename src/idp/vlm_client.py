"""Client for local vLLM VL model."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
from functools import lru_cache
from pathlib import Path

import httpx

from idp.config import settings

_ENTITY_SCHEMA_PATH = Path(__file__).parent / "entity_schema.json"

_VL_ENDPOINTS: list[str] = []
_VL_ENDPOINT_INDEX = 0
_VL_ENDPOINT_LOCK: asyncio.Lock | None = None


class RoundRobinEndpointSelector:
    def __init__(self, endpoints: list[str]) -> None:
        self._endpoints = endpoints
        self._index = 0

    def next(self) -> str:
        if not self._endpoints:
            return settings.vl_endpoint
        endpoint = self._endpoints[self._index % len(self._endpoints)]
        self._index += 1
        return endpoint


def get_vl_endpoints() -> list[str]:
    if settings.vl_endpoints:
        return settings.vl_endpoints
    if settings.vl_endpoint:
        return [settings.vl_endpoint]
    return []


def get_endpoint_selector() -> RoundRobinEndpointSelector:
    return RoundRobinEndpointSelector(get_vl_endpoints())


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    *,
    selector: RoundRobinEndpointSelector | None = None,
) -> httpx.Response:
    for attempt in range(3):
        try:
            response = await client.post(url, json=payload)
        except (httpx.NetworkError, httpx.TimeoutException):
            if attempt < 2:
                if selector is not None:
                    url = f"{selector.next()}/chat/completions"
                await asyncio.sleep(1)
                continue
            raise
        if response.status_code == 429:
            await asyncio.sleep(1)
            if selector is not None:
                url = f"{selector.next()}/chat/completions"
            response = await client.post(url, json=payload)
        if response.status_code != 200:
            raise RuntimeError(
                f"VLM request failed: {response.status_code} {response.text}"
            )
        response.raise_for_status()
        return response
    raise RuntimeError("VLM request failed after retries")


def _load_entity_schema_raw() -> dict:
    try:
        with open(_ENTITY_SCHEMA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "version": "entity-v1",
            "entity_types": [
                {"name": "person", "description": "ФИО физического лица"},
                {"name": "organization", "description": "Название организации"},
                {"name": "date", "description": "Дата"},
                {"name": "address", "description": "Адрес"},
                {"name": "identifier", "description": "Идентификатор, номер документа"},
                {"name": "amount", "description": "Сумма, число с единицами"},
                {"name": "sender", "description": "Отправитель"},
                {"name": "recipient", "description": "Получатель, адресат"},
            ],
        }


@lru_cache(maxsize=1)
def _load_entity_schema() -> dict:
    return _load_entity_schema_raw()


def _get_entity_types() -> list[dict]:
    return _load_entity_schema().get("entity_types", [])


def _build_entity_type_descriptions() -> str:
    types = _get_entity_types()
    return "\n".join(f"- {t['name']}: {t['description']}" for t in types)


def update_entity_schema(new_types: list[dict]) -> None:
    schema = _load_entity_schema_raw()
    existing_names = {t["name"] for t in schema.get("entity_types", [])}
    for new_type in new_types:
        if new_type.get("name") and new_type["name"] not in existing_names:
            schema.setdefault("entity_types", []).append(new_type)
            existing_names.add(new_type["name"])
    with open(_ENTITY_SCHEMA_PATH, "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _load_entity_schema.cache_clear()


def _encode_image(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def _chunked(images: list[Path], size: int) -> list[list[Path]]:
    return [images[i : i + size] for i in range(0, len(images), size)]


def _repair_json(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    repaired = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            repaired.append(line)
            continue
        if repaired:
            prev = repaired[-1].rstrip()
            if prev and not prev.endswith((',', '{', '[', ':', '}', ']')):
                if re.match(r'^\s*"(?:\w|[-_])+"\s*:', stripped):
                    repaired[-1] = prev + ','
        repaired.append(line)
    return "\n".join(repaired)


def _truncate_to_balanced(text: str) -> str:
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_string = False
    escape = False
    best_end = len(text)
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch in "{[":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    best_end = i + 1
                    break
            elif ch == "]":
                depth -= 1
    return text[:best_end]


_ENTITY_RE = re.compile(
    r'\{\s*"type"\s*:\s*"([^"]+)"\s*,\s*"value"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)


def _extract_entities_regex(text: str) -> list[dict]:
    entities = []
    for match in _ENTITY_RE.finditer(text):
        entities.append({"type": match.group(1), "value": match.group(2)})
    return entities


_LONG_VALUE_TYPES = {
    "handwritten_text", "handwritten_note",
    "handwritten_amount", "payment_details",
}
_SHORT_VALUE_TYPES = {"stamp", "signature", "handwritten_signature"}
_JUNK_TYPES: set[str] = set()


def _validate_entity(entity: dict, source_text: str = "") -> dict | None:
    etype = str(entity.get("type", "other"))
    if etype in _JUNK_TYPES:
        return None
    value = str(entity.get("value", "")).strip()
    if not value:
        return None
    if "\n" in value:
        return None
    max_len = 500 if etype in _LONG_VALUE_TYPES else 150
    if len(value) > max_len:
        return None
    words = value.split()
    if value.endswith(".") and len(words) > 5 and etype not in _LONG_VALUE_TYPES:
        return None
    evidence = str(entity.get("evidence", "")).strip()
    if evidence and value == evidence and len(value) > 3 and etype not in _SHORT_VALUE_TYPES:
        return None
    if source_text and evidence and len(evidence) >= 3:
        normalized_source = " ".join(source_text.lower().split())
        normalized_evidence = " ".join(evidence.lower().split())
        if normalized_evidence not in normalized_source:
            pass
    return entity


_PAGE_MARKER_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")


def extract_paragraphs(markdown: str) -> list[dict]:
    result: list[dict] = []
    page = 1
    para_num = 0
    current_lines: list[str] = []

    for line in markdown.splitlines():
        stripped = line.strip()
        match = _PAGE_MARKER_RE.match(stripped)
        if match is not None:
            if current_lines:
                para_num += 1
                result.append({
                    "page": page,
                    "paragraph": para_num,
                    "text": "\n".join(current_lines).strip(),
                })
                current_lines = []
            page = int(match.group(1))
            para_num = 0
            continue
        if stripped:
            current_lines.append(stripped)
        else:
            if current_lines:
                para_num += 1
                result.append({
                    "page": page,
                    "paragraph": para_num,
                    "text": "\n".join(current_lines).strip(),
                })
                current_lines = []

    if current_lines:
        para_num += 1
        result.append({
            "page": page,
            "paragraph": para_num,
            "text": "\n".join(current_lines).strip(),
        })

    return result


SYSTEM_PROMPT_MD = (
    "Reconstruct the document page as clean Markdown.\n\n"
    "STRICTLY EXCLUDE the following noise elements:\n"
    "- Page numbers\n"
    "- Running headers and footers\n"
    "- Journal names, magazine titles, publication names at page bottom or top\n"
    "- Watermarks\n"
    "- Copyright notices\n"
    "- URLs and email addresses not part of the main content\n"
    "- Any decorative or boilerplate text outside the main content area\n\n"
    "Preserve the actual document content in reading order.\n"
    "Split text into paragraphs. Separate paragraphs with a blank line.\n"
    "Preserve tables as Markdown tables.\n"
    "When a table has many columns, read ALL columns — do not truncate to the first column.\n"
    "If a horizontal table is too wide to read in full, note: 'Table too wide, truncated at <column name>'.\n"
    "Describe every image, chart, diagram, or figure inline with [Image: detailed description of what is shown]. "
    "If a page contains no images, do not add any image placeholder.\n"
    "For each image, include what type of visual it is (photo, chart, diagram, screenshot, table, etc.) "
    "and describe its content in detail.\n\n"
    "HANDWRITING DETECTION:\n"
    "If any text is handwritten (cursive, ink, marker, different from printed text), wrap it in [HANDWRITTEN: ...]. "
    "Example: 'The price is [HANDWRITTEN: 5000] rubles.'\n\n"
    "Output ONLY the Markdown text. No explanations."
)

SYSTEM_PROMPT_MD_TEST = "Convert the document images to Markdown. Output only Markdown."

SYSTEM_PROMPT_ENT = (
    "Extract atomic metadata entities from the provided text.\n\n"
    "ENTITY DEFINITION: An entity is a discrete, structured fact with a short specific value. "
    "Valid examples: dates, amounts, phone numbers, INN, OGRN, names, addresses, document numbers, codes.\n\n"
    "PERSON NAMES (CRITICAL): Extract ALL person names (ФИО) regardless of context. "
    "This includes:\n"
    "- Names with titles/positions: 'И. И. Иванов, ведущий научный сотрудник'\n"
    "- Names without any title or context: just 'Сталин' or 'Иванов'\n"
    "- Names in lists: 'И. И. Иванов, Д. Д. Дятлов, В.В. Должанский'\n"
    "- Names in signatures: 'В. Лаптев'\n"
    "- Names in documents: 'И.В. Сталин'\n\n"
    "For person entities, extract the FULL NAME as it appears in the text. "
    "If initials are used (e.g., 'И.В. Сталин'), keep them as-is. "
    "If only last name appears (e.g., 'Сталин'), extract it as the value.\n\n"
    "FORBIDDEN (do NOT extract these):\n"
    "- Full sentences or clauses\n"
    "- Paragraphs or headings\n"
    "- Generic document type words without specific value (e.g., just 'приказ', 'договор', 'акт')\n"
    "- Boilerplate phrases like 'см. приложение', 'без изменений', 'ответственный'\n"
    "- Values longer than 150 characters\n"
    "- Values that are identical to the surrounding sentence\n\n"
    "For each entity provide:\n"
    "- type: one of the schema types below, or 'other' if none matches\n"
    "- value: exact short text from the document\n"
    "- normalized_value: normalized form if applicable, otherwise omit\n"
    "- page: 1-based page number\n"
    "- paragraph: 1-based paragraph number within the page\n"
    "- evidence: exact short snippet containing the entity\n"
    "- confidence: float 0.0-1.0\n"
    "- handwritten: true if handwritten, false otherwise\n\n"
    f"Schema:\n{_build_entity_type_descriptions()}\n\n"
    'Return ONLY valid JSON: {"entities": [...]}\n'
    "No explanations, no code fences."
)

SYSTEM_PROMPT_ENT_TEST = "Extract entities from the text. Return only JSON with an 'entities' array."

SYSTEM_PROMPT_COMBINED = (
    "You are a document analysis assistant. Process the document images and do BOTH tasks:\n\n"
    "1. Reconstruct the document page as clean Markdown.\n"
    "2. Extract all entities from the document.\n\n"
    "For task 1, follow these rules:\n"
    "- Exclude page numbers, headers, footers, watermarks, copyright notices, decorative text\n"
    "- Preserve tables as Markdown tables\n"
    "- Read ALL columns of every table — never truncate to only the first column\n"
    "- Describe every image, chart, diagram, or figure inline with [Image: detailed description]\n"
    "- Wrap handwritten text in [HANDWRITTEN: ...]\n"
    "- Output ONLY the Markdown text\n\n"
    "For task 2, extract ONLY atomic metadata entities.\n"
    "ENTITY DEFINITION: An entity is a discrete, structured fact with a short specific value. "
    "FORBIDDEN: full sentences, generic words like 'приказ' without number/date, boilerplate phrases, values > 150 chars.\n\n"
    f"Entity schema:\n{_build_entity_type_descriptions()}\n\n"
    "For each entity provide: type, value, normalized_value (if applicable), page, paragraph, evidence, confidence (0.0-1.0), handwritten (true/false).\n\n"
    "Return a single JSON object with TWO keys:\n"
    '{"markdown": "<reconstructed markdown>", "entities": [<entity objects>]}\n'
    "No explanations, no code fences, no extra text."
)

SYSTEM_PROMPT_COMBINED_TEST = (
    "Process the document images. Reconstruct as Markdown and extract entities. "
    "Return a single JSON object with 'markdown' and 'entities' keys. No extra text."
)

SYSTEM_PROMPT_ENT_VISUAL = (
    "Extract atomic metadata entities from this document page image.\n\n"
    "ENTITY DEFINITION: An entity is a discrete, structured fact with a short specific value. "
    "Valid examples: dates, amounts, phone numbers, INN, OGRN, names, addresses, document numbers, codes.\n\n"
    "PERSON NAMES (CRITICAL): Extract ALL person names (ФИО) regardless of context. "
    "This includes:\n"
    "- Names with titles/positions: 'И. И. Иванов, ведущий научный сотрудник'\n"
    "- Names without any title or context: just 'Сталин' or 'Иванов'\n"
    "- Names in lists: 'И. И. Иванов, Д. Д. Дятлов, В.В. Должанский'\n"
    "- Names in signatures: 'В. Лаптев'\n"
    "- Names in documents: 'И.В. Сталин'\n\n"
    "For person entities, extract the FULL NAME as it appears in the text. "
    "If initials are used (e.g., 'И.В. Сталин'), keep them as-is. "
    "If only last name appears (e.g., 'Сталин'), extract it as the value.\n\n"
    "FORBIDDEN (do NOT extract these):\n"
    "- Full sentences or clauses\n"
    "- Paragraphs or headings\n"
    "- Generic document type words without specific value (e.g., just 'приказ', 'договор', 'акт')\n"
    "- Boilerplate phrases like 'см. приложение', 'без изменений', 'ответственный'\n"
    "- Values longer than 150 characters\n"
    "- Values that are identical to the surrounding sentence\n\n"
    "For each entity provide:\n"
    "- type: one of the schema types below, or 'other' if none matches\n"
    "- value: exact short text from the document\n"
    "- normalized_value: normalized form if applicable, otherwise omit\n"
    "- page: 1-based page number\n"
    "- paragraph: 1-based paragraph number within the page\n"
    "- evidence: exact short snippet containing the entity\n"
    "- confidence: float 0.0-1.0\n"
    "- handwritten: true if handwritten, false otherwise\n\n"
    f"Schema:\n{_build_entity_type_descriptions()}\n\n"
    'Return ONLY valid JSON: {"entities": [...]}\n'
    "No explanations, no code fences."
)

SYSTEM_PROMPT_ENT_VISUAL_TEST = "Extract entities from this document page image. Return only JSON with an 'entities' array."


async def reconstruct_markdown(images: list[Path]) -> str:
    if not images:
        return ""
    sys_prompt = SYSTEM_PROMPT_MD_TEST if settings.test_mode else SYSTEM_PROMPT_MD
    chunks = _chunked(images, settings.vl_max_images)
    max_tokens = 256 if settings.test_mode else settings.vl_max_tokens
    selector = get_endpoint_selector()
    semaphore = asyncio.Semaphore(settings.vl_concurrency)

    async with httpx.AsyncClient(timeout=settings.vl_timeout_seconds) as client:
        async def _process_chunk(i: int, chunk: list[Path]) -> str:
            async with semaphore:
                user_content = [
                    {"type": "text", "text": "Reconstruct as Markdown."},
                ]
                for img in chunk:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_encode_image(img)}"},
                    })
                payload = {
                    "model": settings.vl_model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                }
                response = await _post_with_retry(client, f"{selector.next()}/chat/completions", payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return _strip_code_fences(content)

        tasks = [_process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        parts = await asyncio.gather(*tasks)

    marked = []
    for i, part in enumerate(parts):
        marked.append(f"<!-- page {i + 1} -->\n{part}")
    return "\n\n".join(marked)


async def extract_entities_from_text(
    text: str,
    endpoint: str | None = None,
    model: str | None = None,
) -> list[dict]:
    model = model or settings.vl_model
    sys_prompt = SYSTEM_PROMPT_ENT_TEST if settings.test_mode else SYSTEM_PROMPT_ENT
    max_tokens = 256 if settings.test_mode else settings.vl_max_tokens
    max_chars = 500 if settings.test_mode else 6000
    if len(text) > max_chars:
        chunks = _chunk_text(text, max_chars)
    else:
        chunks = [text]
    selector = RoundRobinEndpointSelector([endpoint] if endpoint else get_vl_endpoints())
    semaphore = asyncio.Semaphore(settings.vl_concurrency)

    async with httpx.AsyncClient(timeout=settings.vl_timeout_seconds) as client:
        async def _process_chunk(i: int, chunk: str) -> list[dict]:
            async with semaphore:
                prompt = (
                    "Text:\n"
                    f"{chunk}\n\n"
                    "Extract entities and return JSON only."
                )
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": max_tokens,
                }
                response = await _post_with_retry(client, f"{selector.next()}/chat/completions", payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = _parse_vlm_json_response(content, f"entities chunk {i + 1}")
                if not parsed:
                    return []
                raw_entities = parsed.get("entities", parsed.get("result", []))
                result: list[dict] = []
                if isinstance(raw_entities, list):
                    for entity in raw_entities:
                        if not isinstance(entity, dict):
                            continue
                        validated = _validate_entity(entity, chunk)
                        if validated is None:
                            continue
                        confidence = float(entity.get("confidence", 0.0))
                        if confidence < settings.min_entity_confidence:
                            continue
                        handwritten = entity.get("handwritten")
                        if handwritten is None:
                            evidence_str = str(entity.get("evidence", ""))
                            value_str = str(entity.get("value", ""))
                            handwritten = "[HANDWRITTEN:" in evidence_str or "[HANDWRITTEN:" in value_str
                        result.append({
                            "type": str(entity.get("type", "other")),
                            "value": str(entity.get("value", "")),
                            "normalized_value": entity.get("normalized_value"),
                            "page": int(entity.get("page", 0)),
                            "paragraph": int(entity.get("paragraph", 0)),
                            "evidence": str(entity.get("evidence", "")),
                            "confidence": float(entity.get("confidence", 0.0)),
                            "handwritten": bool(handwritten),
                        })
                return result

        tasks = [_process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        chunk_results = await asyncio.gather(*tasks)

    all_entities: list[dict] = []
    for entities in chunk_results:
        all_entities.extend(entities)
    return _deduplicate_entities(all_entities)


def _chunk_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        para_len = len(para)
        if para_len > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            words = para.split()
            sub_chunk: list[str] = []
            sub_len = 0
            for word in words:
                if sub_len + len(word) + 1 > max_chars and sub_chunk:
                    chunks.append(" ".join(sub_chunk))
                    sub_chunk = []
                    sub_len = 0
                sub_chunk.append(word)
                sub_len += len(word) + 1
            if sub_chunk:
                chunks.append(" ".join(sub_chunk))
            continue
        if current_len + para_len + 2 > max_chars and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += para_len + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _deduplicate_entities(entities: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result: list[dict] = []
    for entity in entities:
        key = (
            str(entity.get("type", "")),
            str(entity.get("value", "")).strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(entity)
    return result


def _strip_code_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) > 1:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _parse_vlm_json_response(content: str, context: str) -> dict:
    cleaned = _strip_code_fences(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    repaired = _repair_json(cleaned)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    truncated = _truncate_to_balanced(repaired)
    try:
        return json.loads(truncated)
    except json.JSONDecodeError:
        pass
    entities = _extract_entities_regex(cleaned)
    if entities:
        return {"entities": entities}
    _write_trash(context, content)
    print(f"WARNING: VLM {context} JSON parse error", file=sys.stderr)
    print(f"  Raw response: {content[:300]}", file=sys.stderr)
    return {}


def _write_trash(context: str, content: str) -> None:
    trash_path = settings.trash_path
    if not trash_path:
        return
    try:
        path = Path(trash_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": time.time(),
                "context": context,
                "raw": content,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def extract_markdown_and_entities(images: list[Path]) -> tuple[str, list[dict]]:
    if not images:
        return "", []
    sys_prompt = SYSTEM_PROMPT_COMBINED_TEST if settings.test_mode else SYSTEM_PROMPT_COMBINED
    chunks = _chunked(images, settings.vl_max_images)
    max_tokens = 256 if settings.test_mode else settings.vl_max_tokens
    selector = get_endpoint_selector()
    semaphore = asyncio.Semaphore(settings.vl_concurrency)

    async with httpx.AsyncClient(timeout=settings.vl_timeout_seconds) as client:
        async def _process_chunk(i: int, chunk: list[Path]) -> tuple[str, list[dict]]:
            async with semaphore:
                user_content = [
                    {"type": "text", "text": "Process the document page: reconstruct Markdown and extract entities."},
                ]
                for img in chunk:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_encode_image(img)}"},
                    })
                payload = {
                    "model": settings.vl_model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.0,
                    "max_tokens": max_tokens,
                }
                response = await _post_with_retry(client, f"{selector.next()}/chat/completions", payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = _parse_vlm_json_response(content, f"combined page {i + 1}")
                if not parsed:
                    return "", []
                md = parsed.get("markdown", "")
                raw_entities = parsed.get("entities", [])
                entities: list[dict] = []
                if isinstance(raw_entities, list):
                    for entity in raw_entities:
                        if not isinstance(entity, dict):
                            continue
                        validated = _validate_entity(entity, md)
                        if validated is None:
                            continue
                        confidence = float(entity.get("confidence", 0.0))
                        if confidence < settings.min_entity_confidence:
                            continue
                        handwritten = entity.get("handwritten")
                        if handwritten is None:
                            evidence_str = str(entity.get("evidence", ""))
                            value_str = str(entity.get("value", ""))
                            handwritten = "[HANDWRITTEN:" in evidence_str or "[HANDWRITTEN:" in value_str
                        entities.append({
                            "type": str(entity.get("type", "other")),
                            "value": str(entity.get("value", "")),
                            "normalized_value": entity.get("normalized_value"),
                            "page": int(entity.get("page", i + 1)),
                            "paragraph": int(entity.get("paragraph", 0)),
                            "confidence": float(entity.get("confidence", 0.0)),
                            "handwritten": bool(handwritten),
                        })
                return md, entities

        tasks = [_process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        chunk_results = await asyncio.gather(*tasks)

    markdown_parts: list[str] = []
    all_entities: list[dict] = []
    for i, (md, entities) in enumerate(chunk_results):
        if isinstance(md, str) and md.strip():
            markdown_parts.append(f"<!-- page {i + 1} -->\n{md}")
        all_entities.extend(entities)
    return "\n\n".join(markdown_parts), _deduplicate_entities(all_entities)


async def extract_entities_from_images(images: list[Path]) -> list[dict]:
    if not images:
        return []
    sys_prompt = SYSTEM_PROMPT_ENT_VISUAL_TEST if settings.test_mode else SYSTEM_PROMPT_ENT_VISUAL
    max_tokens = 256 if settings.test_mode else settings.vl_max_tokens
    chunks = _chunked(images, settings.vl_max_images)
    selector = get_endpoint_selector()
    semaphore = asyncio.Semaphore(settings.vl_concurrency)

    async with httpx.AsyncClient(timeout=settings.vl_timeout_seconds) as client:
        async def _process_chunk(i: int, chunk: list[Path]) -> list[dict]:
            async with semaphore:
                user_content = [
                    {"type": "text", "text": "Extract all entities from this document page image."},
                ]
                for img in chunk:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_encode_image(img)}"},
                    })
                payload = {
                    "model": settings.vl_model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.0,
                    "max_tokens": max_tokens,
                }
                response = await _post_with_retry(client, f"{selector.next()}/chat/completions", payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = _parse_vlm_json_response(content, f"entities visual page {i + 1}")
                if not parsed:
                    return []
                raw_entities = parsed.get("entities", [])
                result: list[dict] = []
                if isinstance(raw_entities, list):
                    for entity in raw_entities:
                        if not isinstance(entity, dict):
                            continue
                        validated = _validate_entity(entity, content)
                        if validated is None:
                            continue
                        confidence = float(entity.get("confidence", 0.0))
                        if confidence < settings.min_entity_confidence:
                            continue
                        handwritten = entity.get("handwritten")
                        if handwritten is None:
                            evidence_str = str(entity.get("evidence", ""))
                            value_str = str(entity.get("value", ""))
                            handwritten = "[HANDWRITTEN:" in evidence_str or "[HANDWRITTEN:" in value_str
                        result.append({
                            "type": str(entity.get("type", "other")),
                            "value": str(entity.get("value", "")),
                            "normalized_value": entity.get("normalized_value"),
                            "page": int(entity.get("page", i + 1)),
                            "paragraph": int(entity.get("paragraph", 0)),
                            "evidence": str(entity.get("evidence", "")),
                            "confidence": float(entity.get("confidence", 0.0)),
                            "handwritten": bool(handwritten),
                        })
                return result

        tasks = [_process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        chunk_results = await asyncio.gather(*tasks)

    all_entities: list[dict] = []
    for entities in chunk_results:
        all_entities.extend(entities)
    return _deduplicate_entities(all_entities)
