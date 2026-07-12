# Offline PDF Batch Pipeline

Локальный полностью изолированный pipeline для пакетной обработки PDF. Система рекурсивно обходит разрешённые каталоги, превращает документы в заземлённый Markdown, извлекает сущности и публикует воспроизводимый результат в MinIO.

PDF text layer намеренно не используется: документ обрабатывается только как изображение страниц.

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

Controller и workers работают как постоянный сервис через target deployment profile. Команда submit только создаёт batch и сразу возвращает его идентификатор:

```bash
idp batch submit /data/incoming/contracts /data/incoming/reports
```

Проверка состояния и полный отчёт:

```bash
idp batch status <batch-id>
idp batch report <batch-id> --format json
```

Controller продолжает обработку после закрытия терминала. После падения worker/controller задания возвращаются в очередь по истечении lease.

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

Перед активацией target server проверяет все assets через `idp profile validate <profile>`. Rollback переключает на уже импортированный immutable profile и не требует интернет-доступа.

## Тестирование

- Local CI запускает unit, schema, queue, storage и mocked integration tests без GPU и model weights.
- Target server запускает resumable real-model smoke и canary/soak suites только перед promotion model/runtime/profile.
- Пока нет размеченного ground truth, quality checks подтверждают схемы, lineage, evidence, recovery и resource limits, но не заявляют accuracy metrics.
