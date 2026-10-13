"""Shared fixtures: VLM stub, isolated input/output roots, sample documents."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from idp.config import settings  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.vlm_stub import StubServer  # noqa: E402


@pytest.fixture()
def stub_server():
    """Run the deterministic VLM stub and point the pipeline at it."""
    with StubServer() as server:
        previous_endpoints = settings.vl_endpoints
        previous_endpoint = settings.vl_endpoint
        previous_model = settings.vl_model
        settings.vl_endpoints = [server.endpoint]
        settings.vl_endpoint = server.endpoint
        settings.vl_model = "stub-vl"
        try:
            yield server
        finally:
            settings.vl_endpoints = previous_endpoints
            settings.vl_endpoint = previous_endpoint
            settings.vl_model = previous_model


@pytest.fixture()
def workspace(tmp_path):
    """Isolated input/output roots for one test."""
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    previous = (settings.input_root, settings.output_root,
                settings.artifacts_mode, settings.reconstruct_formats)
    settings.input_root = input_root
    settings.output_root = output_root
    settings.artifacts_mode = True
    settings.reconstruct_formats = ["docx", "pdf"]
    try:
        yield SimpleNamespace(input=input_root, output=output_root, tmp=tmp_path)
    finally:
        (settings.input_root, settings.output_root,
         settings.artifacts_mode, settings.reconstruct_formats) = previous


class SimpleNamespace:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


@pytest.fixture()
def sample_pdf(workspace):
    return fixtures.make_pdf(workspace.input / "report.pdf", pages=3)


@pytest.fixture()
def sample_docx(workspace):
    return fixtures.make_docx(workspace.input / "spec.docx")


@pytest.fixture()
def large_pdf(workspace):
    return fixtures.make_pdf(workspace.input / "big" / "big_report.pdf", pages=40)


@pytest.fixture()
def sample_html(workspace):
    return fixtures.make_html(workspace.input / "note.html")
