# Offline PDF Batch Pipeline

Локальный полностью изолированный пайплайн для пакетной обработки документов. Система рекурсивно обходит разрешённые каталоги, конвертирует их в формат Markdown, извлекает сущности и публикует результат в MinIO.

В PDF текстовый слой намеренно не используется: документ обрабатывается только как изображение страниц.

Перед MinerU каждый PDF рендерится через vision-only рендерер в растровые изображения страниц в RGB с фиксированным разрешением (DPI) и сохранёнными координатами трансформации.. Каждая страница передаётся в локальный SwinIR x4 адаптер. Если размер улучшенного изображения меньше исходника, превышает лимит по количеству пикселей, теряет энтропию или заметно обрезает яркостные значения, в пайплайне сохраняется улучшенный артефакт для аудита, но в MinerU передаёт оригинальный рендер с записанной причиной. Ни рендерер, ни шлюз качества не читают текстовый и не используют OCR.

MinerU получает только отобранные изображения страниц и формирует локальный файл middle.json. Этот исходный артефакт сохраняется без изменений для целей аудита, однако его данные (OCR, текст, Markdown, HTML, LaTeX) никогда не используются как источник контента. Внутренний layout_manifest хранит геометрию, тип, порядок чтения, иерархию, связи и обрезку каждого блока, включая типы, которые могут появиться в будущем. Таблицы, изображения, диаграммы, формулы, колонтитулы, печати и подписи не удаляются; их смысловое содержание будет восстановлено на отдельном этапе с использованием Qwen-VL.

PaddleOCR не создаёт новый page layout: `PP-OCRv5_server_det` выделяет строки только внутри text-bearing MinerU blocks. Router запускает East-Slavic PP-OCRv5 для RU/UK/BY/EN, Cyrillic PP-OCRv5 для другой кириллицы и PP-OCRv6 medium для поддерживаемых Latin/CJK scripts. У каждого token сохраняются page bbox, block ID, detector/recognizer confidence, script, language, model ID, revision и line crop. Неподдерживаемый script не превращается в текст: он остаётся image evidence с `unsupported_script` finding для Qwen-VL.

Qwen2.5-VL-32B работает в одном logical reconstruction run на эксклюзивном GPU0 role slot. Он получает selected page images, все relevant block crops, полный MinerU layout/reading order, PaddleOCR tokens и OCR findings. При длинных страницах controller дробит блоки только по image/block budget, затем детерминированно склеивает ответы в один Markdown, без второго validation pipeline. Structured output обязан вернуть каждый block ID ровно один раз в исходном reading order; OCR correction допустима только для существующего token в том же block с visual evidence. В том же output модель расшифровывает таблицы, изображения, diagrams, charts, formulas, stamps/signatures и возвращает findings по OCR disagreement, unreadable regions, missing blocks и очевидным суммам/датам/нумерации/ссылкам.

## Логика работы

![Схема offline PDF batch pipeline](docs/pipeline-overview.svg)

## Компоненты

| Компонент | Назначение | Ресурс |
|---|---|---|
| PostgreSQL | Источник истины, очередь работ, leases, retries, статусы и reservations | CPU / disk |
| MinIO | Временные артефакты и immutable result bundles | Local storage |
| Render | Детерминированный PDF-to-image; text layer игнорируется | CPU |
| SwinIR x4 | Upscale каждой страницы с автоматическим fallback | GPU1 |
| MinerU 3.4 | Полный layout всех блоков и reading order | GPU1 |
| PaddleOCR | Построчный OCR только внутри текстовых блоков MinerU | GPU1 |
| Qwen2.5-VL-32B | OCR validation, таблицы, изображения, диаграммы, формулы и сборка Markdown | GPU0 |
| Fenic + Qwen3-14B | Schema-driven extraction сущностей из Markdown | GPU0 |

## Что сохраняет MinerU

Внутренний `layout_manifest` сохраняет каждый блок без потерь: текст, заголовки, списки, таблицы, изображения, диаграммы, формулы, header/footer, сноски, печати и подписи. Для каждого блока доступны `block_id`, страница, bbox, hierarchy, relations, reading order и ссылка на crop.

