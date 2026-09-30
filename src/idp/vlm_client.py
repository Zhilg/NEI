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
from idp.document_classifier import detect_document_type
from idp.prompts import (
    annotation_system_prompt,
    combined_system_prompt,
    entity_system_prompt,
    markdown_system_prompt,
)

_ENTITY_SCHEMA_PATH = Path(__file__).parent / "entity_schema.json"

_OCR_ARTIFACT_PATTERNS = [
    re.compile(r"\b8OO(\s*мм|\s*mm)\b", re.IGNORECASE),
    re.compile(r"\bO0(\s*мм|\s*mm)\b", re.IGNORECASE),
    re.compile(r"(?<=\d)\s*-\s*(?=\d)"),
    re.compile(r"\b0+([1-9]\d*)\b"),
    re.compile(r"\b([1-9]\d*)0{3,}\b"),
]

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
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            print(f"VLM network error (attempt {attempt + 1}): {e}", file=sys.stderr)
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
            error_msg = f"VLM request failed: {response.status_code} {response.text[:500]}"
            print(error_msg, file=sys.stderr)
            raise RuntimeError(error_msg)
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
    if source_text and evidence and len(evidence) >= 3:
        normalized_source = " ".join(source_text.lower().split())
        normalized_evidence = " ".join(evidence.lower().split())
        if normalized_evidence not in normalized_source:
            pass
    return entity


def _process_entity(entity: dict, source_text: str, index: int, test_mode: bool = False) -> dict | None:
    validated = _validate_entity(entity, source_text)
    if validated is None:
        return None
    adjusted = _adjust_confidence(validated, source_text)
    confidence = float(adjusted.get("confidence", 0.0))
    handwritten = adjusted.get("handwritten")
    if handwritten is None:
        evidence_str = str(adjusted.get("evidence", ""))
        value_str = str(adjusted.get("value", ""))
        handwritten = "[HANDWRITTEN:" in evidence_str or "[HANDWRITTEN:" in value_str
    result = {
        "type": str(adjusted.get("type", "other")),
        "value": str(adjusted.get("value", "")),
        "normalized_value": adjusted.get("normalized_value"),
        "page": int(adjusted.get("page", index + 1)),
        "paragraph": int(adjusted.get("paragraph", 0)),
        "evidence": str(adjusted.get("evidence", "")),
        "confidence": confidence,
        "handwritten": bool(handwritten),
    }
    if test_mode and confidence < 0.5:
        result["comment"] = "Низкая уверенность, требуется проверка оператором"
    return result


def _adjust_confidence(entity: dict, source_text: str = "") -> dict:
    entity = dict(entity)
    confidence = float(entity.get("confidence", 0.0))
    value = str(entity.get("value", "")).strip()
    evidence = str(entity.get("evidence", "")).strip()
    handwritten = bool(entity.get("handwritten", False))

    if handwritten:
        confidence *= 0.85

    if source_text and evidence and len(evidence) >= 3:
        normalized_source = " ".join(source_text.lower().split())
        normalized_evidence = " ".join(evidence.lower().split())
        if normalized_evidence not in normalized_source:
            confidence *= 0.7

    if not value:
        confidence = 0.0

    entity["confidence"] = max(0.0, min(1.0, round(confidence, 4)))
    return entity


_PAGE_MARKER_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _detect_heading(text: str) -> tuple[str, int] | None:
    match = _HEADING_RE.match(text.strip())
    if match:
        level = len(match.group(1))
        return match.group(2).strip(), level
    return None


def extract_paragraphs(markdown: str) -> list[dict]:
    result: list[dict] = []
    page = 1
    para_num = 0
    current_lines: list[str] = []
    current_section: str | None = None
    current_section_level: int = 0

    def _flush() -> None:
        nonlocal para_num
        if current_lines:
            para_num += 1
            text = "\n".join(current_lines).strip()
            item: dict = {
                "page": page,
                "paragraph": para_num,
                "text": text,
            }
            if current_section:
                item["section"] = current_section
                item["section_level"] = current_section_level
            result.append(item)
            current_lines.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        match = _PAGE_MARKER_RE.match(stripped)
        if match is not None:
            _flush()
            page = int(match.group(1))
            para_num = 0
            continue
        heading = _detect_heading(stripped)
        if heading:
            _flush()
            current_section, current_section_level = heading
            current_lines.append(stripped)
            continue
        if stripped:
            current_lines.append(stripped)
        else:
            _flush()

    _flush()
    return result


