"""Client for local vLLM VL model."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path

import httpx

from idp.config import settings
from idp.feedback_store import FeedbackStore
from idp.prompts import (
    annotation_system_prompt,
    entity_system_prompt,
    markdown_system_prompt,
)

logger = logging.getLogger(__name__)

def _encode_image(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def _chunked(images: list[Path], size: int) -> list[list[Path]]:
    return [images[i : i + size] for i in range(0, len(images), size)]


class FeedbackCache:
    def __init__(self) -> None:
        self._store = FeedbackStore()
        self._cache: dict[str, str] = {}

    def get_block(self, force_refresh: bool = False) -> str:
        if not settings.finetune_feedback_enabled:
            return ""
        limit = settings.finetune_max_feedback_examples
        if limit <= 0:
            return ""
        path = Path(settings.operator_corrections_path)
        try:
            stat = path.stat()
            token = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            token = "empty"
        if not force_refresh and self._cache.get(token):
            return self._cache[token]
        block = self._store.build_feedback_block(limit=limit)
        self._cache.clear()
        self._cache[token] = block
        return block


_feedback_cache = FeedbackCache()


_BBOX_SUFFIX_RE = re.compile(
    r"\s*(?:\|\s*(?:bbox:\s*[\d.\s]+?|img:\s*\d+)\s*)+(?=\]|$)",
    re.IGNORECASE,
)


def strip_bbox_annotations(text: str) -> str:
    """Remove region annotations (bbox/img) that the VLM adds for image extraction.

    Keeps markdown clean for paragraphs, entities and the .md artifact while the
    annotated version is used to cut image regions out of page renders.
    """
    return _BBOX_SUFFIX_RE.sub("", text)


def with_feedback(system_prompt: str, force_refresh: bool = False) -> str:
    block = _feedback_cache.get_block(force_refresh=force_refresh)
    if not block:
        return system_prompt
    return f"{system_prompt}\n\n{block}"


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


def _extract_entities_fallback(text: str) -> list[dict]:
    entities: list[dict] = []
    for match in re.finditer(r'"type"\s*:\s*"([^"]+)"', text):
        etype = match.group(1)
        after = text[match.end():match.end() + 300]
        value_match = re.search(r'"value"\s*:\s*"((?:[^"\\]|\\.)*)"', after)
        if not value_match:
            value_match = re.search(r'"value"\s*:\s*\'((?:[^\'\\]|\\.)*)\'', after)
        if not value_match:
            value_match = re.search(r'"value"\s*:\s*([^,\}\n\r]+)', after)
        if value_match:
            value = value_match.group(1).strip().strip('"').strip("'")
            if value:
                entities.append({"type": etype, "value": value})
    return entities


_LONG_VALUE_TYPES = {
    "handwritten_text", "handwritten_note",
    "handwritten_amount", "payment_details",
}
_SHORT_VALUE_TYPES = {"stamp", "signature", "handwritten_signature"}
_JUNK_TYPES: set[str] = set()


def _validate_entity(entity: dict) -> dict | None:
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
    return entity


_LOW_CONFIDENCE_COMMENTS = (
    (0.3, "Рукопись плохо читается, требуется проверка оператором"),
    (0.5, "Рукопись частично нечёткая, рекомендуется проверить"),
    (0.7, "Рукопись допускает неточности, рекомендуется уточнить"),
)
_DEFAULT_LOW_CONFIDENCE_COMMENT = "Низкая уверенность, требуется проверка оператором"


def _low_confidence_comment(confidence: float, handwritten: bool) -> str:
    if not handwritten:
        return _DEFAULT_LOW_CONFIDENCE_COMMENT
    for threshold, comment in _LOW_CONFIDENCE_COMMENTS:
        if confidence < threshold:
            return comment
    return _DEFAULT_LOW_CONFIDENCE_COMMENT


def _process_entity(entity: dict, source_text: str, page: int, test_mode: bool = False) -> dict | None:
    """Normalize one raw VLM entity; ``page`` is the 1-based page the source chunk starts at."""
    validated = _validate_entity(entity)
    if validated is None:
        return None
    adjusted = _adjust_confidence(validated, source_text)
    confidence = float(adjusted.get("confidence", 0.0))
    handwritten = adjusted.get("handwritten")
    if handwritten is None:
        evidence_str = str(adjusted.get("evidence", ""))
        value_str = str(adjusted.get("value", ""))
        handwritten = "[HANDWRITTEN:" in evidence_str or "[HANDWRITTEN:" in value_str
    try:
        entity_page = int(adjusted.get("page") or 0) or max(1, page)
    except (TypeError, ValueError):
        entity_page = max(1, page)
    result = {
        "type": str(adjusted.get("type", "other")),
        "value": str(adjusted.get("value", "")),
        "normalized_value": adjusted.get("normalized_value"),
        "page": entity_page,
        "paragraph": int(adjusted.get("paragraph", 0)),
        "evidence": str(adjusted.get("evidence", "")),
        "confidence": confidence,
        "handwritten": bool(handwritten),
    }
    if test_mode and confidence < 0.5:
        result["comment"] = _low_confidence_comment(confidence, bool(handwritten))
    return result


_HANDWRITTEN_DEFAULT_LOW = 0.35
_HANDWRITTEN_DEFAULT_HIGH = 0.70
_HANDWRITTEN_MAX_EVIDENCE_LEN = 60
_CONFUSABLE_CHARS = frozenset("0OoДЗ8BВ5Sбь9гqяРрPpCсТтАаЕеКкМмНнУуХх")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_HANDWRITTEN_MARKER_RE = re.compile(r"\[HANDWRITTEN:\s*([^\]]*)\]", re.IGNORECASE)


def _strip_handwritten_marker(snippet: str) -> str:
    return _HANDWRITTEN_MARKER_RE.sub(r"\1", snippet)


def _ambiguity_factor(snippet: str) -> float:
    ambiguous = 0
    for token in _TOKEN_RE.findall(_strip_handwritten_marker(snippet)):
        has_digit = any(ch.isdigit() for ch in token)
        has_alpha = any(ch.isalpha() for ch in token)
        if has_digit and has_alpha:
            ambiguous += 1
            continue
        if has_alpha and len(token) >= 3:
            confusables = sum(1 for ch in token if ch in _CONFUSABLE_CHARS)
            if confusables / len(token) >= 0.6:
                ambiguous += 1
    return max(0.8, 1.0 - 0.08 * ambiguous)


def _handwriting_penalty(value: str, evidence: str, source_text: str) -> float:
    penalty = 1.0
    snippet = _strip_handwritten_marker(evidence.strip() or value.strip())
    if snippet:
        length = len(snippet)
        if length <= 4:
            penalty *= 0.75
        elif length <= 10:
            penalty *= 0.85
        elif length > _HANDWRITTEN_MAX_EVIDENCE_LEN:
            penalty *= 0.9
        penalty *= _ambiguity_factor(snippet)
    if source_text:
        marker_count = source_text.count("[HANDWRITTEN:")
        if marker_count >= 2:
            penalty *= 1.0 - min(0.15, 0.03 * (marker_count - 1))
    return penalty


def _adjust_confidence(entity: dict, source_text: str = "") -> dict:
    entity = dict(entity)
    raw_confidence = entity.get("confidence")
    confidence = float(raw_confidence) if raw_confidence is not None else 0.5
    value = str(entity.get("value", "")).strip()
    evidence = str(entity.get("evidence", "")).strip()
    handwritten = bool(entity.get("handwritten", False))
    if not handwritten and "[HANDWRITTEN:" in f"{value} {evidence}":
        handwritten = True

    if handwritten:
        uncalibrated = raw_confidence is None or confidence >= 1.0
        if uncalibrated:
            penalty = _handwriting_penalty(value, evidence, source_text)
            confidence = (
                _HANDWRITTEN_DEFAULT_HIGH
                - (_HANDWRITTEN_DEFAULT_HIGH - _HANDWRITTEN_DEFAULT_LOW) * (1.0 - penalty)
            )
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


def _chunk_text(text: str, max_chars: int) -> list[tuple[str, int]]:
    """Split text into chunks; each chunk is paired with its start offset in ``text``."""
    if len(text) <= max_chars:
        return [(text, 0)]
    paragraphs = text.split("\n\n")
    chunks: list[tuple[str, int]] = []
    current: list[str] = []
    current_len = 0
    current_start = 0
    cursor = 0

    def offset_of(paragraph: str) -> int:
        """Offset of the next occurrence of ``paragraph`` at or after the cursor."""
        nonlocal cursor
        position = text.find(paragraph, cursor)
        if position < 0:
            position = cursor
        cursor = position + len(paragraph)
        return position

    for para in paragraphs:
        para_offset = offset_of(para)
        para_len = len(para)
        if para_len > max_chars:
            if current:
                chunks.append(("\n\n".join(current), current_start))
                current = []
                current_len = 0
            words = para.split()
            sub_chunk: list[str] = []
            sub_len = 0
            for word in words:
                if sub_len + len(word) + 1 > max_chars and sub_chunk:
                    chunks.append((" ".join(sub_chunk), para_offset))
                    sub_chunk = []
                    sub_len = 0
                sub_chunk.append(word)
                sub_len += len(word) + 1
            if sub_chunk:
                chunks.append((" ".join(sub_chunk), para_offset))
            continue
        if current_len + para_len + 2 > max_chars and current:
            chunks.append(("\n\n".join(current), current_start))
            current = []
            current_len = 0
        if not current:
            current_start = para_offset
        current.append(para)
        current_len += para_len + 2
    if current:
        chunks.append(("\n\n".join(current), current_start))
    return chunks


def _page_markers(text: str) -> list[tuple[int, int]]:
    """(offset, page number) of every ``<!-- page N -->`` marker in document order."""
    markers: list[tuple[int, int]] = []
    position = 0
    while True:
        match = _PAGE_MARKER_RE.search(text, position)
        if match is None:
            break
        markers.append((match.start(), int(match.group(1))))
        position = match.end()
    return markers


def _page_at_offset(markers: list[tuple[int, int]], offset: int) -> int:
    """1-based page a chunk starting at ``offset`` belongs to (default 1)."""
    page = 1
    for marker_offset, marker_page in markers:
        if marker_offset <= offset:
            page = marker_page
        else:
            break
    return max(1, page)


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
    patterns = [
        re.compile(r'<thinking>\s*.*?\s*</thinking>\s*', re.DOTALL | re.IGNORECASE),
        re.compile(r'\[THINKING\][^\[]*\[/THINKING\]', re.DOTALL | re.IGNORECASE),
        re.compile(r'<thought>\s*.*?\s*</thought>\s*', re.DOTALL | re.IGNORECASE),
        re.compile(r'<reasoning>\s*.*?\s*</reasoning>\s*', re.DOTALL | re.IGNORECASE),
        re.compile(r'<arg_value>[^\n]*</arg_value>', re.DOTALL | re.IGNORECASE),
    ]
    result = content
    for pattern in patterns:
        result = pattern.sub('', result)
    result = re.sub(r'</thinking>\s*$', '', result, flags=re.DOTALL | re.IGNORECASE).strip()
    result = re.sub(r'\[/THINKING\]\s*$', '', result, flags=re.DOTALL | re.IGNORECASE).strip()
    return result.strip()


def clean_ocr_artifacts(text: str) -> str:
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
    fallback = _extract_entities_fallback(cleaned)
    if fallback:
        return {"entities": fallback}
    _write_trash(context, content)
    logger.warning("VLM %s JSON parse error", context)
    logger.debug("  Raw response: %s", content[:300])
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


class VlmClient:
    def __init__(self) -> None:
        self._endpoints = get_vl_endpoints()

    def _http(self, endpoints: list[str] | None = None) -> _HttpClient:
        selector = RoundRobinEndpointSelector(endpoints if endpoints is not None else self._endpoints)
        return _HttpClient(_shared_client(), selector)

    def _semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(settings.vl_concurrency)

    def _chunked(self, images: list[Path]) -> list[list[Path]]:
        return _chunked(images, settings.vl_max_images)

    def _test_max_tokens(self) -> int:
        return 256 if settings.test_mode else settings.vl_max_tokens

    def _model(self, override: str | None = None) -> str:
        return override or settings.vl_model

    def _prompt_kwargs(self) -> dict:
        return {"test": settings.test_mode}

    @staticmethod
    def _collect_entities(
        raw_entities: object,
        source_text: str,
        page: int,
    ) -> list[dict]:
        result: list[dict] = []
        if not isinstance(raw_entities, list):
            return result
        for entity in raw_entities:
            if not isinstance(entity, dict):
                continue
            processed = _process_entity(entity, source_text, page, test_mode=settings.test_mode)
            if processed is None:
                continue
            confidence = float(processed.get("confidence", 0.0))
            if not settings.test_mode and confidence < settings.min_entity_confidence:
                continue
            result.append(processed)
        return result

    async def generate_document_annotation(
        self,
        images: list[Path] | None = None,
        text: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
    ) -> str:
        if not images and not text:
            return ""
        model = self._model(model)
        http = self._http([endpoint] if endpoint else None)

        async with self._semaphore():
            if text:
                user_content = [
                    {"type": "text", "text": f"Document text:\n{text}\n\nGenerate a brief annotation (1-2 sentences)."},
                ]
            else:
                user_content = [
                    {"type": "text", "text": "Generate a brief annotation (1-2 sentences) for this document page."},
                ]
                for img in (images or [])[:2]:
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
            response = await http.post(f"{http.selector.next()}/chat/completions", payload)
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return _strip_code_fences(_strip_thinking_blocks(content)).strip()

    async def reconstruct_markdown(self, images: list[Path]) -> str:
        if not images:
            return ""
        sys_prompt = with_feedback(markdown_system_prompt(**self._prompt_kwargs()))
        chunks = self._chunked(images)
        max_tokens = self._test_max_tokens()
        http = self._http()
        sem = self._semaphore()

        async def _process_chunk(chunk: list[Path]) -> str:
            async with sem:
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
                response = await http.post(f"{http.selector.next()}/chat/completions", payload)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return _strip_code_fences(_strip_thinking_blocks(content))

        tasks = [_process_chunk(chunk) for chunk in chunks]
        parts = await asyncio.gather(*tasks)

        marked = []
        for i, part in enumerate(parts):
            marked.append(f"<!-- page {i + 1} -->\n{part}")
        return "\n\n".join(marked)

    async def extract_entities_from_text(
        self,
        text: str,
        endpoint: str | None = None,
        model: str | None = None,
        document_type: str = "other",
    ) -> list[dict]:
        model = self._model(model)
        sys_prompt = with_feedback(entity_system_prompt(document_type=document_type, visual=False, **self._prompt_kwargs()))
        max_tokens = self._test_max_tokens()
        max_chars = 500 if settings.test_mode else 6000
        chunks = _chunk_text(text, max_chars)
        markers = _page_markers(text)
        http = self._http([endpoint] if endpoint else None)
        sem = self._semaphore()

        async def _process_chunk(index: int, chunk: str, page: int) -> list[dict]:
            async with sem:
                prompt = f"Text:\n{chunk}\n\nExtract entities and return JSON only."
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
                url = f"{http.selector.next()}/chat/completions"
                response = await http.post(url, payload)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                content = _strip_thinking_blocks(content)
                parsed = _parse_vlm_json_response(content, f"entities chunk {index + 1}")
                if not parsed:
                    return []
                return self._collect_entities(
                    parsed.get("entities", parsed.get("result", [])), chunk, page
                )

        tasks = [
            _process_chunk(i, chunk, _page_at_offset(markers, offset))
            for i, (chunk, offset) in enumerate(chunks)
        ]
        chunk_results = await asyncio.gather(*tasks)

        all_entities: list[dict] = []
        for entities in chunk_results:
            all_entities.extend(entities)
        final = _deduplicate_entities(all_entities)
        return normalize_entities(final, document_type=document_type)

    async def extract_entities_from_images(self, images: list[Path], document_type: str = "other") -> list[dict]:
        if not images:
            return []
        test_kwargs = self._prompt_kwargs()
        if document_type != "other" and not settings.test_mode:
            test_kwargs = {"test": False}
        sys_prompt = with_feedback(entity_system_prompt(document_type=document_type, visual=True, **test_kwargs))
        max_tokens = self._test_max_tokens()
        chunks = self._chunked(images)
        http = self._http()
        sem = self._semaphore()

        async def _process_chunk(index: int, chunk: list[Path], page: int) -> list[dict]:
            async with sem:
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
                response = await http.post(f"{http.selector.next()}/chat/completions", payload)
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                content = _strip_thinking_blocks(content)
                parsed = _parse_vlm_json_response(content, f"entities visual page {index + 1}")
                if not parsed:
                    return []
                return self._collect_entities(parsed.get("entities", []), content, page)

        tasks = [
            _process_chunk(i, chunk, i * settings.vl_max_images + 1)
            for i, chunk in enumerate(chunks)
        ]
        chunk_results = await asyncio.gather(*tasks)

        all_entities: list[dict] = []
        for entities in chunk_results:
            all_entities.extend(entities)
        return _deduplicate_entities(normalize_entities(all_entities, document_type=document_type))


_shared_httpx_client: httpx.AsyncClient | None = None


def _shared_client() -> httpx.AsyncClient:
    """One connection pool shared by every VLM request in this process."""
    global _shared_httpx_client
    if _shared_httpx_client is None or _shared_httpx_client.is_closed:
        _shared_httpx_client = httpx.AsyncClient(
            timeout=settings.vl_timeout_seconds,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
    return _shared_httpx_client


async def close_vlm_client() -> None:
    global _shared_httpx_client
    if _shared_httpx_client is not None and not _shared_httpx_client.is_closed:
        await _shared_httpx_client.aclose()
    _shared_httpx_client = None


_default_vlm_client = VlmClient()


async def generate_document_annotation(
    images: list[Path] | None = None,
    text: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
) -> str:
    return await _default_vlm_client.generate_document_annotation(images=images, text=text, endpoint=endpoint, model=model)


async def reconstruct_markdown(images: list[Path]) -> str:
    return await _default_vlm_client.reconstruct_markdown(images)


async def extract_entities_from_text(
    text: str,
    endpoint: str | None = None,
    model: str | None = None,
    document_type: str = "other",
) -> list[dict]:
    return await _default_vlm_client.extract_entities_from_text(text=text, endpoint=endpoint, model=model, document_type=document_type)


async def extract_entities_from_images(images: list[Path], document_type: str = "other") -> list[dict]:
    return await _default_vlm_client.extract_entities_from_images(images=images, document_type=document_type)