OCR обрабатывает только текстовые блоки. Все остальные блоки передаются в Qwen-VL вместе с изображением и metadata: модель расшифровывает таблицы, интерпретирует диаграммы, графики и формулы, затем включает значимую информацию в единый Markdown.

## Запуск

В репозитории есть два deployment profile:

- `infra/compose/local.yml` поднимает PostgreSQL, MinIO, Alembic migration, controller и idle worker для локальной проверки control plane.
- `infra/compose/target.yml` требует immutable image digests и секреты, отключает внешний network egress через internal network и запускает migration до controller/worker.

PostgreSQL control plane реализует `FOR UPDATE SKIP LOCKED`, leases/heartbeat recovery, retries/quarantine, resource pools, MinIO artifact contract и atomic final-output pointer. Batch API принимает только абсолютные каталоги внутри `IDP_ALLOWED_ROOTS`, не следует symbolic links, сохраняет все dispositions и перед постановкой работы копирует stable PDF из проверенного file descriptor в temporary artifact storage.

Команда submit создаёт immutable snapshot и сразу возвращает batch ID:

```bash
idp batch submit /data/incoming/contracts /data/incoming/reports
```

Проверка состояния и полный отчёт:

```bash
idp batch status <batch-id>
idp batch report <batch-id> --format json
```

Controller продолжает обработку после закрытия терминала. После падения worker/controller задания возвращаются в очередь по истечении lease.

Отчёт всегда содержит все пути: queued, reused, skipped, quarantined и cancelled. Повторить можно только quarantined item, используя уже скопированный immutable source object, а не изменившийся исходный файл:

```bash
idp batch report <batch-id> --format csv
idp batch cancel <batch-id>
idp batch retry <item-id>
```

Повторное содержимое PDF переиспользует published final bundle только при совпадении SHA-256 и immutable `pipeline_profile_hash`; промежуточные, cancelled и незавершённые результаты не переиспользуются.

## Результаты

Для каждого технически обработанного PDF публикуется immutable bundle в MinIO:

```text
final.md
entities.json
manifest.json
```

`manifest.json` содержит hashes, artifact lineage, profile/model/prompt/schema versions, OCR coverage, VLM findings, fallback decisions, попытки обработки и `quality=pass|warning|failed`.

`quality=failed` не скрывает технически готовый результат: он публикуется со статусом `COMPLETED_WITH_WARNINGS`. Файлы, которые невозможно обработать технически, получают `QUARANTINED` и не блокируют оставшийся batch.

## Air-Gapped Развёртывание

Runtime не имеет доступа к интернету и не выполняет model/package downloads. Connected build host готовит immutable release bundle с pinned OCI images, Python wheelhouse, системными пакетами, моделями, tokenizers, OCR dictionaries, checksums, SBOM и import scripts.

Release bundle содержит подписанный Ed25519 `manifest.json`: для каждого asset в нём зафиксированы путь, тип, размер и SHA-256. Private signing key остаётся только на connected build host; target хранит только public verification key. Target не может выпустить доверенный release самостоятельно.

```bash
# Connected build host: явный JSON build spec + private key.
idp release build release-spec.json ./out/release-2026.07.13 --private-key ./release-private.pem

# Target: проверить транспортируемый bundle, импортировать, активировать.
idp release verify /media/release-2026.07.13 --public-key /etc/idp/release-public.pem
idp release import /media/release-2026.07.13
idp release activate release-2026.07.13
idp profile validate
```

Импорт сначала проверяет signature и SHA-256 всех assets, копирует bundle в staging, повторно проверяет его и только затем атомарно публикует immutable release directory. OCI archives загружаются только из проверенных локальных файлов. Rollback переключает активный symlink на уже импортированный проверенный release и не требует интернет-доступа:

```bash
idp release rollback release-2026.07.12
```

`profile validate` проверяет active release, offline flags, PostgreSQL и MinIO до запуска controller/worker. Systemd units в `infra/systemd/` делают эту проверку обязательной startup dependency.

## Тестирование

- Local CI запускает unit, schema, queue, storage и mocked integration tests без GPU и model weights.
- Target server запускает resumable real-model smoke и canary/soak suites только перед promotion model/runtime/profile.
- Пока нет размеченного ground truth, quality checks подтверждают схемы, lineage, evidence, recovery и resource limits, но не заявляют accuracy metrics.