async def generate_document_annotation(
    images: list[Path] | None = None,
    text: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
) -> str:
    if not images and not text:
        return ""
    endpoint = endpoint or settings.vl_endpoint
    model = model or settings.vl_model
    url = f"{endpoint}/chat/completions"
    selector = get_endpoint_selector()
    semaphore = asyncio.Semaphore(settings.vl_concurrency)

    async with httpx.AsyncClient(timeout=settings.vl_timeout_seconds) as client:
        async def _process() -> str:
            async with semaphore:
                if text:
                    user_content = [
                        {"type": "text", "text": f"Document text:\n{text}\n\nGenerate a brief annotation (1-2 sentences)."},
                    ]
                else:
                    user_content = [
                        {"type": "text", "text": "Generate a brief annotation (1-2 sentences) for this document page."},
                    ]
                    for img in images[:2]:
                        user_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_encode_image(img)}"},
                        })
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": annotation_system_prompt()},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 200,
                    "include_reasoning": False,
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20},
                }
                response = await _post_with_retry(client, f"{selector.next()}/chat/completions", payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return _strip_code_fences(_strip_thinking_blocks(content)).strip()

        return await _process()


async def reconstruct_markdown(images: list[Path]) -> str:
    if not images:
        return ""
    sys_prompt = markdown_system_prompt(test=settings.test_mode)
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
                    "include_reasoning": False,
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20},
                }
                response = await _post_with_retry(client, f"{selector.next()}/chat/completions", payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return _strip_code_fences(_strip_thinking_blocks(content))

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
    document_type: str = "other",
) -> list[dict]:
    model = model or settings.vl_model
    sys_prompt = entity_system_prompt(document_type=document_type, visual=False, test=settings.test_mode)
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
                    "include_reasoning": False,
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20},
                }
                url = f"{selector.next()}/chat/completions"
                response = await _post_with_retry(client, url, payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                content = _strip_thinking_blocks(content)
                print(f"ENT chunk {i+1} response length: {len(content) if content else 0}", file=sys.stderr)
                print(f"ENT chunk {i+1} preview: {content[:300] if content else 'EMPTY'}", file=sys.stderr)
                parsed = _parse_vlm_json_response(content, f"entities chunk {i + 1}")
                if not parsed:
                    print(f"ENT chunk {i+1} PARSE FAILED", file=sys.stderr)
                    return []
                raw_entities = parsed.get("entities", parsed.get("result", []))
                print(f"ENT chunk {i+1} raw entities: {len(raw_entities) if isinstance(raw_entities, list) else 'NOT A LIST'}", file=sys.stderr)
                result: list[dict] = []
                if isinstance(raw_entities, list):
                    for entity in raw_entities:
                        if not isinstance(entity, dict):
                            continue
                        validated = _validate_entity(entity, chunk)
                        if validated is None:
                            print(f"ENT chunk {i+1} FILTERED: {entity}", file=sys.stderr)
                            continue
                        processed = _process_entity(entity, chunk, i, test_mode=settings.test_mode)
                        if processed is None:
                            continue
                        confidence = float(processed.get("confidence", 0.0))
                        if not settings.test_mode and confidence < settings.min_entity_confidence:
                            print(f"ENT chunk {i+1} LOW CONF ({confidence}): {entity}", file=sys.stderr)
                            continue
                        result.append(processed)
                return result

        tasks = [_process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        chunk_results = await asyncio.gather(*tasks)

    all_entities: list[dict] = []
    for entities in chunk_results:
        all_entities.extend(entities)
    final = _deduplicate_entities(all_entities)
    normalized = normalize_entities(final, document_type=document_type)
    print(f"ENT total: {len(normalized)} entities after dedup+normalize", file=sys.stderr)
    return normalized


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


def _strip_thinking_blocks(content: str) -> str:
    import re
    patterns = [
        re.compile(r'.*?\s*>>>\s*', re.DOTALL | re.IGNORECASE),
        re.compile(r'<thinking>\s*.*?\s*</thinking>\s*', re.DOTALL | re.IGNORECASE),
        re.compile(r'\[THINKING\][^\[]*\[/THINKING\]', re.DOTALL | re.IGNORECASE),
        re.compile(r'<thought>\s*.*?\s*</thought>\s*', re.DOTALL | re.IGNORECASE),
        re.compile(r'<reasoning>\s*.*?\s*</reasoning>\s*', re.DOTALL | re.IGNORECASE),
        re.compile(r'<arg_value>[^\n]*</arg_value>', re.DOTALL | re.IGNORECASE),
        re.compile(r'thinking\s*:?\s*.*?\n', re.DOTALL | re.IGNORECASE),
        re.compile(r'First.*?\n', re.DOTALL | re.IGNORECASE),
        re.compile(r'Need.*?\n', re.DOTALL | re.IGNORECASE),
        re.compile(r'I will.*?\n', re.DOTALL | re.IGNORECASE),
        re.compile(r'Then.*?\n', re.DOTALL | re.IGNORECASE),
        re.compile(r'The user.*?\n', re.DOTALL | re.IGNORECASE),
        re.compile(r'Let me.*?\n', re.DOTALL | re.IGNORECASE),
        re.compile(r'I need.*?\n', re.DOTALL | re.IGNORECASE),
    ]
    result = content
    for pattern in patterns:
        result = pattern.sub('', result)
    result = re.sub(r'>>>\s*$', '', result, flags=re.DOTALL | re.IGNORECASE).strip()
    result = re.sub(r'</thinking>\s*$', '', result, flags=re.DOTALL | re.IGNORECASE).strip()
    result = re.sub(r'\[/THINKING\]\s*$', '', result, flags=re.DOTALL | re.IGNORECASE).strip()
    return result.strip()


def _clean_ocr_artifacts(text: str) -> str:
    cleaned = text
    for pattern in _OCR_ARTIFACT_PATTERNS:
        cleaned = pattern.sub(lambda m: _fix_ocr_match(m), cleaned)
    return cleaned


def _fix_ocr_match(m: re.Match) -> str:
    full = m.group(0)
    if full.startswith("8OO"):
        return "800" + full[3:]
    if full.startswith("O0"):
        return "00" + full[2:]
    return full.replace("-", " ")


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


async def extract_markdown_and_entities(images: list[Path], document_type: str = "other") -> tuple[str, list[dict]]:
    if not images:
        return "", []
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
                entity_prompt = entity_system_prompt(document_type=document_type, visual=True, test=settings.test_mode)
                system_content = (
                    combined_system_prompt(document_type=document_type, test=settings.test_mode)
                    + "\n\n"
                    + "ENTITY EXTRACTION RULES:\n"
                    + entity_prompt
                )
                payload = {
                    "model": settings.vl_model,
                    "messages": [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.0,
                    "max_tokens": max_tokens,
                    "include_reasoning": False,
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20},
                }
                url = f"{selector.next()}/chat/completions"
                print(f"VLM request to {url}, images: {len(chunk)}, model: {settings.vl_model}", file=sys.stderr)
                response = await _post_with_retry(client, url, payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                content = _strip_thinking_blocks(content)
                print(f"VLM response length: {len(content) if content else 0}", file=sys.stderr)
                print(f"VLM response preview: {content[:200] if content else 'EMPTY'}", file=sys.stderr)
                parsed = _parse_vlm_json_response(content, f"combined page {i + 1}")
                if not parsed:
                    print(f"VLM parse failed for chunk {i + 1}", file=sys.stderr)
                    return "", []
                md = parsed.get("markdown", "")
                raw_entities = parsed.get("entities", [])
                entities: list[dict] = []
                if isinstance(raw_entities, list):
                    for entity in raw_entities:
                        if not isinstance(entity, dict):
                            continue
                        processed = _process_entity(entity, md, i, test_mode=settings.test_mode)
                        if processed is None:
                            continue
                        confidence = float(processed.get("confidence", 0.0))
                        if not settings.test_mode and confidence < settings.min_entity_confidence:
                            continue
                        entities.append(processed)
                return md, entities

        tasks = [_process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        chunk_results = await asyncio.gather(*tasks)

    markdown_parts: list[str] = []
    all_entities: list[dict] = []
    for i, (md, entities) in enumerate(chunk_results):
        if isinstance(md, str) and md.strip():
            markdown_parts.append(f"<!-- page {i + 1} -->\n{md}")
        all_entities.extend(entities)
    return "\n\n".join(markdown_parts), _deduplicate_entities(normalize_entities(all_entities, document_type=document_type))


def get_entity_system_prompt(document_type: str = "other") -> str:
    return entity_system_prompt(document_type=document_type, visual=False, test=settings.test_mode)


def normalize_entities(entities: list[dict], document_type: str = "other") -> list[dict]:
    normalized: list[dict] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        value = str(entity.get("value", "")).strip()
        if not value:
            continue
        etype = str(entity.get("type", "")).lower()
        norm_value = entity.get("normalized_value")
        if document_type in {"ttz", "ttkh", "tech_doc"}:
            if re.search(r"\b\d+(?:\.\d+)?\s*(?:мм|см|м|км|кг|г|с|мс|Гц|кГц|МГц|В|А|Вт|кВт)\b", value, re.IGNORECASE):
                if etype in {"amount", "identifier", "other"}:
                    entity = dict(entity)
                    entity["type"] = "amount"
                    if not norm_value:
                        norm_value = "NUM UNIT"
            if re.search(r"от\s+\d+(?:\.\d+)?\s*до\s+\d+(?:\.\d+)?", value, re.IGNORECASE):
                entity = dict(entity)
                if not norm_value:
                    norm_value = "RANGE_NUM"
            if re.search(r"[±\+]\s*\d+(?:\.\d+)?", value):
                entity = dict(entity)
                if not norm_value:
                    norm_value = "TOLERANCE_NUM"
        if etype == "date" and not norm_value:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", value)
            if m:
                entity = dict(entity)
                norm_value = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        if norm_value:
            entity["normalized_value"] = norm_value
        normalized.append(entity)
    return normalized


async def extract_entities_from_images(images: list[Path], document_type: str = "other") -> list[dict]:
    if not images:
        return []
    sys_prompt = entity_system_prompt(document_type=document_type, visual=True, test=settings.test_mode)
    if document_type != "other" and not settings.test_mode:
        sys_prompt = entity_system_prompt(document_type=document_type, visual=True, test=False)
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
                    "include_reasoning": False,
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20},
                }
                response = await _post_with_retry(client, f"{selector.next()}/chat/completions", payload, selector=selector)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                content = _strip_thinking_blocks(content)
                parsed = _parse_vlm_json_response(content, f"entities visual page {i + 1}")
                if not parsed:
                    return []
                raw_entities = parsed.get("entities", [])
                result: list[dict] = []
                if isinstance(raw_entities, list):
                    for entity in raw_entities:
                        if not isinstance(entity, dict):
                            continue
                        processed = _process_entity(entity, content, i, test_mode=settings.test_mode)
                        if processed is None:
                            continue
                        confidence = float(processed.get("confidence", 0.0))
                        if not settings.test_mode and confidence < settings.min_entity_confidence:
                            continue
                        result.append(processed)
                return result

        tasks = [_process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        chunk_results = await asyncio.gather(*tasks)

    all_entities: list[dict] = []
    for entities in chunk_results:
        all_entities.extend(entities)
    return _deduplicate_entities(normalize_entities(all_entities, document_type=document_type))
