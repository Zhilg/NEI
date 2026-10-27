"""Single-file web UI: drop a file into the box, press buttons, get artifacts.

A local browser tool for processing one document at a time (PDF/DOCX/XLSX/
PPTX/HTML) without watching directories:

  * drop a file on the box (or pick it with the file input);
  * press "Обработать всё" to run the pipeline on every dropped file at once,
    or "Обработать" to process a single file;
  * press "DOCX" / "PDF" / "TXT" to (re)build that artifact from the bundle;
  * download the Markdown, the bundle text or the reconstructed documents.

Everything is standard library (``http.server``) so the UI needs no extra
packages inside the air-gapped image.

Run (with a real vLLM endpoint):
    idp-web --root data/ui --host 127.0.0.1 --port 18067

Run without a GPU (uses the deterministic VLM stub):
    idp-web --stub --root data/ui --port 18067

Layout under ``--root``::

    uploads/<file_id>/<original name>   dropped files (read-only inputs)
    output/<file_id>/                   worker output for that file
      <name>.md                         reconstructed markdown
      txts/<name>.pdf/text.txt, ...     bundle (input for the format buttons)
      txts/<name>.pdf/reconstruct.docx  reconstructed DOCX (etc.)

Programmatic use (tests): ``FileUIServer(root).start()`` / ``.stop()``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from idp.config import settings
from idp.reconstructor import reconstruct_bundle

logger = logging.getLogger(__name__)

FORMAT_LABELS = {"md": "Markdown", "txt": "TXT (bundle)", "docx": "DOCX", "pdf": "PDF", "vsdx": "VSDX"}
DEFAULT_FORMATS = ["docx", "pdf", "vsdx"]
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IDP — обработка файлов</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 15px/1.45 system-ui, sans-serif; margin: 0; background: #f4f5f7; color: #1c1e21; }
  header { padding: 14px 22px; background: #22334d; color: #fff; font-weight: 600; }
  header span { font-weight: 400; opacity: .75; margin-left: 10px; font-size: 13px; }
  main { max-width: 980px; margin: 22px auto; padding: 0 16px 60px; }
  #drop { border: 2px dashed #8a97ab; border-radius: 12px; padding: 34px; text-align: center;
          background: #fff; transition: .15s border-color, .15s background; cursor: pointer; }
  #drop.hover { border-color: #2f6fe4; background: #eef4ff; }
  #drop p { margin: 6px 0; }
  #drop .hint { color: #66707f; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; margin-top: 18px; background: #fff;
          border-radius: 10px; overflow: hidden; box-shadow: 0 1px 2px rgba(20,30,50,.08); }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #eceff3; font-size: 14px; }
  th { background: #fafbfc; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #66707f; }
  tr:last-child td { border-bottom: none; }
  .name { font-weight: 600; word-break: break-all; }
  .status { font-size: 13px; }
  .status.pending { color: #8a6d1f; }
  .status.running { color: #2f6fe4; }
  .status.done { color: #1c7a3d; }
  .status.error { color: #b3261e; }
  button { font: inherit; border: 1px solid #c3cbd8; background: #fff; border-radius: 7px;
           padding: 5px 11px; margin: 1px 2px; cursor: pointer; }
  button:hover:not(:disabled) { border-color: #2f6fe4; color: #2f6fe4; }
  button.primary { background: #2f6fe4; border-color: #2f6fe4; color: #fff; }
  button.primary:hover:not(:disabled) { background: #2559b8; color: #fff; }
  button:disabled { opacity: .45; cursor: default; }
  a.dl { color: #2f6fe4; text-decoration: none; margin-right: 10px; font-size: 13px; }
  a.dl:hover { text-decoration: underline; }
  .error { color: #b3261e; font-size: 12px; }
  .files { font-size: 13px; color: #3d4652; }
</style>
</head>
<body>
<header>IDP — обработка файлов<span>перетащите файлы в область, нажмите «Обработать всё» или обрабатывайте по отдельности</span></header>
<main>
  <div id="drop">
    <p><strong>Перетащите файлы сюда</strong></p>
    <p class="hint">PDF, DOCX, XLSX, PPTX, HTML, изображения — или нажмите, чтобы выбрать</p>
    <input type="file" id="picker" multiple hidden>
  </div>
  <button id="processAll" class="primary" style="margin: 18px 0; padding: 12px 24px; font-size: 16px;" disabled>Обработать всё</button>
  <p id="processAllHint" class="hint" style="display:none; margin-top:-10px;">Выбросьте файлы или выберите их — нажмите кнопку, чтобы обработать все сразу</p>
  <table>
    <thead><tr>
      <th style="width:34%">Файл</th><th style="width:14%">Статус</th>
      <th style="width:30%">Действия</th><th>Результат</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
</main>
<script>
const state = new Map();
const rows = document.getElementById('rows');
const processAllBtn = document.getElementById('processAll');
const processAllHint = document.getElementById('processAllHint');

function updateProcessAllButton() {
  const pending = Array.from(state.values()).filter(
    f => f.status === 'pending' || f.status === 'error'
  );
  processAllBtn.disabled = !pending.length;
}

document.getElementById('drop').addEventListener('click', () => document.getElementById('picker').click());
document.getElementById('picker').addEventListener('change', (e) => { upload(e.target.files); e.target.value = ''; });

['dragenter', 'dragover'].forEach(ev => document.getElementById('drop').addEventListener(ev, (e) => {
  e.preventDefault(); document.getElementById('drop').classList.add('hover');
}));
['dragleave', 'drop'].forEach(ev => document.getElementById('drop').addEventListener(ev, (e) => {
  e.preventDefault(); document.getElementById('drop').classList.remove('hover');
}));
document.getElementById('drop').addEventListener('drop', (e) => upload(e.dataTransfer.files));

processAllBtn.addEventListener('click', () => {
  fetch('action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'process_all' }),
  }).then(() => { poll(); });
});

async function upload(files) {
  for (const file of files) {
    const body = new FormData();
    body.append('file', file, file.name);
    const response = await fetch('upload', { method: 'POST', body });
    const info = await response.json();
    if (!response.ok) { alert(info.error || 'upload failed'); continue; }
    state.set(info.id, { ...info, status: 'pending', error: '', files: [] });
  }
  render();
  updateProcessAllButton();
  poll();
}

async function act(id, action) {
  await fetch('action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action }),
  });
  poll();
}

async function poll() {
  const response = await fetch('api/status');
  const data = await response.json();
  let changed = false;
  for (const item of data.files) {
    const previous = state.get(item.id);
    if (!previous || JSON.stringify(previous) !== JSON.stringify(item)) {
      state.set(item.id, item);
      changed = true;
    }
  }
  if (changed) render();
  updateProcessAllButton();
  const running = data.files.some(f => f.status === 'running' || f.status === 'pending');
  if (running) setTimeout(poll, 1000);
}

function render() {
  rows.innerHTML = '';
  for (const item of state.values()) {
    const tr = document.createElement('tr');
    const running = item.status === 'running';
    const done = item.status === 'done';

    const name = document.createElement('td');
    name.className = 'name';
    name.textContent = item.name;
    tr.appendChild(name);

    const status = document.createElement('td');
    status.className = 'status ' + item.status;
    status.textContent = { pending: 'в очереди', running: 'обработка…', done: 'готово', error: 'ошибка' }[item.status] || item.status;
    if (item.error) {
      const err = document.createElement('div');
      err.className = 'error';
      err.textContent = item.error;
      status.appendChild(err);
    }
    tr.appendChild(status);

    const actions = document.createElement('td');
    const process = document.createElement('button');
    process.textContent = 'Обработать';
    process.className = 'primary';
    process.disabled = running;
    process.onclick = () => act(item.id, 'process');
    actions.appendChild(process);
    for (const format of ['docx', 'pdf', 'vsdx', 'txt']) {
      const button = document.createElement('button');
      button.textContent = format.toUpperCase();
      button.title = 'пересобрать ' + format.toUpperCase() + ' из bundle';
      button.disabled = running || !item.bundle;
      button.onclick = () => act(item.id, format);
      actions.appendChild(button);
    }
    tr.appendChild(actions);

    const result = document.createElement('td');
    result.className = 'files';
    for (const file of item.files || []) {
      const link = document.createElement('a');
      link.className = 'dl';
      link.href = 'download?id=' + encodeURIComponent(item.id) + '&path=' + encodeURIComponent(file.path);
      link.textContent = file.label;
      link.download = '';
      result.appendChild(link);
    }
    tr.appendChild(result);
    rows.appendChild(tr);
  }
}
</script>
</body>
</html>
"""


