"""Web UI tests: upload a file, process it, rebuild formats, download."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from idp.config import settings
from idp.webui import FileUIServer
from tests.vlm_stub import hash_bytes


@pytest.fixture()
def ui(workspace, stub_server):
    server = FileUIServer(workspace.tmp / "ui")
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def client(ui):
    return httpx.Client(base_url=ui.url, timeout=60.0)


def _wait_done(client: httpx.Client, file_id: str, timeout: float = 90.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get("/api/status").json()
        for item in status["files"]:
            if item["id"] == file_id and item["status"] in {"done", "error"}:
                return item
        time.sleep(0.2)
    raise AssertionError(f"job {file_id} did not finish in time")


def _upload(client: httpx.Client, path: Path) -> str:
    response = client.post(
        "/upload",
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_index_page(ui, client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Перетащите файлы сюда" in response.text
    assert "Обработать" in response.text
    assert "Обработать всё" in response.text


def _wait_done_multi(client: httpx.Client, file_ids: set[str], timeout: float = 90.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get("/api/status").json()
        remaining = {
            f["id"] for f in status["files"]
            if f["id"] in file_ids and f["status"] not in {"done", "error"}
        }
        if not remaining:
            return {f["id"]: f for f in status["files"] if f["id"] in file_ids}
        time.sleep(0.2)
    raise AssertionError(f"jobs did not finish in time: {remaining}")


def test_process_all_button_processes_every_file(workspace, ui, client, sample_pdf, sample_docx):
    id_pdf = _upload(client, sample_pdf)
    id_docx = _upload(client, sample_docx)

    # both files are pending after upload
    status = client.get("/api/status").json()
    assert len(status["files"]) == 2
    assert all(f["status"] == "pending" for f in status["files"])

    # trigger process_all (no file id needed)
    response = client.post("/action", json={"action": "process_all"})
    assert response.status_code == 200
    assert response.json()["ok"]

    results = _wait_done_multi(client, {id_pdf, id_docx})
    for file_id, item in results.items():
        assert item["status"] == "done", f"{file_id}: {item}"
        assert item["error"] == ""
        assert item["bundle"]


def test_process_all_only_processes_pending_and_error(ui, client, sample_pdf):
    """process_all skips files that are already 'done'."""
    file_id = _upload(client, sample_pdf)
    client.post("/action", json={"id": file_id, "action": "process"})
    item = _wait_done(client, file_id)
    assert item["status"] == "done"

    # process_all should be a no-op (no pending/error files) and not error
    response = client.post("/action", json={"action": "process_all"})
    assert response.status_code == 200
    assert response.json()["ok"]


def test_stub_flag_starts_stub():
    """The --stub flag auto-starts the VLM stub and wires settings."""
    from idp.webui import _start_stub

    saved = (settings.vl_endpoints, settings.vl_endpoint, settings.vl_model)
    try:
        stub = _start_stub()
        assert settings.vl_endpoints == [stub.endpoint]
        assert settings.vl_endpoint == stub.endpoint
        assert settings.vl_model == "stub-vl"

        # the stub actually responds
        resp = httpx.get(f"{stub.endpoint}/models", timeout=10.0)
        assert resp.status_code == 200
        assert resp.json()["data"][0]["id"] == "stub-vl"
        stub.__exit__(None, None, None)
    finally:
        (settings.vl_endpoints, settings.vl_endpoint, settings.vl_model) = saved


def test_upload_process_and_download(workspace, ui, client, sample_pdf):
    file_id = _upload(client, sample_pdf)
    job = client.post("/action", json={"id": file_id, "action": "process"}).json()
    assert job["ok"]

    item = _wait_done(client, file_id)
    assert item["status"] == "done", item
    assert item["error"] == ""
    assert item["bundle"]

    labels = {entry["label"] for entry in item["files"]}
    assert {"Markdown", "TXT (bundle)", "DOCX", "PDF"} <= labels, item["files"]

    # every page heading survived into the reconstructed PDF
    rendered = sorted((workspace.output / "rendered" / "report").glob("*.png"))
    pdf_entry = next(entry for entry in item["files"] if entry["label"] == "PDF")
    pdf_bytes = client.get(
        "/download", params={"id": file_id, "path": pdf_entry["path"]}
    ).content
    assert b"%PDF" in pdf_bytes[:1024]
    import fitz

    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pdf_text = "".join(page.get_text() for page in document)
    finally:
        document.close()
    for png in rendered:
        assert f"Страница {hash_bytes(png.read_bytes())}" in pdf_text

    md_entry = next(entry for entry in item["files"] if entry["label"] == "Markdown")
    markdown = client.get("/download", params={"id": file_id, "path": md_entry["path"]}).text
    assert "## Страница" in markdown


def test_format_buttons_rebuild_artifacts(workspace, ui, client, sample_pdf):
    file_id = _upload(client, sample_pdf)
    client.post("/action", json={"id": file_id, "action": "process"})
    item = _wait_done(client, file_id)
    assert item["status"] == "done"

    bundle = Path(item["bundle"])
    # remove both reconstructions and rebuild them one at a time
    (bundle / "reconstruct.docx").unlink()
    (bundle / "reconstruct.pdf").unlink()

    client.post("/action", json={"id": file_id, "action": "docx"})
    docx_item = _wait_done(client, file_id)
    assert docx_item["status"] == "done"
    assert (bundle / "reconstruct.docx").is_file()
    assert not (bundle / "reconstruct.pdf").is_file()

    client.post("/action", json={"id": file_id, "action": "pdf"})
    pdf_item = _wait_done(client, file_id)
    assert pdf_item["status"] == "done"
    assert (bundle / "reconstruct.pdf").is_file()


def test_rebuild_without_bundle_reports_error(workspace, ui, client, sample_pdf):
    file_id = _upload(client, sample_pdf)
    client.post("/action", json={"id": file_id, "action": "pdf"})
    item = _wait_done(client, file_id)
    assert item["status"] == "error"
    assert item["error"]


def test_download_rejects_traversal(workspace, ui, client, sample_pdf):
    file_id = _upload(client, sample_pdf)
    response = client.get(
        "/download", params={"id": file_id, "path": "../../../../etc/passwd"}
    )
    assert response.status_code == 404


def test_status_lists_uploaded_files(workspace, ui, client, sample_pdf):
    file_id = _upload(client, sample_pdf)
    status = client.get("/api/status").json()
    assert len(status["files"]) == 1
    assert status["files"][0]["id"] == file_id
    assert status["files"][0]["name"] == "report.pdf"


def test_unknown_action(workspace, ui, client, sample_pdf):
    file_id = _upload(client, sample_pdf)
    client.post("/action", json={"id": file_id, "action": "nope"})
    item = _wait_done(client, file_id)
    assert item["status"] == "error"
