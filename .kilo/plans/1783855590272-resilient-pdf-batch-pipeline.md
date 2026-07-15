# Утверждённый план: устойчивый offline PDF batch pipeline

## Цель

Построить локальный, полностью air-gapped batch-пайплайн для рекурсивной обработки PDF из переданных абсолютных директорий. Система должна запускаться один раз командой submit, продолжать работу без терминала пользователя, восстанавливаться после рестарта и публиковать воспроизводимый Markdown с сущностями для каждого технически обработанного файла.

Поддерживаемый вход v1: только PDF. PDF text layer всегда отбрасывается и нигде не извлекается, не сохраняется и не используется.

```text
allowlisted absolute directories
  -> stable recursive PDF scan + SHA-256
  -> PostgreSQL batch/job state + bounded workers
  -> deterministic PDF render to page images
  -> SwinIR x4 + image-quality fallback
  -> MinerU full lossless layout
  -> line OCR inside MinerU text blocks
  -> one Qwen2.5-VL reconstruction/validation job
  -> complete grounded Markdown
  -> Fenic + Qwen3 entity extraction
  -> atomic MinIO result bundle + PostgreSQL report/state
```

## Жёсткие архитектурные решения

- PostgreSQL является единственным источником истины для batch, файлов, заданий, состояний, leases, retries, resource reservations, ошибок, итогового качества и ссылок на artifacts.
- PostgreSQL job table является единственной очередью. Воркеры claim-ят готовые jobs через `FOR UPDATE SKIP LOCKED`, продлевают lease heartbeat и возвращают просроченные jobs в очередь через reaper после сбоя.
- MinIO хранит versioned artifacts и final bundles. PostgreSQL хранит метаданные, hashes, JSON results и object keys, но не бинарные PDF или изображения.
- Нет document classifier, processing profiles по типам документа, packet segmentation, native-text trust, отдельного text-quality сервиса, отдельного table/figure/diagram/formula сервиса, второго VLM или отдельного validation stage.
- MinerU является единственным источником document layout. Qwen-VL является единственным компонентом, который компонуeт документ, интерпретирует нетекстовые блоки, проверяет OCR и выполняет лёгкую внутреннюю логическую проверку в том же reconstruction job.
- Fenic не управляет заданиями и состоянием. Он является harness для schema-driven extraction сущностей из готового Markdown.

## Запуск, входные файлы и состояния

1. Развернуть постоянные controller и workers через systemd или Docker Compose. Они независимы от пользовательского терминала и автоматически поднимаются после reboot.
2. Реализовать `idp batch submit ROOT... [--profile PROFILE] [--priority N]`. Команда принимает только абсолютные пути, создаёт batch и возвращает `batch_id`; она не ждёт обработки документов.
3. Разрешать только пути, чей `realpath` находится внутри allowlist roots, смонтированных controller-у read-only. Не следовать symbolic links. Не изменять и не удалять исходные файлы.
4. Scanner рекурсивно обнаруживает PDF. Перед hash он получает два идентичных `stat`-снимка с настраиваемой паузой. Изменившийся файл получает `SKIPPED_UNSTABLE`; symlink - `SKIPPED_SYMLINK`; неподдерживаемый файл - `SKIPPED_UNSUPPORTED`.
5. Каждый batch хранит неизменяемый scan snapshot. Файлы, созданные или изменённые после scan phase, будут обработаны только следующим batch.
6. До тяжёлой обработки source PDF копируется в temporary MinIO object. Это позволяет продолжить работу, если исходная папка стала недоступна.
7. Terminal item states: `REUSED`, `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `QUARANTINED`, `SKIPPED_UNSUPPORTED`, `SKIPPED_UNSTABLE`, `SKIPPED_SYMLINK`.
8. Terminal batch states: `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `COMPLETED_WITH_ERRORS`, `PAUSED_CAPACITY`, `CANCELLED`.
9. Corrupt/encrypted PDF, PDF bomb, resource-limit breach, неподдерживаемый script или исчерпанные technical retries не останавливают остальной batch. Item получает `QUARANTINED` с кодом причины, последней успешной стадией и diagnostics.

## Ограничения ресурсов и восстановление

