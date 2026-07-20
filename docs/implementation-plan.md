# Implementation Plan: Offline PDF Batch Pipeline

The approved detailed plan is maintained at `.kilo/plans/1783855590272-resilient-pdf-batch-pipeline.md`. This document is the repository-facing execution sequence.

## Phase 1: Documentation and project boundary

1. Keep `docs/SUMMARY.md`, `docs/architecture.md`, this file and `README.md` aligned with the approved PDF-only pipeline.
2. Remove all obsolete architecture plans and documentation that describe an external broker, native PDF text, classification, standalone text-quality, independent table/figure processing or dual extraction.
3. Create the Python workspace, typed configuration, domain models, formatting/type/test commands and CI baseline.

**Done when:** repository documentation has one architecture only and local checks need neither GPUs nor model weights.

## Phase 2: Durable operational foundation

1. Provision PostgreSQL, MinIO, controller, worker process and metrics in local and target deployment profiles.
2. Add Alembic migrations for batch, document, job, lease, artifact, resource reservation, entity result and audit records.
3. Implement `SKIP LOCKED` claims, dependency scheduling, leases, worker heartbeats, reaper recovery, retries, quarantine, cancellation and storage-capacity pause.
4. Implement immutable artifact storage, SHA-256 verification, lineage and exactly-once final publication.

**Done when:** worker/controller crashes recover without lost jobs, duplicate final output or leaked reservations.

## Phase 3: Compose deployment

1. Run the pipeline through one Docker Compose stack with mounted source code, models, local OCR/MinerU tools, input folders and persistent data.
2. Build a portable application image on Windows, then use Compose health checks and smoke tests instead of release activation or signatures.
3. Enforce disabled egress, telemetry and runtime downloads on the target server.

**Done when:** target installation, validation, activation and rollback work without internet access.

## Phase 4: Batch discovery and reporting

1. Implement `idp batch submit`, `status`, `report`, `retry` and `cancel`.
2. Enforce allowlisted absolute roots, read-only access, `realpath` validation and no symlink traversal.
3. Implement stable-file checks, SHA-256 identity, supported-file dispositions, limits, immutable scan snapshots and strict versioned result reuse.
4. Produce JSON/CSV reports with every discovered path, state, final bundle key or exact quarantine reason.

**Done when:** an invalid, duplicate, changing, corrupt or encrypted PDF cannot block independent items or silently disappear.

## Phase 5: Vision preparation and full layout

1. Implement vision-only PDF rendering and coordinate manifests; never extract or persist a PDF text layer.
2. Integrate tiled SwinIR x4 plus quality gate and original-image fallback.
3. Deploy pinned offline MinerU 3.4.0 and normalize `middle.json` into lossless internal layout manifests.
4. Contract-test all block types, geometry transforms, hierarchy, relations and reading order.

**Done when:** every MinerU block remains available with source geometry and crop lineage.

## Phase 6: OCR and reconstruction

1. Add line detection only within MinerU text-bearing blocks.
2. Add Russian-first line routing: East-Slavic PP-OCRv5, generic Cyrillic PP-OCRv5 and Latin/CJK PP-OCRv6.
3. Persist OCR provenance and unsupported-script findings.
4. Implement GPU0 admission scheduler and Qwen2.5-VL-32B structured reconstruction.
5. Require one logical Qwen-VL run to validate OCR, transcribe tables, interpret non-text blocks, assemble Markdown and report lightweight evidence-backed validation findings.

**Done when:** the result is one grounded Markdown document with full block coverage and no unreferenced OCR/VLM fact.

## Phase 7: Entities and publication

1. Validate Fenic against local Qwen3-14B/vLLM in an offline integration spike.
2. Implement the versioned Pydantic entity schema, evidence validation and result persistence.
3. Atomically publish `final.md`, `entities.json` and `manifest.json`.
4. Apply retention cleanup only after durable publication and create deterministic audit samples.

**Done when:** every entity references exact Markdown evidence and valid document geometry.

## Phase 8: Observability and acceptance

1. Add metrics and alerts for queue depth, lease recovery, retries, quarantine, stage duration, VRAM, capacity, cache reuse and quality distribution.
2. Run local Tier 0 unit/schema/state tests and Tier 1 mocked integration tests without GPUs or weights.
3. Run target Tier 2 real-model smoke tests and Tier 3 resumable canary/soak tests before profile promotion.
4. Keep all initial quality claims operational until audit produces labelled ground truth.

**Done when:** an air-gapped target can promote a verified profile with recorded smoke/canary evidence, while normal CI remains lightweight.
