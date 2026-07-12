# Offline PDF Batch Pipeline

Локальный полностью изолированный pipeline для пакетной обработки PDF. Система рекурсивно обходит разрешённые каталоги, превращает документы в заземлённый Markdown, извлекает сущности и публикует воспроизводимый результат в MinIO.

PDF text layer намеренно не используется: документ обрабатывается только как изображение страниц.

## Логика работы

```mermaid
flowchart TB
    classDef input fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    classDef control fill:#f3e5f5,stroke:#6a1b9a,color:#4a148c
    classDef cpu fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef gpu1 fill:#fff3e0,stroke:#ef6c00,color:#e65100
    classDef gpu0 fill:#ffebee,stroke:#c62828,color:#b71c1c
    classDef output fill:#fce4ec,stroke:#c2185b,color:#880e4f
    classDef error fill:#eceff1,stroke:#546e7a,color:#263238

    subgraph INPUT["1. Постановка batch"]
        A["Оператор<br/><code>idp batch submit /allowed/root ...</code>"]:::input
        B["Проверка absolute path + realpath<br/>allowlist roots, read-only mounts"]:::control
        C["Рекурсивный scanner<br/>без перехода по symbolic links"]:::cpu
        D["Два одинаковых stat-снимка<br/>SHA-256 PDF"]:::cpu
        A --> B --> C --> D
    end

    subgraph CONTROL["2. Durable control plane"]
        PG[("PostgreSQL<br/>batches, items, jobs,<br/>leases, retries, state")]:::control
        J["Bounded job queues<br/><code>FOR UPDATE SKIP LOCKED</code>"]:::control
        R["Resource reservations<br/>CPU / GPU / storage"]:::control
        D --> PG --> J --> R
    end

    subgraph SAFE["Входные исключения"]
        S1["SKIPPED_UNSUPPORTED"]:::error
        S2["SKIPPED_UNSTABLE"]:::error
        S3["SKIPPED_SYMLINK"]:::error
        Q["QUARANTINED<br/>corrupt/encrypted/limit/error"]:::error
    end

    C -. symlink .-> S3
    D -. non-PDF .-> S1
    D -. changed file .-> S2

    subgraph PREPARE["3. Vision-only preparation"]
        M0[("MinIO temporary artifacts<br/>source PDF, pages, crops, manifests")]:::control
        E["CPU render<br/>PDF -> page images<br/>text layer discarded"]:::cpu
        F["GPU1: SwinIR x4<br/>image-quality gate"]:::gpu1
        G{"Upscale improves image?"}:::gpu1
        H["Use upscaled page"]:::gpu1
        I["Use original rendered page<br/>record fallback reason"]:::gpu1
        R --> M0 --> E --> F --> G
        G -->|yes| H
        G -->|no| I
    end

    subgraph LAYOUT["4. Full document structure"]
        L["GPU1: MinerU 3.4<br/>complete layout from every page"]:::gpu1
        LM["Internal layout manifest<br/>all blocks, bbox, hierarchy,<br/>relations, reading order, crops"]:::gpu1
        H --> L
        I --> L
        L --> LM
    end

    subgraph OCR["5. OCR only inside text blocks"]
        O1["GPU1: PP-OCRv5 detector<br/>lines within MinerU text blocks"]:::gpu1
        O2{"Script / language router"}:::gpu1
        O3["East-Slavic PP-OCRv5<br/>RU / UK / BY / EN"]:::gpu1
        O4["Cyrillic PP-OCRv5<br/>other Cyrillic"]:::gpu1
        O5["PP-OCRv6 medium<br/>supported Latin / CJK"]:::gpu1
        OM["OCR manifest<br/>text, token bbox, confidence,<br/>language, model revision"]:::gpu1
        LM --> O1 --> O2
        O2 --> O3 --> OM
        O2 --> O4 --> OM
        O2 --> O5 --> OM
    end

    subgraph RECONSTRUCT["6. One Qwen-VL reconstruction run"]
        GS["GPU0 admission scheduler<br/>only one heavy role at a time"]:::control
        V["GPU0: Qwen2.5-VL-32B<br/>images + crops + layout + OCR"]:::gpu0
        MD["Grounded document Markdown<br/>block_id, page, bbox, evidence"]:::gpu0
        VF["Evidence-based findings<br/>OCR corrections, unreadable blocks,<br/>missing blocks, simple logic checks"]:::gpu0
        LM --> GS
        OM --> GS
        GS --> V
        V --> MD
        V --> VF
    end

    subgraph ENTITIES["7. Entity extraction"]
        FN["GPU0: Fenic + Qwen3-14B<br/>schema-driven semantic.extract"]:::gpu0
        EN["Typed entities<br/>type, value, page, block_id,<br/>bbox, evidence, confidence"]:::gpu0
        MD --> GS --> FN --> EN
    end

    subgraph PUBLISH["8. Publication and report"]
        P["Validate schemas, hashes<br/>and every evidence reference"]:::output
        BUNDLE["Immutable MinIO result bundle<br/><code>final.md</code><br/><code>entities.json</code><br/><code>manifest.json</code>"]:::output
        STATE{"Quality state"}:::output
        DONE["COMPLETED<br/>quality=pass"]:::output
        WARN["COMPLETED_WITH_WARNINGS<br/>quality=warning or failed"]:::output
        REPORT["PostgreSQL state + CSV/JSON report"]:::control
        EN --> P
        VF --> P
        P --> BUNDLE --> STATE
        STATE -->|pass| DONE --> REPORT
        STATE -->|warning / failed| WARN --> REPORT
    end

    E -. terminal technical failure .-> Q
    F -. terminal technical failure .-> Q
    L -. terminal technical failure .-> Q
    O1 -. terminal technical failure .-> Q
    V -. terminal technical failure .-> Q
    FN -. terminal technical failure .-> Q
    Q --> REPORT
```

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