1. В `pipeline_profile` закрепить per-stage limits: размер PDF, число страниц, DPI, pixels/page, crop area, maximum artifact bytes/document, queue capacity, worker concurrency, timeout, retry count, minimum MinIO free space, CPU/RAM/VRAM budget.
2. Использовать отдельные bounded pools для scanner/hash/render/MinIO I/O, GPU1 parsing и GPU0 LLM. Controller не создаёт page/block jobs без доступной очереди и resource reservation.
3. GPU1 обслуживает только SwinIR, MinerU и PaddleOCR. Их jobs имеют независимые bounded queues и pinned model/runtime settings.
4. GPU0 обслуживается единым admission scheduler. В каждый момент активна только одна тяжёлая роль: Qwen2.5-VL reconstruction либо Qwen3 Fenic extraction. Scheduler проверяет VRAM до запуска, хранит reservation в PostgreSQL и освобождает orphaned reservation по lease/heartbeat.
5. OOM или падение model worker - operational failure: worker перезапускается, admission/batch size уменьшается по policy, событие фиксируется. Нельзя бесконечно повторять тот же job при том же resource envelope.
6. Перед heavy job резервировать место в object storage. При нехватке места batch переходит в `PAUSED_CAPACITY`; новые heavy jobs не выдаются, уже подготовленные final bundles могут завершить publication.
7. Каждый stage идемпотентен по immutable input artifact hashes и profile hash. Publication выполняется exactly-once через transactionally guarded output pointer и immutable MinIO prefix.

## Модельный pipeline

### 1. Vision-only render

- Render PDF детерминированным renderer-ом в page images. Не читать и не сохранять PDF text layer.
- `render_manifest` хранит page number, original geometry, rotation, DPI, colour mode, image hash и transform координат.
- Ограничить страницы и пиксели до выделения GPU. Несоответствующие limits файлы помещать в quarantine.

### 2. Upscale и image-quality signal

- Каждая render page проходит локальный `SwinIR x4` без GAN.
- Обязательный лёгкий quality gate сравнивает исходное и улучшенное изображение по artifact/clipping/no-reference quality signals и sample OCR preflight. Он не создаёт отдельный ML stage и не использует text layer.
- Если upscale ухудшает страницу, downstream получает исходный render, а `upscale_fallback_reason` записывается в manifest. Original render immutable.
- Render/upscale signals - часть итоговой quality observability, не самостоятельный text-quality classifier.

### 3. MinerU: полный layout без потерь

- Использовать закреплённый offline MinerU `3.4.0`, pipeline backend. Revision, model checksums, runtime digest и all local assets фиксируются в `pipeline_profile`.
- Адаптер превращает MinerU `middle.json` в внутренний versioned `layout_manifest`; vendor JSON нельзя передавать следующим стадиям напрямую.
- `layout_manifest` сохраняет каждый block без исключений: text, title, list, table, image, chart, formula, header, footer, footnote, stamp, signature и любые будущие известные block types, плюс page, bbox, hierarchy, relations, reading order и crop references.
- Текст, OCR spans и Markdown, созданные MinerU, не используются как content source. Из MinerU берётся только полная структура, геометрия и порядок.

### 4. OCR только в текстовых блоках

- OCR выполняется исключительно для MinerU text-bearing blocks. Нетекстовые blocks не теряются и не требуют отдельного recognizer service.
- `PP-OCRv5_server_det` выделяет lines внутри text block crop. Он не создаёт page-level blocks и не меняет MinerU reading order.
- Language/script router выбирает recognizer per line:
  - `eslav_PP-OCRv5_mobile_rec` - primary для русского, украинского, белорусского и английского.
  - `cyrillic_PP-OCRv5_mobile_rec` - для прочей кириллицы.
  - `PP-OCRv6_medium` - для поддерживаемых Latin/CJK scripts.
- OCR manifest хранит raw and normalized text, line/token bbox в coordinates исходной страницы, confidence, script/language, model revision и crop hash.
- Неизвестный script не подменяется вымышленным текстом: он попадает в Qwen-VL context как image block с `unsupported_script` finding.

### 5. Один Qwen-VL reconstruction и validation job

- Использовать закреплённый `Qwen2.5-VL-32B-Instruct` в local 4-bit AWQ/GPTQ configuration через vLLM на GPU0.
- Один reconstruction job получает полный layout manifest, все relevant page images/crops, OCR lines/tokens и coordinate transforms. Для длинных PDF job допускает deterministic page/chunk batching внутри одного logical reconstruction run, без второго validation pipeline.
- В одном structured output Qwen-VL обязан:
  - сохранить полноту и reading order всех MinerU blocks;
  - сверить OCR с изображениями и исправить текст только при visual evidence;
  - расшифровать таблицы;
  - описать и включить в документ изображения, диаграммы, графики, формулы, печати, подписи, headers/footers/footnotes, когда они семантически значимы;
  - собрать единый document-level Markdown;
  - выполнить лёгкую локальную проверку текста и структуры: OCR disagreement, очевидные несогласованности сумм/дат/нумерации/ссылок, пропущенные blocks и нечитаемые regions;
  - вернуть `findings` только с block/page/bbox evidence и uncertainty reasons.
