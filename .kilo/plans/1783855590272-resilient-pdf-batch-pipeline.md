# Утверждённый план: устойчивый batch-пайплайн PDF

## Контекст и итоговое решение

Заменить прежнюю концепцию из 18 микросервисов, Kafka, native PDF text layer и dual-path extraction на один управляемый локальный batch-пайплайн для PDF. Приоритет: воспроизводимость, контролируемые ресурсы, восстановление после сбоев и отсутствие тихой потери документов.

Целевой поток:

```text
absolute directory roots
  -> recursive PDF discovery
  -> PostgreSQL batch + immutable file snapshot
  -> PDF render to page images (discard PDF text layer)
  -> SwinIR x4 upscale + quality gate
  -> MinerU layout/reading order/block geometry only
  -> line detection + language-routed PaddleOCR
  -> Qwen2.5-VL-32B grounded Markdown reconstruction
  -> Fenic + Qwen3-14B typed entity extraction
  -> atomic MinIO result bundle + PostgreSQL terminal status/report
```

Первый поддерживаемый входной формат: только PDF. DOCX, изображения, HTML, email и прочие форматы не преобразовываются неявно; scanner учитывает их в отчёте как `SKIPPED_UNSUPPORTED`.

## Зафиксированные решения

### Air-gapped delivery и разделение сред

