"""Deterministic OpenAI-compatible VLM stub for offline testing.

Serves ``/v1/models`` and ``/v1/chat/completions`` so the whole pipeline
(worker, operator, reconstructor) can be exercised without a GPU.

Markdown responses are derived from the page images in the request: every
image gets a heading ``## Страница <hash10>`` plus a paragraph, list and
table, so tests can assert that every page's content reached the output.

Standalone usage:
    python3 tests/vlm_stub.py --port 8123

Programmatic usage (see conftest.py): the server is started on an ephemeral
port in a background thread and ``settings.vl_endpoints`` is pointed at it.

Environment / CLI flags:
    --fail-first N     fail the first N chat requests with HTTP 503 (retry tests)
    --model NAME       model name reported by /v1/models
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MARKDOWN_SYSTEM_MARKERS = (
    "Reconstruct the document page as clean Markdown",
    "Convert the document images to Markdown",
    "Reconstruct as Markdown",
)
ANNOTATION_MARKERS = ("Generate a brief annotation", "brief annotation")
ENTITY_MARKERS = ("Extract entities", "Extract all entities")

ENTITIES = [
    {"type": "person", "value": "Иванов И.И.", "page": 1, "paragraph": 1,
     "evidence": "Иванов И.И.", "confidence": 0.9, "handwritten": True},
    {"type": "amount", "value": "12.5", "page": 1, "paragraph": 1,
     "evidence": "12.5", "confidence": 0.8, "handwritten": False},
    {"type": "date", "value": "12.05.2026", "page": 3, "paragraph": 1,
     "evidence": "12.05.2026", "confidence": 0.9, "handwritten": False},
]


def hash_bytes(data: bytes) -> str:
    """Stable 10-char hash of raw image bytes (identifies a page render)."""
    return hashlib.sha256(data).hexdigest()[:10]


def image_hash(data_url: str) -> str:
    """Stable 10-char hash of a base64 data URL (identifies a page render)."""
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        raw = data_url.encode("utf-8")
    return hash_bytes(raw)


def markdown_for_images(data_urls: list[str]) -> str:
    """One heading/paragraph/list/table block per page image in the request."""
    blocks: list[str] = []
    for index, data_url in enumerate(data_urls, start=1):
        digest = image_hash(data_url)
        blocks.append(
            f"## Страница {digest}\n\n"
            f"Содержание страницы {index}: сводные данные по объекту {digest} "
            f"приведены в таблице ниже.\n\n"
            f"- пункт {index}.1;\n- пункт {index}.2.\n\n"
            f"| Параметр | Значение | Ед. изм. |\n"
            f"|---|---|---|\n"
            f"| Масса {digest} | 12.5 | кг |\n"
            f"| Длина {digest} | 3400 | мм |"
        )
        if index == 1:
            blocks.append(
                f"[Image: chart: график выполнения | bbox: 120 300 880 700]\n\n"
                f"Уточнённое значение [HANDWRITTEN: {digest} | bbox: 610 420 760 455] "
                f"проставлено от руки."
            )
    return "\n\n".join(blocks)


def _request_images(payload: dict) -> list[str]:
    images: list[str] = []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = str(part.get("image_url", {}).get("url", ""))
                if url:
                    images.append(url)
    return images


def _detect_kind(system: str, user_text: str) -> str:
    haystack = f"{system}\n{user_text}"
    if any(marker in haystack for marker in MARKDOWN_SYSTEM_MARKERS):
        return "markdown"
    if any(marker in haystack for marker in ANNOTATION_MARKERS):
        return "annotation"
    if any(marker in haystack for marker in ENTITY_MARKERS):
        return "entities"
    return "annotation"


class StubState:
    def __init__(self, model: str = "stub-vl", fail_first: int = 0) -> None:
        self.model = model
        self.fail_first = fail_first
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []


def make_handler(state: StubState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # silence
            return

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                self._json({
                    "object": "list",
                    "data": [{"id": state.model, "object": "model", "created": 0, "owned_by": "stub"}],
                })
                return
            self._json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self._json({"error": "bad json"}, status=400)
                return

            images = _request_images(payload)
            messages = payload.get("messages") or []
            system = next((str(m.get("content", "")) for m in messages if m.get("role") == "system"), "")
            user_text = ""
            for message in messages:
                content = message.get("content")
                if isinstance(content, str):
                    user_text += " " + content
                elif isinstance(content, list):
                    user_text += " " + " ".join(
                        str(part.get("text", "")) for part in content if isinstance(part, dict)
                    )
            kind = _detect_kind(system, user_text)

            with state.lock:
                state.requests.append({
                    "kind": kind,
                    "model": payload.get("model"),
                    "images": [image_hash(url) for url in images],
                })

            if state.fail_first > 0:
                state.fail_first -= 1
                self._json({"error": "stub injected failure"}, status=503)
                return

            if kind == "markdown":
                content = markdown_for_images(images)
            elif kind == "entities":
                content = json.dumps({"entities": ENTITIES}, ensure_ascii=False)
            else:
                content = "Служебный документ с табличными данными и подписями."

            if payload.get("stream"):
                self._stream(content, payload.get("model") or state.model)
                return

            self._json({
                "id": "stub-1",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model") or state.model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            })

        def _stream(self, content: str, model: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def _event(payload: dict) -> None:
                body = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(body)
                self.wfile.flush()

            base = {
                "id": "stub-stream",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
            }
            _event({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            # split into word-ish chunks so clients measure a realistic decode
            step = max(1, len(content) // 24)
            for start in range(0, len(content), step):
                _event({**base, "choices": [{"index": 0, "delta": {"content": content[start:start + step]}, "finish_reason": None}]})
            _event({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

    return Handler


class StubServer:
    """Context manager: runs the stub on an ephemeral port in a thread."""

    def __init__(self, fail_first: int = 0, model: str = "stub-vl") -> None:
        self.state = StubState(model=model, fail_first=fail_first)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> StubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic OpenAI-compatible VLM stub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--model", default="stub-vl")
    parser.add_argument("--fail-first", type=int, default=0)
    args = parser.parse_args()

    state = StubState(model=args.model, fail_first=args.fail_first)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"VLM stub listening on http://{args.host}:{args.port}/v1 (model={args.model})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
