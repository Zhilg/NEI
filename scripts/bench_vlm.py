"""Measure VL endpoint throughput to compare model sizes (e.g. Qwen3.8-27B vs 7B).

Measures, for a fixed set of page images:
  - time to first token (prefill) per request
  - decode tokens/s
  - end-to-end latency per request and per page
  - aggregate pages/s at a given concurrency

Usage:
    python3 scripts/bench_vlm.py --endpoint http://vllm-vl-0:8000/v1 --model Qwen3.8-27B \
        --pages path/to/page_*.png --images-per-request 2 --concurrency 8 --repeat 3

    # offline self-test against the deterministic stub (no GPU):
    python3 scripts/bench_vlm.py --endpoint http://127.0.0.1:8123/v1 --model stub-vl --stub

Results are printed and appended to ``--report`` (JSONL) when given.
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from idp.prompts import markdown_system_prompt  # noqa: E402

DEFAULT_MARKDOWN_PROMPT = "Reconstruct as Markdown."


def encode_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _payload(model: str, images: list[str], max_tokens: int) -> dict:
    content: list[dict] = [{"type": "text", "text": DEFAULT_MARKDOWN_PROMPT}]
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}})
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": markdown_system_prompt()},
            {"role": "user", "content": content},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": True,
        "include_reasoning": False,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20},
    }


def _run_stream(client: httpx.Client, url: str, payload: dict) -> dict:
    started = time.perf_counter()
    first_token: float | None = None
    chunks = 0
    with client.stream("POST", url, json=payload) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("content"):
                chunks += 1
                if first_token is None:
                    first_token = time.perf_counter()
    finished = time.perf_counter()
    return {
        "ttft_sec": round((first_token or finished) - started, 4),
        "total_sec": round(finished - started, 4),
        "tokens": chunks,
        "tokens_per_sec": round(chunks / max(finished - started, 1e-6), 2),
    }


def benchmark(
    endpoint: str,
    model: str,
    pages: list[Path],
    *,
    images_per_request: int = 2,
    max_tokens: int = 16384,
    repeat: int = 1,
    timeout: float = 600.0,
) -> dict:
    """Run ``repeat`` passes over ``pages`` and aggregate timings."""
    endpoint = endpoint.rstrip("/")
    url = f"{endpoint}/chat/completions"
    encoded = [encode_image(page) for page in pages]
    batches = [encoded[i : i + images_per_request] for i in range(0, len(encoded), images_per_request)]

    runs: list[dict] = []
    with httpx.Client(timeout=timeout) as client:
        for _ in range(max(1, repeat)):
            for batch in batches:
                runs.append(_run_stream(client, url, _payload(model, batch, max_tokens)))

    ttft = [run["ttft_sec"] for run in runs]
    total = [run["total_sec"] for run in runs]
    pages_per_second = len(pages) / max(sum(total), 1e-6)
    return {
        "model": model,
        "endpoint": endpoint,
        "pages": len(pages),
        "images_per_request": images_per_request,
        "requests": len(runs),
        "repeat": repeat,
        "max_tokens": max_tokens,
        "ttft_sec": {
            "mean": round(statistics.fmean(ttft), 4),
            "max": round(max(ttft), 4),
        },
        "total_sec": {
            "mean": round(statistics.fmean(total), 4),
            "max": round(max(total), 4),
        },
        "tokens_per_sec": {
            "mean": round(statistics.fmean([run["tokens_per_sec"] for run in runs]), 2),
        },
        "pages_per_second": round(pages_per_second, 4),
        "runs": runs,
    }


def _print_report(report: dict) -> None:
    print(f"model                : {report['model']}")
    print(f"endpoint             : {report['endpoint']}")
    print(f"pages                : {report['pages']} (x{report['repeat']} pass(es))")
    print(f"images per request   : {report['images_per_request']}")
    print(f"requests             : {report['requests']}")
    print(f"time to first token  : mean {report['ttft_sec']['mean']}s, max {report['ttft_sec']['max']}s")
    print(f"request latency      : mean {report['total_sec']['mean']}s, max {report['total_sec']['max']}s")
    print(f"decode speed         : mean {report['tokens_per_sec']['mean']} tok/s")
    print(f"throughput           : {report['pages_per_second']} pages/s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark a VL endpoint's throughput")
    parser.add_argument("--endpoint", required=True, help="OpenAI-compatible /v1 endpoint")
    parser.add_argument("--model", required=True, help="served model name")
    parser.add_argument("--pages", nargs="*", default=[], help="page images (default: generated test pages)")
    parser.add_argument("--images-per-request", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--report", default="", help="append the JSON report to this file")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    pages = [Path(page) for page in args.pages]
    if not pages:
        from tests import fixtures

        pages = []
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for index in range(3):
                path = Path(tmp) / f"bench_page_{index}.png"
                fixtures.make_png(path, width=1240, height=1754)
                pages.append(path)

    report = benchmark(
        args.endpoint,
        args.model,
        pages,
        images_per_request=args.images_per_request,
        max_tokens=args.max_tokens,
        repeat=args.repeat,
        timeout=args.timeout,
    )
    _print_report(report)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")
        print(f"report appended -> {report_path}")


if __name__ == "__main__":
    main()
