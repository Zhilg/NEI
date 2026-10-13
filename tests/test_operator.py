"""Operator client tests: interactive review, force mode, correction saving."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from idp.config import settings
from idp.operator_client import OperatorClient
from tests import fixtures


@pytest.fixture()
def operator_env(workspace, stub_server, monkeypatch):
    corrections = workspace.tmp / "operator_corrections.jsonl"
    previous = settings.operator_corrections_path
    settings.operator_corrections_path = corrections
    drafts = workspace.tmp / "drafts"
    monkeypatch.setattr(
        OperatorClient,
        "_launch_editor",
        staticmethod(lambda path: path.write_text(
            path.read_text(encoding="utf-8") + "\n\nИСПРАВЛЕНО ОПЕРАТОРОМ\n", encoding="utf-8"
        )),
    )
    yield SimpleNamespace(corrections=corrections, drafts=drafts, workspace=workspace)
    settings.operator_corrections_path = previous


class SimpleNamespace:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def _feed(monkeypatch, answers: list[str]) -> None:
    queue = list(answers)

    def fake_input(prompt: str = "") -> str:
        if not queue:
            raise EOFError("no more scripted answers")
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def _run(client: OperatorClient) -> None:
    async def _main() -> None:
        try:
            await client.run()
        finally:
            from idp.vlm_client import close_vlm_client

            await close_vlm_client()

    asyncio.run(_main())


def _corrections(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_accept_saves_correction(operator_env, workspace, monkeypatch):
    fixtures.make_pdf(workspace.input / "page.pdf", pages=2)
    _feed(monkeypatch, ["a", "a"])  # accept both pages
    client = OperatorClient(input_path=workspace.input / "page.pdf", drafts_dir=operator_env.drafts)
    _run(client)

    records = _corrections(operator_env.corrections)
    assert len(records) == 2
    assert records[0]["markdown"].strip(), "VLM markdown saved as correction"
    markers = list(operator_env.drafts.glob("*.reviewed"))
    assert len(markers) == 2


def test_reviewed_pages_are_skipped(operator_env, workspace, monkeypatch):
    fixtures.make_pdf(workspace.input / "page.pdf", pages=1)
    _feed(monkeypatch, ["a"])
    _run(OperatorClient(input_path=workspace.input / "page.pdf", drafts_dir=operator_env.drafts))

    _feed(monkeypatch, ["a"])
    client = OperatorClient(input_path=workspace.input / "page.pdf", drafts_dir=operator_env.drafts)
    _run(client)
    assert len(_corrections(operator_env.corrections)) == 1, "no duplicate correction"


def test_edit_then_accept_saves_edited_markdown(operator_env, workspace, monkeypatch):
    fixtures.make_pdf(workspace.input / "page.pdf", pages=1)
    _feed(monkeypatch, ["e", "a"])  # edit the draft, then accept
    _run(OperatorClient(input_path=workspace.input / "page.pdf", drafts_dir=operator_env.drafts))

    records = _corrections(operator_env.corrections)
    assert len(records) == 1
    assert "ИСПРАВЛЕНО ОПЕРАТОРОМ" in records[0]["markdown"]


def test_force_mode_saves_new_edits(operator_env, workspace, monkeypatch):
    """--force must re-review AND persist the operator's new corrections."""
    pdf = fixtures.make_pdf(workspace.input / "page.pdf", pages=1)

    _feed(monkeypatch, ["e", "a"])
    _run(OperatorClient(input_path=pdf, drafts_dir=operator_env.drafts))
    first = _corrections(operator_env.corrections)
    assert len(first) == 1 and "ИСПРАВЛЕНО ОПЕРАТОРОМ" in first[0]["markdown"]

    # force re-review with a different edit
    _feed(monkeypatch, ["e", "a"])
    client = OperatorClient(input_path=pdf, drafts_dir=operator_env.drafts, force=True)
    _run(client)

    second = _corrections(operator_env.corrections)
    assert len(second) == 2, "force mode must append the new correction"
    assert "ИСПРАВЛЕНО ОПЕРАТОРОМ" in second[-1]["markdown"]


def _markdown_calls(stub_server) -> int:
    return sum(1 for request in stub_server.state.requests if request["kind"] == "markdown")


def test_force_mode_keeps_existing_edits(operator_env, workspace, monkeypatch, stub_server):
    """Accepting in --force mode must not silently replace an edited draft
    with a fresh VLM run (that used to lose the operator's changes)."""
    pdf = fixtures.make_pdf(workspace.input / "page.pdf", pages=1)

    _feed(monkeypatch, ["e", "a"])
    _run(OperatorClient(input_path=pdf, drafts_dir=operator_env.drafts))
    first = _corrections(operator_env.corrections)
    assert "ИСПРАВЛЕНО ОПЕРАТОРОМ" in first[0]["markdown"]

    before = _markdown_calls(stub_server)
    _feed(monkeypatch, ["a"])  # re-review with --force, accept without editing
    _run(OperatorClient(input_path=pdf, drafts_dir=operator_env.drafts, force=True))

    assert _markdown_calls(stub_server) == before, "existing draft must not be re-generated"
    second = _corrections(operator_env.corrections)
    assert len(second) == 2
    assert "ИСПРАВЛЕНО ОПЕРАТОРОМ" in second[-1]["markdown"], "edited content survived force review"


def test_new_vlm_output_option(operator_env, workspace, monkeypatch, stub_server):
    pdf = fixtures.make_pdf(workspace.input / "page.pdf", pages=1)
    _feed(monkeypatch, ["a"])
    _run(OperatorClient(input_path=pdf, drafts_dir=operator_env.drafts))

    before = _markdown_calls(stub_server)
    _feed(monkeypatch, ["n", "a"])  # [n] regenerates the draft from the VLM
    _run(OperatorClient(input_path=pdf, drafts_dir=operator_env.drafts, force=True))
    assert _markdown_calls(stub_server) == before + 1


def test_whitespace_only_answer_is_handled(operator_env, workspace, monkeypatch):
    """A whitespace/empty answer must not crash the prompt loop."""
    fixtures.make_pdf(workspace.input / "page.pdf", pages=1)
    _feed(monkeypatch, [" ", "a"])
    _run(OperatorClient(input_path=workspace.input / "page.pdf", drafts_dir=operator_env.drafts))
    assert len(_corrections(operator_env.corrections)) == 1


def test_eof_quits_gracefully(operator_env, workspace, monkeypatch):
    fixtures.make_pdf(workspace.input / "page.pdf", pages=1)
    _feed(monkeypatch, [])

    def raise_eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    _run(OperatorClient(input_path=workspace.input / "page.pdf", drafts_dir=operator_env.drafts))
    assert _corrections(operator_env.corrections) == []
