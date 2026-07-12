# Offline PDF Batch Pipeline

Air-gapped local pipeline that turns PDFs from allowlisted directories into grounded Markdown and typed entities.

## What It Does

```text
PDF image render (text layer ignored)
-> SwinIR x4 with fallback
-> MinerU full layout
-> PaddleOCR for text blocks
-> Qwen2.5-VL reconstruction, OCR validation, tables and visual blocks
-> Fenic + Qwen3 entity extraction
-> MinIO result bundle
```

PostgreSQL is both the durable state store and job queue. No external broker is used.

## Requirements

- Linux target server with 2x NVIDIA A100 40 GB.
- PostgreSQL and MinIO available locally to the deployment.
- Source directories mounted read-only under configured allowlist roots.
- Imported offline release bundle containing all container images, Python dependencies, models, tokenizers and OCR dictionaries.
- No network access is required or permitted at runtime.

## Operations

Start the persistent controller and workers with the target deployment profile. The exact Compose/systemd assets are added during implementation.

Submit a one-shot batch:

```bash
idp batch submit /data/incoming/contracts /data/incoming/reports
```

Check progress and obtain a complete report:

```bash
idp batch status <batch-id>
idp batch report <batch-id> --format json
```

The controller continues processing if the submitting terminal closes. It recovers interrupted jobs with PostgreSQL leases after a controller or worker restart.

## Outputs

Each technically processed PDF receives an immutable MinIO bundle:

```text
final.md
entities.json
manifest.json
```

`manifest.json` contains source and artifact hashes, model/profile versions, provenance, OCR/VLM findings, fallback decisions, retries and `quality=pass|warning|failed`.

`quality=failed` is still published as a technically completed result. Files that cannot be processed are explicitly reported as `QUARANTINED`; they do not stop the rest of the batch.

## Offline Release Process

1. Build a pinned release bundle on a connected build host.
2. Verify hashes and rehearse installation in a network-disabled environment.
3. Transfer the bundle to the target server.
4. Import and validate it with `idp profile validate <profile>`.
5. Activate only a verified immutable profile; rollback switches to a previously imported profile without internet access.

## Testing

- Local development and standard CI run unit, schema, queue, storage and mocked integration tests without GPUs or full model weights.
- The target server runs resumable real-model smoke and canary/soak suites only for model/runtime/profile promotion.
- Initial quality checks validate provenance, schema, recovery and resource behavior. Accuracy claims wait for manually audited ground truth.
