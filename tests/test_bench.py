"""Bench script test against the stub (measures the stub, not a GPU)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests import fixtures
from tests.vlm_stub import StubServer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def test_benchmark_against_stub(tmp_path):
    import bench_vlm

    pages = [
        fixtures.make_png(tmp_path / f"page_{index}.png", width=1240, height=1754)
        for index in range(3)
    ]
    with StubServer() as server:
        report = bench_vlm.benchmark(
            server.endpoint, "stub-vl", pages, images_per_request=2, repeat=2
        )

    assert report["pages"] == 3
    assert report["requests"] == 4  # two batches x two passes
    assert report["pages_per_second"] > 0
    assert report["ttft_sec"]["mean"] >= 0
    assert report["total_sec"]["mean"] > 0
    assert all(run["tokens"] > 0 for run in report["runs"])


def test_benchmark_cli_writes_report(tmp_path):
    from tests import fixtures as fixture_module

    page = fixture_module.make_png(tmp_path / "page.png", width=1240, height=1754)
    report = tmp_path / "bench.jsonl"
    with StubServer() as server:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "bench_vlm.py"),
                "--endpoint",
                server.endpoint,
                "--model",
                "stub-vl",
                "--pages",
                str(page),
                "--repeat",
                "1",
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert result.returncode == 0, result.stderr
    assert "pages/s" in result.stdout
    records = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
    assert records and records[0]["model"] == "stub-vl"