- Output contract: Markdown blocks неизменно ссылаются на `block_id`, page, bbox и evidence. Qwen не имеет права создавать факты без source evidence.
- Logical findings являются quality signals; они не создают новый model call, внешние lookup или автоматический reject.

### 6. Fenic entity extraction

- После готового Markdown запускать Fenic как semantic extraction harness с локальным `Qwen3-14B` 4-bit via vLLM on GPU0.
- Перед реализацией сделать offline integration spike для Fenic с OpenAI-compatible local endpoint, включая required dummy secret/adapter behavior. В runtime не допускаются внешние provider calls.
- Применить versioned Pydantic schema pack: `entities[]` с `type`, `value`, `normalized_value`, `page`, `block_id`, `bbox`, `evidence`, `confidence`. Начальные типы: `person`, `organization`, `date`, `address`, `identifier`, `amount`.
- Entity валидна только если её page/block/bbox существуют, а evidence - exact quote из Markdown. Нарушение записывается как entity finding, но не удаляет технически готовый output.

## Результаты, качество и retention

1. Publisher до commit валидирует Markdown schema, entity schema, artifact hashes, page/block/bbox references и manifest completeness.
2. Atomically публиковать immutable MinIO bundle:
   - `final.md`;
   - `entities.json`;
   - `manifest.json`.
3. `manifest.json` содержит source/output hashes, profile/prompt/schema versions, модели и их checksums, all stage attempts, resource/fallback decisions, OCR coverage/confidence, VLM validation findings, evidence coverage и `quality=pass|warning|failed`.
4. Technical success с любым quality state публикуется. `quality=failed` означает `COMPLETED_WITH_WARNINGS`, а не потерю output. Downstream обязан фильтровать quality явно.
5. После successful publication удалить temporary source copy, page images, crops, intermediate manifests и traces по lifecycle policy. Final bundle остаётся до явного удаления. Исходный PDF в user directory не изменяется.

## Данные PostgreSQL

Создать Alembic schema и typed repositories для:

- `pipeline_profiles`: immutable releases, model/runtime assets/checksums, prompts, schema versions, limits и quality policy.
- `batches`, `batch_roots`, `documents`, `batch_items`: submit request, scan snapshot, canonical `source_sha256`, source dispositions, output and quality state.
- `jobs`, `stage_runs`, `resource_reservations`: dependency graph, queue/lease/heartbeat, attempts, errors, timings, resource measurements and idempotency keys.
- `artifacts`: object key, hash, type, retention and lineage.
- `entity_results`: compact validated entity data and summary for reporting.
- `audit_samples`, `events`/`audit_log`: deterministic audit selection and immutable operational actions.

All state transition, job claim, lease renewal, reservation release and final output pointer updates must be transactional. Model workers report results to repository/controller layer; they do not update item state directly.

## Air-gapped delivery