- Целевой сервер полностью изолирован от интернета. На нём запрещены package/model/image downloads, telemetry и любые внешние LLM/OCR API. Runtime containers запускаются с deny-by-default network policy; допустимы только необходимые локальные связи между controller, PostgreSQL, MinIO и model services.
- Разработка разделена на две среды. Lightweight developer/CI environment не требует A100, полного набора весов или full pipeline: там выполняются deterministic unit, schema, migration, queue, storage, contract и mocked-model tests. Target GPU environment выполняет реальные model/integration/soak проверки и не считается обязательной средой для каждого изменения.
- Создать connected build/release environment как единственное место с интернетом. Оно создаёт versioned offline release bundle, но не является production runtime и не хранит production data.
- Offline release bundle содержит locked source distribution, OCI images by immutable digest, Python wheelhouse with hashes, OS package repository/snapshot or signed package set, model/tokenizer/config/dictionary assets, model profile, SBOM, license inventory, checksums manifest, import/verification scripts и signed release manifest.
- На connected build host bundle собирается только из pinned revisions. После скачивания выполняются checksum/signature verification, vulnerability/license review и offline installation rehearsal в network-disabled container/VM. Теги `latest` и floating dependency ranges запрещены.
- Transfer на target производится через контролируемый носитель или approved internal offline channel. Target importer verifies release manifest and every SHA-256 before import; несовпадение блокирует deployment. Imported artifacts получают release ID и не изменяются in place.
- `idp profile validate` на target дополнительно проверяет отсутствие internet egress, наличие всех локальных files/images, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` и эквивалентные offline flags, disabled telemetry, model startup, GPU/VRAM, PostgreSQL/MinIO и free-storage limits. Неуспешная проверка не активирует profile.
- Upgrade и rollback выполняются только переключением immutable release/profile pointer после target validation; откат не требует интернета и не перезаписывает ранее импортированный bundle.

### Многоуровневая стратегия тестирования

- Tier 0, на рабочей машине и обычном CI: formatting, lint, type checks, unit tests, Pydantic/JSON schema validation, Alembic migration tests, PostgreSQL queue/lease/recovery tests, MinIO adapter tests, scanner/path safety tests, coordinate-transform tests, prompts/manifest contract tests. GPU, реальные веса и внешняя сеть не требуются.
- Tier 1, containerized integration without real models: controller, PostgreSQL, MinIO, mocked model endpoints and fault injection. Проверяются bounded queues, idempotency, retry/quarantine, atomic publication, cache invalidation, disk capacity pause, controller/worker restart and terminal reports.
- Tier 2, target hardware smoke suite: по одному проверенному PDF на render, upscale, MinerU, each OCR route, Qwen-VL, Fenic/Qwen3 and final publication. Suite должна быть resumable per stage, сохранять diagnostics and environment fingerprints, и завершаться в фиксированный bounded runtime; это обязательная проверка imported profile.
- Tier 3, target hardware canary/soak suite: небольшой неизменяемый corpus, отдельно хранимый в offline release/test bundle, покрывающий русские scans, digital PDFs with ignored text layer, tables, multi-column pages, formulas/images, long PDFs, corrupt/encrypted/oversized inputs, retries and restart recovery. Запускается перед активацией нового profile и по явному release decision, не на каждый commit.
- Tier 4, production-like batch run: выполняется только после Tier 2/3; controller сохраняет stage manifests and resumable state, поэтому прерванные длительные проверки не начинаются заново и не требуют повторного полного model execution.
- Так как labelled ground truth отсутствует, Tier 2/3 не заявляют OCR/Markdown/entity accuracy. Они проверяют operational correctness, schemas, provenance/evidence coverage, determinism bounds, resource envelopes and absence of silent failure. Sampled audit results создают первый набор для будущих accuracy gates.
- CI обязан разделять изменения: changes to queue/storage/docs/config run Tier 0/1; model/runtime/profile changes additionally require a recorded target Tier 2/3 evidence bundle before promotion. Отсутствие доступа к target hardware блокирует promotion profile, но не локальную разработку unrelated code.

### Режим работы и безопасность доступа

- Развёртывается постоянный локальный controller/worker service через Docker Compose или systemd. Он переживает выход пользователя, рестарт процесса и reboot хоста.
- CLI `idp batch submit <absolute-root>...` только валидирует вход, создаёт batch и возвращает `batch_id`. Режим `idp batch run --wait` используется только для CI и оператора.
- Controller получает исходные каталоги через read-only mounts и принимает только реальные абсолютные пути внутри конфигурационного allowlist. Путь нормализуется через `realpath` до регистрации batch.
- Scanner рекурсивный, не следует symbolic links, не выходит за заявленный root и не изменяет исходные файлы.
- До постановки файла в очередь scanner получает два идентичных `stat`-снимка с настраиваемой паузой, затем рассчитывает SHA-256. Файл, изменившийся во время сканирования или хеширования, попадает в `SKIPPED_UNSTABLE` и будет обнаружен в следующем batch.
- Каждый batch создаёт снимок найденных файлов. Файлы, появившиеся после scan phase, не добавляются в уже созданный batch.

### Хранилища и очередь

- PostgreSQL является единственным источником истины для batch, file identity, стадий, попыток, lease, heartbeat, конфигураций, указателей на артефакты, итоговых статусов и отчётов.
- PostgreSQL job table заменяет Kafka. Воркеры берут готовые работы транзакционно через `FOR UPDATE SKIP LOCKED`; каждая работа имеет `lease_owner`, `lease_expires_at` и heartbeat. Просроченные leases reaper возвращает в очередь после падения процесса.
- Локальный MinIO хранит только versioned artifacts и final result bundles. Нельзя хранить исходные PDF или изображения как BLOB в PostgreSQL.
- Исходный PDF копируется в защищённый temporary object до запуска тяжёлых стадий, поэтому обработка не зависит от доступности исходной папки после scan phase.
- После успешной атомарной публикации final bundle копия PDF и все temporary artifacts удаляются по lifecycle policy. Оригинальный PDF в исходной папке никогда не удаляется или изменяется.
- Final bundle хранится до явного удаления: `final.md`, `entities.json`, `manifest.json`; PostgreSQL хранит compact summary и object keys.

### Ресурсы, параллелизм и admission control

- Все очереди bounded. Controller не создаёт неограниченное число page/block jobs и не загружает объекты заранее без зарезервированной квоты.
- GPU1 выделен только для `upscale -> MinerU -> PaddleOCR`. Для каждого stage задаются worker count, GPU VRAM budget, per-job timeout, max batch size, queue capacity и retry policy в model profile.
- GPU0 управляется единым scheduler. На GPU0 одновременно активна только одна тяжёлая модельная роль: Qwen2.5-VL-32B Markdown reconstruction или Qwen3-14B Fenic extraction. Scheduler не допускает задачу без измеренного свободного VRAM и не допускает одновременно две большие модели.
- Scheduler получает heartbeats модельных workers, снимает orphaned reservations и перезапускает worker после OOM/процессного сбоя. OOM не считается повторяемой document error: он уменьшает admission/batch size и фиксируется как operational incident.
- CPU rendering, file hashing, MinIO I/O и PostgreSQL I/O выполняются отдельными bounded pools, чтобы page rendering не блокировал controller и GPU workers.
- Перед постановкой работы резервируются лимиты на disk/object-storage quota. При недостатке свободного места batch переводится в `PAUSED_CAPACITY`, новые тяжёлые jobs не выдаются, а завершённые работы могут безопасно опубликовать результат.

### Предохранители и terminal states

- Model profile задаёт limits: размер PDF, число страниц, максимальные pixels/page после render и upscale, максимальную площадь crop, максимальный итоговый объём artifacts/document, timeout каждой стадии, max attempts и минимальный свободный объём MinIO.
- PDF bomb, encrypted/password-protected PDF, corrupt render, неподдерживаемый script, нарушение resource limit и исчерпанные технические retries не прерывают batch. Item получает `QUARANTINED` с классифицированной причиной, последней успешной стадией и безопасными артефактами для диагностики.
- Terminal batch states: `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `COMPLETED_WITH_ERRORS`, `PAUSED_CAPACITY`, `CANCELLED`.
- Terminal item states: `REUSED`, `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `QUARANTINED`, `SKIPPED_UNSUPPORTED`, `SKIPPED_UNSTABLE`, `SKIPPED_SYMLINK`.
- `COMPLETED` означает `quality=pass`. `COMPLETED_WITH_WARNINGS` публикуется и при `quality=warning`, и при `quality=failed`. Только технически невозможные файлы остаются `QUARANTINED` без выдуманного output.

### Versioned cache и воспроизводимость

- Уникальность документа задаётся `source_sha256`, не путём к файлу. `batch_item` хранит путь, root, размер, mtime, device/inode при наличии и ссылку на canonical document.
- Reuse разрешён только при полном совпадении `source_sha256`, pipeline version, model-profile hash, entity schema-pack version и quality-policy version. Новый batch item получает статус `REUSED` и ссылку на существующий final bundle.
- Любая смена кода пайплайна, моделей, весов, quantization, runtime image, prompt, entity schema или quality thresholds инвалидирует reuse.
- Каждый artifact и final manifest содержат SHA-256, producing stage, parent artifacts, pipeline version, model profile/revision/checksum, prompt version и creation time.

## ML pipeline v1

### 1. Render

- Принимать PDF, не извлекать и не использовать его text layer ни на одной стадии.
- PyMuPDF или эквивалентный deterministic renderer создаёт page images в зафиксированном colour mode и DPI. Render manifest хранит original page geometry, rotation, DPI и coordinate transform.
- Преобразования координат между original page, rendered page, upscaled page, crop и Markdown citation должны быть явно сохранены и протестированы.

### 2. Upscale

- Каждая page image обязательно проходит `SwinIR x4` без GAN, чтобы уменьшить риск генерации несуществующих символов.
- Quality gate сравнивает исходник и upscale: no-reference image quality, clipping/artifact checks и дешёвый OCR preflight на sample regions. Улучшенная версия используется только без деградации; иначе далее передаётся исходный render с записанной причиной fallback.
- Оригинальная rendered page immutable; upscale не изменяет её.

### 3. MinerU

- Использовать закреплённый MinerU `3.4.0`, pipeline backend, offline-local models и проверенную revision/checksum в model profile.
- В производственном адаптере принимать только machine-readable `middle.json`: page geometry, block type, bbox, hierarchy, reading order, table/image/formula regions и block identifiers.
- Текст, OCR spans и Markdown, которые MinerU генерирует сам, не являются источником истины и не должны попасть в final output как текст.
- Адаптер нормализует MinerU schema в собственный versioned `layout_manifest`; прямое использование vendor JSON downstream запрещено.

### 4. PaddleOCR

- OCR запускается только на text-block crops из `layout_manifest`; MinerU остаётся единственным источником page layout и reading order.
- Внутри каждого многострочного text block `PP-OCRv5_server_det` выделяет line crops. Детектор не создаёт новые page blocks и не меняет reading order.
- Script/language router выбирает recognizer на line level:
  - `eslav_PP-OCRv5_mobile_rec` для русского, украинского, белорусского и английского, это основной путь для корпуса.
  - `cyrillic_PP-OCRv5_mobile_rec` для другой кириллицы.
  - `PP-OCRv6_medium` для поддерживаемых Latin/CJK scripts.
  - Неподдерживаемый script фиксируется в manifest и не подменяется галлюцинированным текстом.
- OCR output содержит normalized text, raw text, line/token bbox в page coordinates, language/script, confidence, model ID/revision и link на crop.

### 5. Qwen-VL Markdown reconstruction

- Использовать `Qwen2.5-VL-32B-Instruct` в закреплённой local 4-bit AWQ/GPTQ configuration через vLLM на GPU0.
- Qwen получает ограниченный page/document context: selected rendered/upscaled page image, normalized MinerU layout/reading order, OCR lines/tokens with bbox/confidence, table/image/formula crops и explicit coordinate transforms.
- Output строго schema-validated: block-level Markdown с сохранёнными `block_id`, `page`, `bbox`, evidence citations, table representation, image/formula placeholders, validation flags и uncertainty reasons.
- Qwen может исправить OCR текст только при визуальном evidence в переданном image/crop; изменения должны быть recorded as corrections with source OCR and evidence reference.
- Длинные документы обрабатываются page/chunk-wise с deterministic assemble phase. Document joins не могут изменять block identifiers или потерять source citations.

### 6. Fenic entity extraction

- Fenic используется как semantic DataFrame harness, а не как system-of-record/очередь: он получает уже опубликованный temporary Markdown + block provenance и выполняет `semantic.extract`.
- Local `Qwen3-14B` 4-bit через vLLM на GPU0 предоставляется Fenic как OpenAI-compatible language model. Любой required local-provider adapter и dummy secret configuration должны быть проверены offline в implementation spike до включения production.
- Entity schema pack v1: versioned Pydantic contract `entities[]` с обязательными `type`, `value`, `normalized_value`, `page`, `block_id`, `bbox`, `evidence`, `confidence`. Первые разрешённые типы: `person`, `organization`, `date`, `address`, `identifier`, `amount`.
- Каждая entity должна reference существующий block, валидный bbox и exact evidence quote из final Markdown. Нарушение контракта становится quality finding, не скрытым успешным extraction.

### 7. Publication и quality

- Publisher валидирует Markdown schema, entity schema, references, artifact checksums и manifest completeness до commit.
- Публикация atomically выполняется в отдельный immutable versioned MinIO prefix. Только после успешного commit PostgreSQL transaction переводит item в completed state.
- `manifest.json` обязан содержать `quality=pass|warning|failed`, block/entity confidence, evidence coverage, validation findings, fallback decisions, all retry attempts и model/prompt/schema versions.
- `quality=failed` публикуется, но downstream обязан фильтровать `quality` явно; наличие final bundle не означает корректность результата.

## Данные PostgreSQL

Создать Alembic-managed schema и repository/service boundaries для следующих сущностей:

- `pipeline_profiles`: immutable active/inactive model profile, runtime image digests, local paths, model revisions/checksums, GPU/CPU/disk limits, prompts, thresholds и policy hashes.
- `batches`: submit metadata, requested roots, snapshot timestamps, selected profile, lifecycle state, counters, audit sampling seed, report locations.
- `batch_roots`: normalized allowlisted roots, filesystem identity and scan outcome.
- `documents`: canonical `source_sha256`, basic file metadata, source object lifecycle state, current reusable final versions.
- `batch_items`: one discovered source path within one batch, document link, scan disposition, current stage/state, quality, final output link and quarantine reason.
- `stage_runs`: stage/item attempts, immutable input/output artifact manifests, timestamps, worker, errors, retry classification, resource measurements and correlation ID.
- `jobs`: work unit type, dependencies, priority, state, queue timing, lease, heartbeat, attempt count, payload reference and idempotency key.
- `resource_reservations`: GPU/device, CPU pool, disk quota reservation, owner job, amount, heartbeat and release status.
- `artifacts`: MinIO key, content SHA-256, media type, size, retention class, producer and lineage.
- `entity_results`: compact schema-validated entities and output/quality summary for query/reporting without loading MinIO objects.
- `audit_samples`: deterministic selected batch items, review status, reviewer result and later ground-truth annotation links.
- `events`/`audit_log`: immutable lifecycle actions and manual retry/cancel/operator actions.

State transitions, job claims, lease updates, publication pointer updates и resource reservation release должны выполняться в transactional boundaries. Прямые state updates из model workers запрещены: worker сообщает result controller/repository layer.

## Interfaces и операции

- `idp batch submit ROOT... [--profile PROFILE] [--priority N]`: validates absolute allowlisted roots, creates scan job, returns batch ID.
- `idp batch status BATCH_ID`: counters by stage/state/quality, capacity state, oldest active job and report URL/key.
- `idp batch report BATCH_ID --format json|csv`: all discovered paths and their terminal/current state, output key or exact failure/quarantine reason.
- `idp batch retry BATCH_ID --item ITEM_ID|--state QUARANTINED`: creates new idempotent stage/job attempts only after reason is eligible for retry.
- `idp batch cancel BATCH_ID`: prevents unclaimed jobs; running workers finish only at safe checkpoints, then controller marks remaining work cancelled.
- `idp profile validate PROFILE`: verifies offline model files/checksums, runtime health endpoints, MinIO, PostgreSQL schema, disk quota, GPU detection/VRAM and required Fenic/vLLM compatibility.
- Controller exposes authenticated local operational API/metrics only; raw arbitrary filesystem paths cannot be submitted through an unauthenticated endpoint.

## Implementation sequence

1. Replace planning/documentation boundary before coding.
   - Replace `docs/SUMMARY.md` with the approved pipeline diagram and compact glossary: inputs, outputs, stages, model versions, queue/storage roles, resource ownership, lifecycle states and quality semantics.
   - Replace the obsolete 18-service/Kafka architecture in `docs/architecture.md` with the single-controller design, PostgreSQL queue schema, MinIO artifact hierarchy, GPU scheduling diagram, state machines and recovery semantics.
   - Replace `docs/implementation-plan.md` with this implementation plan in repository-facing form.
   - Replace root `README.md` with install/air-gap prerequisites, controller start, allowed-root configuration, `batch submit/status/report`, output bundle structure and operational constraints.
   - Mark the older `.kilo/plans` implementation/architecture plans as superseded, or remove them only if repository policy allows; this file remains the authoritative plan.

2. Establish project skeleton and local operational stack.
   - Create Python workspace, configuration validation, typed domain models, Make targets and CI checks.
   - Provision PostgreSQL, MinIO, controller, workers, Prometheus/Grafana or equivalent local observability and authenticated model services with pinned image digests.
   - Build connected-release pipeline producing an immutable offline release bundle: pinned OCI images, hashed wheelhouse, OS package snapshot, models/tokenizers/OCR dictionaries, SBOM/licenses, checksums and import/verification tooling.
   - Add target-side import, offline verification, activation and rollback commands. Runtime must use offline mode and fail if a dependency tries to download.
   - Add separate local/CI and target-GPU Compose/profiles so developers can run all non-model tests without full weights or GPU capacity.

3. Implement persistence, work queue and recovery before ML stages.
   - Add Alembic migrations for the schema above, transactional repositories, idempotency keys, stage state machine and event log.
   - Implement `SKIP LOCKED` job claim, dependencies, leases, heartbeat, reaper, cancellation, capacity pause and retry/DLQ/quarantine classification.
   - Implement MinIO artifact writer with SHA-256 verification, immutable prefixes, atomic final publication and lifecycle cleanup.

4. Implement input discovery and batch lifecycle.
   - Add allowlist/realpath checks, recursive scanner without symlink traversal, stable-file check, file limits and SHA-256 deduplication.
   - Implement batch CLI/API, per-path scan dispositions, strict versioned reuse and CSV/JSON reports.
   - Write integration tests for duplicate paths/content, files changing during scan, unavailable source after snapshot, encrypted/corrupt PDF, oversized PDF and controller restart.

5. Implement deterministic image preparation.
   - Render PDF pages vision-only with fixed/adaptive configured DPI and coordinate manifests.
   - Integrate SwinIR x4 tiled inference and quality gate; preserve original render and test fallback with degraded images.
   - Enforce image/pixel/crop/timeout limits and release temporary resources at every safe checkpoint.

6. Implement MinerU structural adapter.
   - Deploy pinned MinerU 3.4.0 pipeline service offline on GPU1.
   - Convert only `middle.json` geometry/reading-order/block classification into internal `layout_manifest`; discard MinerU text/Markdown from the product data path.
   - Contract-test all expected block types, coordinate transforms, multi-column order, tables/images/formulas and MinerU schema drift.

7. Implement OCR with language routing.
   - Run PP-OCRv5 server detector strictly inside MinerU text blocks.
   - Add script/language detection and `eslav`, generic `cyrillic`, and PP-OCRv6 recognizer routing; record line/token-level provenance.
   - Build fixtures dominated by Russian scanned PDFs and validate line coordinates, mixed Cyrillic/Latin text, low-confidence blocks and unknown scripts.

8. Implement GPU scheduler and Qwen-VL reconstruction.
   - Add resource reservations and model worker lifecycle so GPU0 never concurrently hosts Qwen-VL and Qwen3 extraction.
   - Build versioned prompts and structured response schemas for grounded page/chunk Markdown, table transcription and OCR correction logs.
   - Add document assembly with deterministic joins, schema checks, citation validation, retries and quality findings.

9. Implement Fenic entity extraction and publication.
   - Verify Fenic against the local OpenAI-compatible Qwen3-14B endpoint in an air-gapped integration spike; implement a thin adapter if Fenic requires OpenAI-specific configuration.
   - Define and version generic Pydantic entity schema pack; run Fenic extraction, evidence validation and persistence.
   - Publish final bundles atomically, write compact PostgreSQL results, apply quality policy and clean temporary source/artifacts only after durable success.

10. Add observability, operations and validation.
   - Emit structured logs and metrics for throughput, queue depth, lease recovery, retries, quarantine reasons, stage latency, GPU VRAM, batch size, artifacts/disk quota, cache reuse and quality distributions.
   - Implement alerts for stuck leases, repeated OOM, no capacity, failed model endpoint, MinIO failure and high quarantine rate.
   - Implement Tier 0/1 local/CI suites using mocks and synthetic small PDFs; they must never fetch model weights or require the target GPU server.
   - Build Tier 2 target-hardware smoke suite and Tier 3 resumable canary/soak suite from an immutable offline test bundle; only profile/runtime changes require their recorded evidence for promotion.
   - Add `profile validate` and operational smoke tests because no labelled benchmark corpus exists.
   - Select deterministic audit sample per batch: 1% of published items, minimum 20. Store review outcomes to create the first labelled corpus; do not claim accuracy metrics before labels exist.

## Validation and acceptance criteria

- `profile validate` succeeds in a fully air-gapped environment: all image digests, wheelhouse dependencies and model checksums are local; no network download occurs.
- A target import rejects a missing, unpinned or checksum-mismatched image, wheel, model, tokenizer, OCR dictionary or OS package; a verified immutable offline release can be activated and rolled back without internet access.
- Tier 0/1 pass on a machine without A100 GPUs or full model weights. Tier 2/3 target evidence is required only for a model/runtime/profile promotion and is resumable after a target restart.
- `batch submit` rejects non-absolute paths, paths outside the allowlist, symlink escapes and unsupported file types according to documented dispositions.
- A batch with valid, duplicate, changing, corrupt, encrypted and oversized PDFs reaches a terminal state; one failing document never blocks independent items.
- Killing any worker/controller during each stage recovers through lease expiry without duplicate final publication or lost item state.
- A repeated batch reuses only exact compatible completed output and reprocesses after changing a profile, schema or policy hash.
- GPU scheduler prevents concurrent Qwen-VL/Qwen3 execution, respects VRAM budgets, releases reservations after crash and pauses safely under storage pressure.
- Every published `final.md`/`entities.json` has a valid `manifest.json`; all cited pages, blocks, bboxes, OCR lines and entities resolve to artifact lineage.
- Russian PDF fixtures route to East-Slavic OCR; other Cyrillic/Latin/CJK fixtures route to their correct recognizers. No unsupported script is silently represented as confident text.
- Quality `pass`, `warning` and `failed` are distinguishable in PostgreSQL, batch report and manifest. `quality=failed` remains published only when technical processing completed.
- `docs/SUMMARY.md`, `docs/architecture.md`, `docs/implementation-plan.md` and `README.md` contain no operational claims about the removed 18-service/Kafka/native-text/dual-path architecture after the documentation migration.

## Explicit non-goals for v1

- No always-on filesystem watcher; new/changed files are handled by the next submitted one-shot batch.
- No arbitrary office/image/email input conversion.
- No custom review portal or automated human-in-the-loop routing; audit records are persisted for future review tooling.
- No accuracy claim, calibration curve or supervised quality metric until manually reviewed ground truth exists.
- No external cloud APIs or model downloads at runtime.
- No requirement to execute real full-model end-to-end tests on developer laptops or ordinary CI runners.