class _Upload:
    def __init__(self, filename: str, size: int) -> None:
        self.filename = filename
        self.size = size


class FileUIServer:
    """Background-thread HTTP server that processes dropped files one by one."""

    def __init__(self, root: Path, host: str = "127.0.0.1", port: int = 0) -> None:
        self.root = Path(root)
        self.uploads_dir = self.root / "uploads"
        self.output_dir = self.root / "output"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._server = ThreadingHTTPServer((host, port), self._make_handler())
        def _serve():
            try:
                self._server.serve_forever()
            except Exception:
                logger.exception("HTTP server thread crashed")
        self._thread = threading.Thread(target=_serve, daemon=True)

    # ------------------------------------------------------------------ api

    def start(self) -> str:
        logger.info("Starting HTTP server on %s:%d", self.host, self.port or 0)
        self._thread.start()
        # Give the thread a moment to bind
        import time
        time.sleep(0.1)
        actual_host, actual_port = self._server.server_address[:2]
        logger.info("HTTP server bound to %s:%d", actual_host, actual_port)
        return self.url

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def status(self) -> dict:
        with self._lock:
            return {"files": [dict(job) for job in self._jobs.values()]}

    # ------------------------------------------------------------- actions

    def register_upload(self, filename: str, data: bytes) -> dict:
        file_id = uuid.uuid4().hex[:12]
        safe_name = Path(filename).name or "file"
        upload_dir = self.uploads_dir / file_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / safe_name).write_bytes(data)
        job = {
            "id": file_id,
            "name": safe_name,
            "size": len(data),
            "status": "pending",
            "error": "",
            "bundle": "",
            "files": [],
        }
        with self._lock:
            self._jobs[file_id] = job
        return job

    def _update(self, file_id: str, **fields: object) -> None:
        with self._lock:
            if file_id in self._jobs:
                self._jobs[file_id].update(fields)

    def run_action(self, file_id: str, action: str) -> None:
        """Run an action in a background thread (one job at a time per file)."""
        thread = threading.Thread(target=self._action_worker, args=(file_id, action), daemon=True)
        thread.start()

    def process_all(self) -> None:
        """Process every pending/error file sequentially in a background thread."""
        thread = threading.Thread(target=self._process_all_worker, daemon=True)
        thread.start()

    def _process_all_worker(self) -> None:
        with self._lock:
            target_ids = [
                jid for jid, job in self._jobs.items()
                if job["status"] in ("pending", "error")
            ]
        for file_id in target_ids:
            self._process(file_id)

    def _action_worker(self, file_id: str, action: str) -> None:
        job = self._jobs.get(file_id)
        if job is None:
            return
        if action == "process":
            self._process(file_id)
        elif action in {"docx", "pdf", "vsdx", "txt"}:
            self._rebuild_format(file_id, action)
        else:
            self._update(file_id, status="error", error=f"неизвестное действие: {action}")

    # ---------------------------------------------------------- processing

    def _file_output_dir(self, file_id: str) -> Path:
        return self.output_dir / file_id

    def _bundle_dir(self, file_id: str) -> Path | None:
        output = self._file_output_dir(file_id)
        candidate = next((path for path in sorted(output.rglob("text.txt"))
                          if path.parent.name != "txts" or True), None)
        if candidate is None:
            for path in sorted(output.rglob("text.txt")):
                if (path.parent / "meta.json").is_file():
                    return path.parent
            return None
        return candidate.parent

    def _process(self, file_id: str) -> None:
        from idp.worker import Worker
        import time
        import urllib.request
        import urllib.error

        job = self._jobs.get(file_id)
        if job is None:
            return
        upload_dir = self.uploads_dir / file_id
        source = next(upload_dir.iterdir(), None)
        if source is None or not source.is_file():
            self._update(file_id, status="error", error="файл не найден")
            return
        output_dir = self._file_output_dir(file_id)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        self._update(file_id, status="running", error="", files=[], bundle="")

        # Pre-flight check: verify VLM endpoints are reachable
        vlm_endpoints = getattr(settings, 'vl_endpoints', []) or [getattr(settings, 'vl_endpoint', '')]
        for endpoint in vlm_endpoints:
            if not endpoint:
                continue
            models_url = endpoint.rstrip('/') + '/models'
            for attempt in range(10):
                try:
                    with urllib.request.urlopen(models_url, timeout=5) as resp:
                        if resp.status == 200:
                            logger.info("VLM endpoint ready: %s", endpoint)
                            break
                except Exception as e:
                    logger.warning("VLM endpoint not ready (attempt %d/10): %s - %s", attempt + 1, models_url, e)
                    time.sleep(2.0)
            else:
                logger.error("VLM endpoint never became ready: %s", endpoint)

        saved = (settings.input_root, settings.output_root,
                 settings.artifacts_mode, settings.reconstruct_formats)
        try:
            settings.input_root = upload_dir
            settings.output_root = output_dir
            settings.artifacts_mode = True
            settings.reconstruct_formats = list(DEFAULT_FORMATS)
            asyncio.run(Worker(artifacts_mode=True, force=True).run())
        except Exception as exc:  # noqa: BLE001 - report any failure in the UI
            logger.exception("Processing failed for %s", source.name)
            self._update(file_id, status="error", error=str(exc)[:500])
            return
        finally:
            (settings.input_root, settings.output_root,
             settings.artifacts_mode, settings.reconstruct_formats) = saved

        bundle = self._bundle_dir(file_id)
        self._update(
            file_id,
            status="done",
            bundle=str(bundle) if bundle else "",
            files=self._collect_files(file_id),
        )

    def _rebuild_format(self, file_id: str, fmt: str) -> None:
        bundle = self._bundle_dir(file_id)
        if bundle is None:
            self._update(file_id, status="error", error="bundle не найден — сначала обработайте файл")
            return
        self._update(file_id, status="running", error="")
        try:
            if fmt == "vsdx":
                written = reconstruct_bundle(bundle, [fmt], content="document")
            else:
                written = reconstruct_bundle(bundle, [fmt])
        except Exception as exc:  # noqa: BLE001
            logger.exception("Reconstruction failed for %s", fmt)
            self._update(file_id, status="error", error=str(exc)[:500])
            return
        if not written:
            self._update(file_id, status="error", error=f"не удалось собрать {fmt.upper()}")
            return
        self._update(file_id, status="done", files=self._collect_files(file_id))

    def _collect_files(self, file_id: str) -> list[dict]:
        output = self._file_output_dir(file_id)
        entries: list[dict] = []
        md = next(iter(sorted(output.glob("*.md"))), None)
        if md is not None:
            entries.append({"label": "Markdown", "path": str(md.relative_to(output))})
        bundle = self._bundle_dir(file_id)
        if bundle is not None:
            text = bundle / "text.txt"
            if text.is_file():
                entries.append({"label": "TXT (bundle)", "path": str(text.relative_to(output))})
            for fmt in ("docx", "pdf", "vsdx"):
                artifact = bundle / f"reconstruct.{fmt}"
                if artifact.is_file():
                    entries.append({"label": fmt.upper(), "path": str(artifact.relative_to(output))})
        return entries

    def resolve_download(self, file_id: str, relative: str) -> Path | None:
        """Resolve a download path inside one job's output (no traversal)."""
        output = self._file_output_dir(file_id)
        candidate = (output / relative).resolve()
        try:
            candidate.relative_to(output.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    # -------------------------------------------------------------- http

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: object) -> None:  # silence
                logger.info("webui: " + fmt, *args)

            # ------------------------------------------------------ helpers

            def _send_json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            # -------------------------------------------------------- routes

            def do_GET(self) -> None:  # noqa: N802
                try:
                    parsed = urlparse(self.path)
                    if parsed.path == "/":
                        body = INDEX_HTML.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if parsed.path == "/health":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(b'{"status":"ok"}')
                        return
                    if parsed.path == "/api/status":
                        self._send_json(server.status())
                        return
                    if parsed.path == "/download":
                        query = parse_qs(parsed.query)
                        file_id = (query.get("id") or [""])[0]
                        relative = (query.get("path") or [""])[0]
                        path = server.resolve_download(file_id, relative)
                        if path is None:
                            self._send_json({"error": "not found"}, status=404)
                            return
                        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                        data = path.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header(
                            "Content-Disposition", f'attachment; filename="{path.name}"'
                        )
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    self._send_json({"error": "not found"}, status=404)
                except Exception as e:
                    logger.exception("GET %s failed", self.path)
                    self._send_json({"error": str(e)}, status=500)

            def do_POST(self) -> None:  # noqa: N802
                try:
                    parsed = urlparse(self.path)
                    if parsed.path == "/upload":
                        self._handle_upload()
                        return
                    if parsed.path == "/action":
                        self._handle_action()
                        return
                    self._send_json({"error": "not found"}, status=404)
                except Exception as e:
                    logger.exception("POST %s failed", self.path)
                    self._send_json({"error": str(e)}, status=500)

            # ----------------------------------------------------- handlers

            def _handle_upload(self) -> None:
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type:
                    self._send_json({"error": "expected multipart/form-data"}, status=400)
                    return
                boundary = ""
                for part in content_type.split(";"):
                    part = part.strip()
                    if part.startswith("boundary="):
                        boundary = part[len("boundary="):].strip('"')
                if not boundary:
                    self._send_json({"error": "missing multipart boundary"}, status=400)
                    return
                body = self._read_body()
                if len(body) > MAX_UPLOAD_BYTES:
                    self._send_json({"error": "file too large"}, status=413)
                    return
                delimiter = b"--" + boundary.encode("utf-8")
                chunks = body.split(delimiter)
                for chunk in chunks:
                    if not chunk or chunk in (b"--\r\n", b"--", b"\r\n"):
                        continue
                    chunk = chunk.lstrip(b"\r\n")
                    if chunk.endswith(b"\r\n"):
                        chunk = chunk[:-2]
                    if b"\r\n\r\n" not in chunk:
                        continue
                    raw_headers, content = chunk.split(b"\r\n\r\n", 1)
                    headers = raw_headers.decode("utf-8", "replace")
                    if "filename=" not in headers:
                        continue
                    filename = ""
                    for header in headers.split("\r\n"):
                        if header.lower().startswith("content-disposition") and "filename=" in header:
                            filename = header.split("filename=", 1)[1].strip().strip('"')
                    if not filename:
                        continue
                    job = server.register_upload(filename, content)
                    self._send_json(job)
                    return
                self._send_json({"error": "no file part"}, status=400)

            def _handle_action(self) -> None:
                try:
                    payload = json.loads(self._read_body().decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._send_json({"error": "bad json"}, status=400)
                    return
                file_id = str(payload.get("id", ""))
                action = str(payload.get("action", ""))
                if action == "process_all":
                    server.process_all()
                    self._send_json({"ok": True, "action": "process_all"})
                    return
                with server._lock:
                    known = file_id in server._jobs
                if not known:
                    self._send_json({"error": "unknown file"}, status=404)
                    return
                server.run_action(file_id, action)
                self._send_json({"ok": True, "id": file_id, "action": action})

        return Handler


def _start_stub() -> StubServer:
    """Start the deterministic VLM stub and point the pipeline at it.

    Lets ``idp-web --stub`` run end-to-end without a GPU/vLLM.  Requires the
    test package on the path (run from the repo root or ``pip install -e .``).
    """
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    tests_root = Path.cwd()
    if str(tests_root) not in sys.path:
        sys.path.insert(0, str(tests_root))
    from tests.vlm_stub import StubServer  # noqa: E402

    stub = StubServer()
    stub.__enter__()
    settings.vl_endpoints = [stub.endpoint]
    settings.vl_endpoint = stub.endpoint
    settings.vl_model = "stub-vl"
    logger.info("VLM stub started at %s", stub.endpoint)
    return stub


def main() -> None:
    parser = argparse.ArgumentParser(description="Single-file web UI for the IDP pipeline")
    parser.add_argument("--root", default="data/ui", help="working directory (uploads + output)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18067)
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Start the deterministic VLM stub (no GPU required)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Starting IDP web UI on %s:%d", args.host, args.port)
    stub: StubServer | None = None
    if args.stub:
        try:
            stub = _start_stub()
        except ImportError as exc:
            logger.error("VLM stub requires the tests package (run from repo root): %s", exc)
            print(f"error: --stub requires running from the repo root: {exc}", file=sys.stderr)
            sys.exit(1)

    server = FileUIServer(Path(args.root), host=args.host, port=args.port)
    url = server.start()
    logger.info("Server started at %s (root: %s)", url, Path(args.root).resolve())
    print(f"IDP web UI on {url}  (root: {Path(args.root).resolve()})")
    if stub:
        print(f"VLM stub active at {stub.endpoint}")
    print("Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        if stub is not None:
            stub.__exit__(None, None, None)


if __name__ == "__main__":
    main()