1. Target server has no internet. Runtime denies external network egress, disables telemetry, disables runtime package/model downloads and uses only local services.
2. Build a connected release environment that creates immutable offline release bundles. It is not production and does not receive production documents.
3. Each release bundle contains pinned source, OCI images by digest, hashed Python wheelhouse, signed OS package snapshot/set, all model/tokenizer/config/dictionary files, profile manifest, SBOM, license inventory, checksums and import/verification scripts.
4. Build host verifies signatures/hashes and rehearses installation in a network-disabled environment before release. Floating versions and `latest` tags are prohibited.
5. Target import verifies every checksum before image/model/package import. Invalid or missing asset blocks activation. Offline flags such as `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are enforced.
6. `idp profile validate PROFILE` verifies release assets, checksums, no-download behavior, disabled egress/telemetry, model endpoint health, database schema, MinIO capacity and GPU/VRAM before a profile becomes active.
7. Upgrade and rollback switch immutable release/profile pointers only. They never overwrite an active release and never require internet access.

## Тестовая стратегия

- **Tier 0, laptop/ordinary CI:** lint, types, unit tests, migrations, schema contracts, state machines, queue/lease/reaper, scanner safety, artifact writer, coordinate transforms, prompts and manifests. No GPU or model weights.
- **Tier 1, container integration:** PostgreSQL, MinIO, controller and mocked model endpoints. Fault-inject retries, worker restart, capacity pause, duplicate publication, cache invalidation and reports. No real models.
- **Tier 2, target smoke:** one small fixed PDF per real component and OCR route. Check render, upscale fallback, full MinerU layout, OCR manifests, Qwen-VL output, Fenic output and publication. Store diagnostics/profile fingerprint. Suite is resumable and bounded.
- **Tier 3, target canary/soak:** immutable small offline test corpus with Russian scans, PDFs with ignored text layers, tables, multi-column documents, figures/charts/formulas, long PDFs, corrupt/encrypted/oversized files and restart scenarios. Run only before model/runtime/profile promotion, not on every source change.
- **Tier 4, production-like batch:** runs after Tier 2/3. Leases and stage manifests make interruption resumable without restarting completed model work.
- No labelled ground truth exists, therefore do not claim accuracy metrics initially. Validate operational correctness, schemas, evidence/provenance coverage, quality findings, resource envelope and recovery. Deterministically select 1% of published items, minimum 20 per batch, for audit and future ground truth.

## Порядок реализации

1. **Eliminate obsolete sources of truth.** Replace `docs/SUMMARY.md`, `docs/architecture.md`, `docs/implementation-plan.md` and `README.md` with this pipeline. `SUMMARY.md` must contain diagrams for data flow, GPU ownership, PostgreSQL jobs/leases, storage artifacts, input/output contracts, terminal states and quality semantics. Delete every obsolete plan in `.kilo/plans` except this approved file; no alternative architecture remains.
2. **Create the foundation.** Add Python workspace, typed config/domain models, Alembic, PostgreSQL, MinIO, controller, metrics and separate local/target deployment profiles.
3. **Implement offline release lifecycle.** Build/export/import/verify/activate/rollback release tooling and `profile validate`; prohibit runtime downloads and external egress.
4. **Implement persistence and scheduler.** Build migrations, state machine, job dependencies, claims, leases, reaper, retries, quarantine, resource reservations, capacity pause, artifacts and atomic publication.
5. **Implement scanner and batch CLI/API.** Add roots allowlist, stable scan, content hashing, limits, source snapshot, cache reuse, cancellation, retry and JSON/CSV reporting.
6. **Implement render/upscale.** Produce vision-only pages, transforms, SwinIR tiled inference, quality fallback and bounded artifact cleanup.
7. **Implement MinerU adapter.** Deploy pinned offline MinerU, convert all `middle.json` blocks losslessly into internal layout manifest and contract-test schema drift/coordinates/reading order.
8. **Implement OCR.** Add within-block line detection, script routing, Russian-first recognizers, evidence manifests and unknown-script handling.
9. **Implement Qwen-VL reconstruction.** Add GPU0 scheduler, versioned prompt/schema, deterministic long-document batching, one logical reconstruction/validation run and final Markdown/finding validation.
10. **Implement Fenic and publisher.** Add Qwen3 local endpoint integration, entity schema/evidence checks, final bundle publication, retention cleanup, quality summaries and audit selection.
11. **Implement observability and tests.** Add stage latency/throughput, queue depth, leases, VRAM, capacity, retries, quarantine, cache and quality metrics; complete Tier 0-3 tests and run target operational acceptance.

## Acceptance criteria

- No obsolete design document or plan remains; repository documentation describes only this pipeline.
- The queue, status, retries and recovery function with PostgreSQL alone; no external broker is deployed or configured.
- Submit handles allowed absolute roots, recursive scans, symlink rejection, file changes, duplicates and terminal reports correctly.
- Restarting controller or any worker at every stage recovers without lost jobs, duplicate final bundles or invalid resource reservations.
- All MinerU block types, reading order, geometry and relations are represented in internal layout manifest and available to Qwen-VL.
- Russian documents use the East-Slavic OCR path; no unsupported text is silently presented as confident content.
- A single logical Qwen-VL reconstruction run produces Markdown that contains every semantically relevant block, table/image/chart/formula interpretation, OCR corrections with evidence and lightweight validation findings.
- Final entities reference valid Markdown evidence and source geometry.
- Final bundles are atomically published with pass/warning/failed quality signals; technical failures are quarantined without blocking independent documents.
- A target server imports and validates an offline release without internet access or downloads. Local CI completes Tier 0/1 without GPU weights; target model/profile promotion has recorded Tier 2/3 evidence.

## Out of scope for v1

- Filesystem watcher/daemon ingestion after a one-shot batch scan.
- Non-PDF input conversion.
- User portal, human review workflow, external record validation and automatic retraining.
- Accuracy/calibration claims before manual audit produces labelled ground truth.
