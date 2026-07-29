# Simplified VLM Pipeline Plan

## Goal
Remove all non-essential components (PostgreSQL, MinIO, MinerU, PaddleOCR, SwinIR, Controller, Operator, Fenic) and keep only the minimal pipeline: **PDF → VLM → Markdown → LLM → entities**, plus **DOCX → Markdown** via `mammoth`.

## Architecture
3 services in Docker Compose: `vllm-vl` (VL model), `vllm-llm` (entity LLM), `worker` (Python code, bind-mounted).

## Removed
- PostgreSQL, Alembic, MinIO, Controller, Operator, all tests
- MinerU, PaddleOCR, SwinIR, Fenic entity extractor
- SHA-256 verification, versioning, checksums

## Pipeline
1. **PDF**: PyMuPDF renders pages to PNG (text layer never read)
2. **DOCX**: `mammoth` converts to Markdown
3. **VLM**: Local vLLM VL model receives page images in chunks, returns `reconstructed.md`
4. **LLM**: Separate vLLM LLM extracts entities as JSON
5. **Output**: `reconstructed.md` + append-only `entities.jsonl` + `stats.jsonl`

## Storage
Filesystem only via bind-mounts:
- `/input` (read-only)
- `/output` (`*.md`, `entities.jsonl`, `stats.jsonl`)
- `/workspace` (code, read-only)
Idempotency: skip if `output/<stem>.md` exists.

## Deploy
- Dockerfile: `python:3.12-slim`, `pip install` from `pyproject.toml` at build time, code bind-mounted
- Compose: 3 services, simple shared network, no `internal: true`
- Export/import scripts: no `.whl` downloads, no SHA-256 checks

## CLI
Worker prints progress bar (`tqdm`) for queue and per-file stages (render, VLM, LLM, save).

## Config
Minimal `pyproject.toml`: remove `alembic`, `minio`, `prometheus-client`, `psycopg`, `sqlalchemy`, `typer`, `pytest`, `mypy`, `ruff`, `huggingface-hub`. Add `mammoth`, `tqdm`, `httpx`. Entrypoint: `idp = "idp.worker:main"`.

## Models
Local vLLM with hardcoded model. RTX 5070 12GB requires distilled/quantized/context-truncated models.

## Risks
- VLM Markdown quality depends on prompt/model iteration
- VL context window limits large docs → page chunking with reading order
- Entity quality without bbox/block grounding → prompt for page + evidence quotes
- VRAM usage → limit images per request or use smaller models

## Validation
1. `docker compose up` with real vLLM services
2. Test PDF → verify `.md` with tables/image descriptions
3. Test DOCX → verify `.md` via mammoth
4. Verify `entities.jsonl` populated
5. Verify `stats.jsonl` has timing/type/size records
6. Re-run → verify already-processed files skipped
