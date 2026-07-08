# IDP System — Detailed Implementation Plan

> **Architecture reference**: `docs/architecture.md` (§1–24)  
> **Stack**: Python (FastAPI/FastStream) · gRPC (proto3) · Kafka · PostgreSQL · MinIO · Redis · MLflow  
> **Deployment**: Air-gapped, 2×A100 40GB (Variant A), all models local  
> **Dataset**: none — built from scratch  
> **HITL**: REST API + Swagger UI (no custom React portal)  
> **Deadline**: none — quality-first

---

# PART I — PHASE BREAKDOWN

## Phase 0 — Foundation & Infrastructure

### Цель
Создать инфраструктурную основу и общие библиотеки, на которой будут строиться все 18 сервисов. Доказать, что Kafka→MinIO→PG→Redis цикл работает в air-gapped контейнере на целевом железе.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 0.1 | **Git-монорепозиторий** | Инициализация структуры (§27 ниже), настройка branch strategy (trunk-based), CODEOWNERS, PR templates |
| 0.2 | **Proto-контракты** | Заморозить все gRPC-сервисы в `contracts/protos/` с versioning; сгенерировать Python-стабы |
| 0.3 | **Kafka** | Docker Compose для локальной разработки; на Production —裸 Kafka на bare metal; topology и naming conventions для топиков (§15.6, §23.3) |
| 0.4 | **PostgreSQL** | Schema: `documents`, `stage_results`, `reviews`, `audit_log`, `calibration_curves`; миграции через Alembic |
| 0.5 | **MinIO** | Buckets: `raw/`, `pages/`, `enhanced/`, `artifacts/`; lifecycle policies; SDK-обёртка |
| 0.6 | **Redis** | Idempotency keys (TTL 7d), dedup cache, distributed locks, circuit breaker state |
| 0.7 | **MLflow (offline mode)** | Model registry, experiment tracking, calibration curve versioning; конфиг для air-gap (без telemetry) |
| 0.8 | **Common Python-библиотека `idp-common`** | Envelope (§15.2), Kafka producer/consumer wrappers, retry/backoff/circuit-breaker, structured logging (JSON), OpenTelemetry integration, MinIO/Redis/PG client factories, error classification (§15.4), idempotency decorator |
| 0.9 | **Docker Compose** | Полный стек для локальной разработки: Kafka+ZooKeeper, PG, MinIO, Redis, MLflow. GPU-сервисы как placeholder-контейнеры |
| 0.10 | **CI/CD pipeline** | Lint (ruff), type-check (mypy), unit tests (pytest), proto compilation check, Docker image build, container registry (local, air-gap-ready) |
| 0.11 | **Observability base** | Loki (logs), Prometheus (metrics), Grafana (dashboards), Tempo (traces); OpenTelemetry collector; sample dashboard «Document Funnel» |
| 0.12 | **Synthetic data pipeline** | Генератор тестовых документов: PDF (digital/scan/hybrid), TIFF, PNG, JPG, DOCX, XLSX, PPTX, HTML, EML. Генерация ground-truth полей. Шаблоны инвойсов/форм/контрактов |

### Зависимости
Нет (стартовая фаза).

### Ожидаемый результат
- Разработчик клонирует репозиторий, запускает `docker compose up` → Kafka, PG, MinIO, Redis, MLflow, observability стек подняты
- Envelope отправляется в Kafka, записывается в MinIO/PG, виден в Grafana
- Proto-стабы компилируются, контракты заморожены
- Synthetic data pipeline генерирует набор документов с ground-truth

### Критерии готовности
- [ ] `make lint && make typecheck && make test` — все зелёные
- [ ] Docker Compose поднимается за <60s на целевом железе
- [ ] Envelope roundtrip (produce→consume→store→read) — 100% success
- [ ] Proto backward-compatibility проверена (buf breaking)
- [ ] CI/CD пайплайн работает на каждом push

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Air-gapped pip/npm registry несовместим с версиями библиотек | Средняя | Высокое | Vendoring всех зависимостей в wheelhouse на этапе 0.1, тестирование на изолированном стенде |
| GPU-драйверы/CUDA несовместимы с целевыми контейнерами | Средняя | Высокое | Тестирование на целевом железе на этапе 0.1; NVIDIA Container Toolkit |
| Kafka topic schema drift при параллельной разработке | Низкая | Среднее | Schema Registry (Confluent/Apicurio) + buf breaking в CI |

### Сложность
Высокая (инфраструктурная, требует экспертизы DevOps + MLOps)

---

## Phase 1 — Skeleton Pipeline (End-to-End Trace)

### Цель
Построить минимальный end-to-end пайплайн: документ входит → проходит Ingestion → Normalization → выходит как page-images + manifest. Доказать, что документ проходит через все инфраструктурные компоненты.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 1.1 | **S1 Ingestion** | HTTP upload endpoint (FastAPI), SHA-256 dedup, MinIO storage, Kafka emit |
| 1.2 | **S2 Format Normalization** | PDF→images (PyMuPDF/pdf2image), TIFF→images, office→PDF→images (LibreOffice), HTML→screenshot (Playwright), EML/MSG→MIME parse + recurse |
| 1.3 | **Basic Orchestrator (S18-lite)** | Kafka consumer producing next-stage commands; simple linear flow: normalize→done |
| 1.4 | **Contract tests** | End-to-end: upload PDF → verify pages in MinIO + state in PG |
| 1.5 | **Synthetic test corpus** | 100 документов каждого типа (900 total) с ground-truth |

### Зависимости
Phase 0 (infrastructure, common libs, proto).

### Ожидаемый результат
- Загрузка PDF/TIFF/PNG/JPG/DOCX/XLSX/PPTX/HTML/EML → page-images + native_text + render_manifest
- Каждый документ проходит через Kafka и получает state `NORMALIZED` в PG
- Dedup работает: тот же файл → тот же doc_id

### Критерии готовности
- [ ] 100% документов из synthetic corpus успешно нормализуются
- [ ] Каждый тип формата (PDF scan/digital/hybrid, TIFF, PNG, JPG, DOCX, XLSX, PPTX, HTML, EML) обработан без ошибок
- [ ] Latency p95 < 10s на 10-страничный PDF (CPU, 16 vCPU)
- [ ] Dedup: повторная загрузка возвращает тот же doc_id
- [ ] Structured logs видны в Loki, traces в Tempo

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| LibreOffice падает на сложных DOCX | Высокая | Среднее | Sandbox-процесс с таймаутом 60s; fallback на рендер через unoconv |
| Playwright нестабилен в контейнере (headless Chromium) | Средняя | Среднее | Использовать официальный Docker-образ Playwright; fallback на wkhtmltopdf |
| EML/MSG с вложенными .eml — рекурсивный цикл | Средняя | Низкое | Лимит глубины рекурсии (3 уровня) |

### Сложность
Средняя (документированные библиотеки, но много edge-cases в форматах)

---

## Phase 2 — Classification & Text Quality

### Цель
Классифицировать документы по типам, определить границы пакетов, оценить качество текстового слоя PDF и решить: использовать native text или запускать OCR.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 2.1 | **S3 Document Classification** | LayoutLMv3 classifier, BIO-sequence labeling для пакетов, OOD-детекция, processing profile routing |
| 2.2 | **S4 Text Layer Quality Estimator** | Coverage/garbage/geometry/cross-check/LID детекторы, взвешенная агрегация |
| 2.3 | **Classification model training** | Разметка synthetic corpus для типов документов; fine-tune LayoutLMv3; валидация F1≥0.90 |
| 2.4 | **Text quality calibration dataset** | Разметка 500+ PDF как «clean/dirty/broken»; обучение логистического агрегатора |
| 2.5 | **Profile config** | YAML-схемы типов документов: поля, валидаторы, extraction strategies |

### Зависимости
Phase 1 (S2 выдаёт page-images + native_text).

### Ожидаемый результат
- Документ классифицируется по типу (invoice, contract, form, letter, report, ID, unknown) с confidence ≥0.90
- Пакеты из нескольких документов разбиваются на сегменты
- Каждый PDF-регион помечен как «use_native» или «ocr_required» с trust score
- Processing profile определён и передаётся downstream

### Критерии готовности
- [ ] Classification F1 ≥ 0.90 на synthetic corpus (известные типы)
- [ ] OOD detection AUROC ≥ 0.85
- [ ] Text trust accuracy ≥ 85% против ground-truth (где доступен и native text, и OCR)
- [ ] Packet boundary detection precision/recall ≥ 0.80

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Мало данных для обучения классификатора | Высокая | Высокое | Synthetic data augmentation; zero-shot fallback через Qwen2.5-VL; active learning loop |
| Packet boundary detection ненадёжен на нестандартных пакетах | Средняя | Среднее | Conservative mode: не разбивать при низкой confidence, весь пакет → generic profile |
| Classification drift при новых типах документов | Средняя | Среднее | OOD detector + periodic retraining; «unknown» → generic profile + HITL |

### Сложность
Высокая (ML: модель + калибровка + fallback-логика)

---

## Phase 3 — OCR Pipeline

### Цель
Построить ensemble OCR из нескольких движков + consensus-алгоритм, который даёт текст точнее любого одиночного движка.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 3.1 | **S5 Image Enhancement** | Deskew, denoise, binarization, super-resolution (Real-ESRGAN), no-reference quality guardrail |
| 3.2 | **S6 OCR Ensemble** | PaddleOCR-VL-0.9B (GPU1), Tesseract 5 (CPU), TrOCR (GPU1); engine routing по типу региона |
| 3.3 | **S7 OCR Consensus** | ROVER-алгоритм, взвешенное голосование, disagreement scoring, token-level alignment |
| 3.4 | **OCR Engine calibration** | Per-engine isotonic calibration на synthetic corpus; char-level accuracy benchmarks |
| 3.5 | **Enhancement A/B evaluation** | Бенчмарк CER до/после enhancement на размеченном наборе |

### Зависимости
Phase 2 (S4 определяет какие регионы идут на OCR, S3 даёт язык-хинт).

### Ожидаемый результат
- Каждый low-trust регион проходит Enhancement → OCR (≥2 движка) → Consensus
- Консенсусный текст точнее лучшего одиночного движка
- Per-token confidence калибрована
- Флаги disagreement для ревью

### Критерии готовности
- [ ] Consensus CER < best_single_engine CER на synthetic corpus (доказательство ценности ensemble)
- [ ] Средний disagreement по документу < 10% (стабильность)
- [ ] Enhancement (super-res) снижает CER на ≥15% для low-DPI кропов
- [ ] Все три движка работают параллельно, GPU1 VRAM < 8GB
- [ ] OCR ensemble latency p95 < 2s per page

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| PaddleOCR-VL-0.9B глючит в air-gapped среде (нет HF download) | Средняя | Высокое | Pre-download весов; HF_HUB_OFFLINE=1; smoke-test при provisioning |
| Consensus неверен, когда все движки ошибаются одинаково (common error) | Средняя | Среднее | RaV (Phase 6) ловит; добавить 다양ность движков (Handwritten → TrOCR, CJK → отдельный) |
| Super-resolution создаёт артефакты, ухудшающие OCR | Средняя | Низкое | No-reference quality guardrail: если метрика ухудшилась — откат к оригиналу |

### Сложность
Высокая (ensemble + калибровка + GPU management)

---

## Phase 4 — Layout & Element Reconstruction

### Цель
Обнаружить все структурные элементы страницы, восстановить reading order, извлечь таблицы, фигуры и формулы, собрать всё в CanonicalDoc.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 4.1 | **S8 Layout Reconstruction** | RT-DETR/PP-DocLayoutV2, Pointer Network, hierarchy tree, text binding |
| 4.2 | **S9 Table Reconstruction** | TATR/POTATR, cell content OCR, grid canonicalization, cross-page merge, column semantics |
| 4.3 | **S10 Figure Processing** | Chart/diagram/image/signature/stamp routing; VLM chart extraction; CLIP embeddings |
| 4.4 | **S11 Semantic Reconstruction** | Merge+order, paragraph stitching, cross-reference resolution, Markdown+block-JSON assembly |
| 4.5 | **DocLayNet evaluation** | Layout mAP benchmark against DocLayNet; reading-order accuracy (Kendall τ) |
| 4.6 | **PubTables-v2 evaluation** | TEDS/GriTS benchmark for table extraction |

### Зависимости
Phase 3 (консенсусный текст + native text для high-trust регионов).

### Ожидаемый результат
- Каждая страница: список bbox-регионов с типами + reading order + hierarchy
- Таблицы: логическая сетка (rows×cols, merged cells) + содержимое ячеек + HTML/OTSL
- Фигуры: структурированные данные (chart data) + captions + embeddings
- CanonicalDoc: единый Markdown + block-JSON с грудингом

### Критерии готовности
- [ ] Layout mAP ≥ 0.85 на DocLayNet
- [ ] Reading-order accuracy (Kendall τ) ≥ 0.80
- [ ] Table TEDS ≥ 0.85, TEDS-Struct ≥ 0.80 на PubTables-2
- [ ] Table recall ≥ 95% (пропуск таблицы = критическая ошибка)
- [ ] Cross-page table merge precision ≥ 90%
- [ ] CanonicalDoc содержит 100% элементов исходной страницы (recall)

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Multi-column reading order путается на сложных макетах | Высокая | Высокое | Layout-as-Thought fallback через VLM; XY-cut sanity check |
| Таблицы без рамок (borderless) не детектируются | Средняя | Высокое | Включить borderless в training data; TATR-v1.2 + borderless variant |
| Cross-page table merge ошибочный | Средняя | Среднее | Image classifier с высоким F1 (0.99 в PubTables-v2); conservative threshold |

### Сложность
Очень высокая (ML + геометрические эвристики + edge-cases)

---

## Phase 5 — Entity Extraction (Dual-Path)

### Цель
Извлечь целевые сущности двумя независимыми моделями (specialist + VLM) с разными режимами отказа.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 5.1 | **S12 Path A (Specialist)** | LayoutLMv3 fine-tuned на extraction; token-classification/QA по schema |
| 5.2 | **S12 Path B (VLM)** | Qwen2.5-VL-32B AWQ на GPU0 через vLLM; schema-guided prompt; structured JSON output; Layout-as-Thought |
| 5.3 | **Extraction schemas** | JSON Schema для каждого типа документа (invoice, contract, form, ...); versioning |
| 5.4 | **LayoutLMv3 training pipeline** | Сбор данных (synthetic + HITL-corrected), fine-tuning, evaluation, versioning в MLflow |
| 5.5 | **VLM prompt engineering** | System prompts для каждого типа; few-shot examples; structured output grammar |
| 5.6 | **Grounding** | Каждое поле привязано к bbox/странице + цитате из документа |

### Зависимости
Phase 4 (CanonicalDoc + page-images), Phase 0 (GPU0 для VLM, GPU1 для specialist).

### Ожидаемый результат
- Каждый тип документа: schema → два набора кандидатов (Path A, Path B)
- Каждое поле: value + bbox + page + evidence + raw_confidence
- Path B отдаёт logprobs для confidence fusion
- LayoutLMv3 работает на GPU1, Qwen2.5-VL на GPU0, не конфликтуют

### Критерии готовности
- [ ] Per-field F1 (Path A) ≥ 0.85, Per-field F1 (Path B) ≥ 0.85 на synthetic corpus
- [ ] Оба пути работают параллельно без VRAM contention (GPU0 < 38GB, GPU1 < 8GB)
- [ ] Hallucination rate (значение есть, а в документе нет) < 5% для каждого пути
- [ ] Schema validation: 100% выходов Path B соответствуют JSON Schema
- [ ] Layout-as-Thought улучшает accuracy на сложных документах ≥ 3pp

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| LayoutLMv3 требует大量的 размеченных данных для fine-tuning | Высокая | Высокое | Synthetic data generation + data augmentation; few-shot через VLM для seed |
| Qwen2.5-VL-32B halluncinates значения, которых нет в документе | Средняя | Высокое | Schema-constrained decoding; RaV (Phase 6) + grounding validation |
| vLLM OOM при длинных документах | Средняя | Среднее | Dynamic batching; chunking документов; KV-cache optimization |
| GPU0 и GPU1 конфликтуют при пиковом использовании | Низкая | Высокое | Strict CUDA_VISIBLE_DEVICES pinning; VRAM budget monitoring + alerting |

### Сложность
Очень высокая (ML training + VLM serving + dual-path architecture)

---

## Phase 6 — Validation & Confidence

### Цель
Построить три уровня валидации (синтаксис, логика, anti-hallucination) и multi-signal confidence engine, которая даёт «честные» per-field confidence scores с ECE < 0.03.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 6.1 | **S13 Reconciliation** | Exact/fuzzy/numeric/semantic comparison; tiebreaker logic |
| 6.2 | **S14 Validation Engine** | Format rules, cross-field LLM validation, RaV (Reconstruction-as-Validation), external DB lookups |
| 6.3 | **S15 Confidence Fusion** | CatBoost meta-classifier, per-type calibration curves (isotonic), Hunter-Mapper dual-call |
| 6.4 | **Calibration dataset** | Накопление размеченных примеров (initially synthetic, потом HITL); ≥500 per doc-type |
| 6.5 | **RaV implementation** | Render extracted value back → compare with original region crop; fidelity scoring |
| 6.6 | **ECE/Brier evaluation** | Weekly calibration quality check; alerting at ECE > 0.03 |

### Зависимости
Phase 5 (оба пути extraction).

### Ожидаемый результат
- Каждое поле: reconciled value + validated (ok/warn/fail) + calibrated confidence
- Agreement между Path A/B → сильнейший confidence signal
- RaV ловит hallucinations с fidelity ρ > 0.80
- ECE < 0.03 per doc-type (после ≥500 labeled examples)

### Критерии готовности
- [ ] ECE < 0.05 на текущем наборе (до набора 500+ examples; <0.03 после)
- [ ] Brier score < 0.10
- [ ] AUROC agreement→correct ≥ 0.80
- [ ] RaV Spearman ρ ≥ 0.75 с fact-based quality
- [ ] Cross-field validation ловит ≥ 80% ошибок (total ≠ Σ line_items, invalid dates, etc.)
- [ ] Hallucination detection rate ≥ 90% (на injected errors)

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Мало данных для калибровки → нетипичные кривые | Высокая | Среднее | Generic calibration curve + conservative thresholds; active learning |
| CatBoost poorly calibrated out of box | Средняя | Среднее | Post-hoc isotonic regression поверх CatBoost |
| RaV ложные срабатывания (ложная positive) | Средняя | Низкое | Threshold tuning; не блокирует pipeline (warn, не reject) |

### Сложность
Высокая (ML: калибровка + uncertainty quantification + RaV)

---

## Phase 7 — Routing, HITL & Feedback

### Цель
Маршрутизировать документы по confidence-бэндам, предоставить REST API для human review, собирать исправления и закрывать обратную связь.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 7.1 | **S16 Routing & HITL** | Confidence-based router, self-correct loop (max 2 retries), review task queue |
| 7.2 | **S17 Feedback/Training** | Correction collection, dataset versioning, periodic retraining pipeline (Airflow/cron) |
| 7.3 | **HITL REST API** | FastAPI endpoints: `GET /reviews`, `POST /reviews/{id}/resolve`, Swagger UI |
| 7.4 | **Output API** | `GET /v1/documents/{id}/output` — финальный результат + confidence + citations |
| 7.5 | **Self-correct loop** | При reject (<0.50): reformulate prompt, alt-OCR, alt-VLM → retry → fail → HITL |
| 7.6 | **Feedback loop** | Human corrections → dataset → retrain → gate evaluation → model promotion |

### Зависимости
Phase 6 (calibrated confidence).

### Ожидаемый результат
- Auto-accept rate ≥ 60% на synthetic corpus (conf ≥ 0.95)
- Review rate ≤ 30% (0.50–0.95)
- Reject rate ≤ 10% (<0.50)
- HITL API работает: получение задачи, показ bbox на изображении, ввод исправления, сохранение
- Self-correct loop улучшает auto-accept rate на ≥ 10pp

### Критерии готовности
- [ ] Auto-accept ≥ 60%, Review ≤ 30%, Reject ≤ 10%
- [ ] Self-correct success rate ≥ 40% (из reject → auto-accept после retry)
- [ ] HITL REST: CRUD операции, Swagger UI доступен
- [ ] Feedback pipeline: correction → retrained model → improved metrics (gate pass)
- [ ] SLA: документ обработан полностью (auto+review) за < 30 min

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Self-correct loop зацикливается | Средняя | Среднее | Hard cap = 2 retries; после → HITL |
| Retrained модель регрессирует | Средняя | Высокое | Gate evaluation (A/B); если регрессия → rollback к предыдущей версии |
| HITL throughput не успевает за intake | Средняя | Среднее | Queue depth alerting; auto-accept threshold adjustment |

### Сложность
Средняя (API + queue management + ML pipeline)

---

## Phase 8 — Orchestrator (Full DAG)

### Цель
Построить полноценный оркестратор: DAG обработки, параллелизм, fan-out/fan-in, таймауты, компенсации, lifecycle state machine.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 8.1 | **S18 Orchestrator** | Kafka-сага: DAG edges, per-stage timeouts, SLA-deadlines, stuck-detection watchdog |
| 8.2 | **State machine** | Lifecycle states (§21): INGESTED→...→COMPLETED/FAILED/PARTIAL; PostgreSQL transitions |
| 8.3 | **Fan-out/fan-in** | Layout→{Tables, Figures, Semantic} parallel; Extract Path A∥B join |
| 8.4 | **Observability** | Document funnel dashboard, stuck documents alert, per-stage latency |
| 8.5 | **Retry orchestration** | Self-correct triggers → OCR/Layout re-processing with different params |

### Зависимости
Phases 1–7 (все сервисы построены).

### Ожидаемый результат
- Полный DAG: документ проходит все 18 сервисов по event-driven графу
- Параллельные ветви (tables, figures, Path A/B) работают одновременно
- Timeout на каждом stage → auto-retry → DLQ
- Dashboard: текущий state каждого документа + funnel analytics

### Критерии готовности
- [ ] End-to-end latency p95 < 5 min на 10-страничный PDF
- [ ] Stuck document detection: alert через 2 min после последнего event
- [ ] Fan-out/fan-in: параллельные ветви завершаются join перед следующим stage
- [ ] Document funnel: видно распределение по state на dashboard
- [ ] Failure recovery: после crash любого сервиса → документ обрабатывается повторно (идемпотентно)

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Сложная state machine с race conditions | Средняя | Высокое | PostgreSQL row-level locking; idempotency keys; event sourcing |
| Один медленный сервис блокирует pipeline | Средняя | Среднее | Per-stage timeout + graceful degradation (best-effort output) |
| Зависшие документы накапливаются | Низкая | Среднее | Watchdog + auto-quarantine; dashboard alerts |

### Сложность
Высокая (distributed systems, event-driven architecture)

---

## Phase 9 — Production Hardening

### Цель
Подготовить систему к production-развёртыванию: performance, security, air-gap provisioning, мониторинг, документация.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 9.1 | **Performance optimization** | GPU batching, async I/O, connection pooling, query optimization, profiling |
| 9.2 | **Security hardening** | mTLS, auth (JWT/API keys), encryption at rest (MinIO, PG), PII scrubbing in logs, network policies |
| 9.3 | **Air-gap provisioning** | Offline model weights, wheelhouse, docker images, pip mirror, HF cache, telemetry disable |
| 9.4 | **Monitoring & alerting** | Grafana dashboards (per-service + global), Prometheus alerts, PagerDuty-compatible |
| 9.5 | **Documentation** | API docs (Swagger/gRPC reflection), runbooks, onboarding guide, architecture overview |
| 9.6 | **Load testing** | Target throughput: 1000 docs/hour; benchmark on target hardware |
| 9.7 | **Disaster recovery** | Backup strategy (PG, MinIO, MLflow), restore procedures, RTO/RPO definitions |

### Зависимости
Phase 8 (full system operational).

### Ожидаемый результат
- Система работает на 2×A100 40GB, air-gapped, без внешних зависимостей
- Мониторинг: все метрики видны, алерты настроены
- Load test: throughput ≥ 1000 docs/hour
- Security audit pass (mTLS, auth, encryption)
- Документация полная, команде не нужно спраивать «как это работает»

### Критерии готовности
- [ ] Load test: 1000 docs/hour sustained, p99 latency < 10 min
- [ ] Air-gap: система запускается на изолированном стенде без интернета
- [ ] Security: mTLS, JWT auth, encrypted storage, no PII in logs
- [ ] Monitoring: 100% stage-level metrics + global funnel + alerts
- [ ] Documentation: onboarding < 2 hours для нового senior-разработчика

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Air-gap provisioning неполный (забыли pip-пакет) | Высокая | Высокое | Comprehensive dependency manifest; offline smoke-test checklist |
| Production workload differs from synthetic | Высокая | Среднее | Gradual rollout; continuous HITL feedback; retraining |
| GPU contention при пиковой нагрузке | Средняя | Среднее | Rate limiting; queue-based admission control |

### Сложность
Высокая (DevOps + Security + Performance)

---

## Phase 10 — Data Pipeline & Continuous Learning

### Цель
Построить замкнутый цикл: HITL corrections → dataset → retrain → evaluate → promote → deploy, без участия человека в рутинных операциях.

### Задачи

| # | Задача | Описание |
|---|--------|----------|
| 10.1 | **Dataset versioning** | DVC или custom versioning на MinIO; каждый тренировочный запуск привязан к dataset version |
| 10.2 | **Automated retraining** | Cron/trigger: ≥N new corrections → retrain specialist (LayoutLMv3) → recalibrate confidence |
| 10.3 | **Gate evaluation** | Hold-out eval set; regression guard (F1, ECE); auto-rollback if gate fails |
| 10.4 | **Canary deployment** | New model version serves 5% traffic → monitor → promote/reject |
| 10.5 | **Active learning** | Uncertainty-based sampling: направлять на разметку документы с highest disagreement |
| 10.6 | **Synthetic data augmentation** | Генерация новых вариантов документов (разные шрифты, layout, языки) |

### Зависимости
Phase 7 (feedback loop), Phase 9 (production deployment).

### Ожидаемый результат
- Retraining pipeline работает без ручного вмешательства (automated)
- Model improvement visible: review rate decreases over time
- No regression: gate evaluation blocks bad models
- Active learning focuses labeling on most impactful documents

### Критерии готовности
- [ ] Retraining pipeline: correction → new model → deployed, fully automated
- [ ] Gate evaluation: blocks model with F1 regression > 2pp
- [ ] Canary: new model served to 5% traffic, auto-promoted if metrics improve
- [ ] Active learning: selects top-N uncertain docs for HITL review

### Риски
| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Overfitting to synthetic data | Средняя | Высокое | Real-world HITL data weighted higher; hold-out eval on diverse corpus |
| Retraining cost (GPU time) | Средняя | Низкое | Off-peak scheduling; LoRA fine-tuning for efficiency |

### Сложность
Средняя (MLOps pipeline, well-documented patterns)

---

# PART II — ROADMAP

```
Phase 0 ──── Phase 1 ──── Phase 2 ──── Phase 3 ──── Phase 4 ──── Phase 5 ──── Phase 6 ──── Phase 7 ──── Phase 8 ──── Phase 9 ──── Phase 10
Foundation   Skeleton    Classify     OCR         Layout      Extract     Validate     Routing     Orchestrator  Hardening    Continuous
                                                                                                               Learning
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
MVP ████████████████████████████████████████████████████                                                                      
                                                                                                              
Production Ready ████████████████████████████████████████████████████████████████████████████████████                               
                                                                                                                           
Enterprise █████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████
```

### Milestone Definitions

#### MVP (Phases 0–5)
- Ingestion + Normalization (все форматы)
- Classification + Text Quality
- OCR Ensemble + Consensus (3 движка)
- Layout + Tables + Figures
- Entity Extraction (dual-path: LayoutLMv3 + Qwen2.5-VL-32B)
- CanonicalDoc output (Markdown + JSON)
- **Чего НЕТ в MVP**: Confidence Fusion, RaV, HITL, Self-correct loop, Full Orchestrator
- **Цель**: доказать, что dual-path extraction с coarse-to-fine parsing работает точнее, чем одиночный подход

#### Production Ready (Phases 0–8)
- Всё из MVP +
- Reconciliation + Validation (format/cross-field/RaV)
- Confidence Fusion (CatBoost + isotonic calibration, ECE < 0.05)
- Routing + HITL (REST API + Swagger)
- Full Orchestrator (DAG, timeouts, fan-out/fan-in)
- Observability (Loki/Prometheus/Grafana/Tempo)
- CI/CD pipeline
- Security (mTLS, auth)
- **Цель**: система обрабатывает документы с автоматическим маршрутизацией и human-in-the-loop

#### Enterprise (Phases 0–10)
- Всё из Production Ready +
- Continuous Learning (retraining pipeline, gate evaluation, canary deployment)
- Active learning (uncertainty-based sampling)
- Air-gap provisioning (полностью изолированное развёртывание)
- Load testing (≥1000 docs/hour)
- Disaster recovery (backup/restore)
- Full documentation
- **Цель**: промышленная система с непрерывным улучшением, enterprise-grade reliability

---

# PART III — GIT REPOSITORY STRUCTURE

```
idp/
├── contracts/                          # Service contracts (§23)
│   └── protos/                         # gRPC proto definitions
│       ├── idp/
│       │   ├── envelope/v1/            # Common envelope
│       │   ├── ingestion/v1/           # S1
│       │   ├── render/v1/              # S2
│       │   ├── classify/v1/            # S3
│       │   ├── trust/v1/               # S4
│       │   ├── enhance/v1/             # S5
│       │   ├── ocr/v1/                 # S6
│       │   ├── consensus/v1/           # S7
│       │   ├── layout/v1/              # S8
│       │   ├── table/v1/               # S9
│       │   ├── figure/v1/              # S10
│       │   ├── semantic/v1/            # S11
│       │   ├── extract/v1/             # S12
│       │   ├── reconcile/v1/           # S13
│       │   ├── validate/v1/            # S14
│       │   ├── confidence/v1/          # S15
│       │   ├── routing/v1/             # S16
│       │   ├── feedback/v1/            # S17
│       │   └── orchestrator/v1/        # S18
│       └── buf.yaml, buf.gen.yaml      # Buf config
│
├── libs/
│   └── idp-common/                     # Shared Python library
│       ├── src/idp_common/
│       │   ├── envelope.py             # Envelope builder/parser
│       │   ├── kafka/                  # Producer/consumer wrappers
│       │   │   ├── producer.py
│       │   │   ├── consumer.py
│       │   │   └── saga.py             # Orchestrator state machine
│       │   ├── storage/
│       │   │   ├── minio.py            # Object storage client
│       │   │   ├── postgres.py         # DB client + models
│       │   │   └── redis.py            # Cache, locks, idempotency
│       │   ├── models/
│       │   │   ├── documents.py        # Domain models
│       │   │   ├── stages.py           # Stage result models
│       │   │   └── confidence.py       # Confidence signals
│       │   ├── retry/
│       │   │   ├── backoff.py          # Exponential backoff + jitter
│       │   │   ├── circuit_breaker.py  # Circuit breaker pattern
│       │   │   └── dead_letter.py      # DLQ producer
│       │   ├── observability/
│       │   │   ├── logging.py          # Structured JSON logging
│       │   │   ├── tracing.py          # OpenTelemetry integration
│       │   │   └── metrics.py          # Prometheus RED metrics
│       │   └── config.py               # Pydantic settings
│       ├── tests/
│       ├── pyproject.toml
│       └── Dockerfile
│
├── services/
│   ├── s1-ingestion/
│   ├── s2-normalize/
│   ├── s3-classify/
│   ├── s4-text-quality/
│   ├── s5-enhance/
│   ├── s6-ocr-ensemble/
│   ├── s7-ocr-consensus/
│   ├── s8-layout/
│   ├── s9-table/
│   ├── s10-figure/
│   ├── s11-semantic/
│   ├── s12-extraction/
│   ├── s13-reconcile/
│   ├── s14-validation/
│   ├── s15-confidence/
│   ├── s16-routing/
│   ├── s17-feedback/
│   └── s18-orchestrator/
│
├── infra/
│   ├── docker-compose.yml              # Local dev stack
│   ├── docker-compose.prod.yml         # Production overrides
│   ├── kubernetes/
│   │   ├── base/                       # Kustomize base
│   │   ├── overlays/
│   │   │   ├── dev/
│   │   │   └── prod/
│   │   └── helm/                       # Helm charts (alternative)
│   ├── terraform/                      # IaC (if cloud)
│   └── airgap/
│       ├── provisioning.sh             # Offline setup script
│       ├── wheelhouse/                 # Vendored pip packages
│       ├── models/                     # Pre-downloaded model weights
│       └── images/                     # Docker image tarballs
│
├── configs/
│   ├── profiles/                       # Doc-type processing profiles (YAML)
│   │   ├── invoice_v3.yaml
│   │   ├── contract_v2.yaml
│   │   └── generic.yaml
│   ├── schemas/                        # Extraction schemas (JSON Schema)
│   ├── rules/                          # Validation rules (CEL/Python)
│   └── calibration/                    # Isotonic regression curves
│
├── data/
│   ├── synthetic/                      # Synthetic generation scripts + output
│   ├── benchmarks/                     # DocLayNet, PubTables, DocVQA
│   └── labeled/                        # Human-labeled ground truth
│
├── models/
│   ├── configs/                        # Training configs (YAML)
│   ├── checkpoints/                    # Trained model artifacts
│   └── registry/                       # MLflow model registry metadata
│
├── tests/
│   ├── unit/                           # Per-service unit tests
│   ├── integration/                    # Cross-service tests
│   ├── contract/                       # gRPC contract tests
│   ├── e2e/                            # End-to-end pipeline tests
│   ├── performance/                    # Load tests (Locust/k6)
│   └── fixtures/                       # Test documents + ground truth
│
├── docs/
│   ├── architecture.md                 # Mermaid diagrams + resource table
│   ├── architecture-review.md          # Full review (Part I)
│   ├── implementation-plan.md          # This document
│   ├── api/                            # Auto-generated API docs
│   ├── runbooks/                       # Ops runbooks
│   └── onboarding/                     # Developer onboarding guide
│
├── scripts/
│   ├── setup.sh                        # Local dev environment setup
│   ├── generate_synthetic.py           # Synthetic data generator
│   ├── benchmark.py                    # Quality benchmark runner
│   └── calibrate.py                    # Confidence calibration script
│
├── .github/workflows/                  # CI/CD
├── Makefile                            # Dev commands
├── pyproject.toml                      # Monorepo workspace config
├── Dockerfile                          # Multi-stage base image
├── docker-compose.yml
├── README.md
└── .gitignore
```

### Per-Service Structure (typical)

```
services/s3-classify/
├── src/
│   ├── __init__.py
│   ├── service.py                      # FastAPI/gRPC server
│   ├── classifier.py                   # Core classification logic
│   ├── models/
│   │   ├── layoutlm_classifier.py     # LayoutLMv3 wrapper
│   │   ├── ood_detector.py            # Out-of-distribution
│   │   └── bio_labeler.py             # BIO sequence labeling
│   ├── schemas.py                      # Pydantic models
│   └── config.py                       # Service config
├── tests/
│   ├── test_classifier.py
│   ├── test_ood.py
│   └── fixtures/
├── Dockerfile
├── pyproject.toml
└── README.md
```

---

# PART IV — MICROSERVICES STRUCTURE

### Language & Framework

| Component | Language | Framework | Rationale |
|-----------|----------|-----------|-----------|
| All services | Python 3.12+ | FastAPI (sync/REST), FastStream (async/Kafka) | User choice; ML ecosystem |
| gRPC servers | Python | grpcio + generated stubs | Native proto support |
| gRPC clients | Python | grpcio async | Connection pooling, deadline propagation |
| Kafka producers/consumers | Python | aiokafka / FastStream | Async, backpressure-aware |
| ML inference (CPU) | Python | FastAPI + onnxruntime | Low-latency, no GPU |
| ML inference (GPU) | Python | vLLM (VLM), Triton (classical) | GPU-optimized serving |

### Service Communication Patterns

| Pattern | Usage | Implementation |
|---------|-------|----------------|
| Async event-driven | Primary orchestration (§23.3) | Kafka topics + FastStream consumers |
| Sync request-response | GPU inference, HITL API | gRPC (inter-service) + HTTP/REST (external) |
| Pub-sub | Observability, monitoring | Prometheus metrics, structured logs |

### Container Strategy

- **Base image**: `python:3.12-slim` + CUDA 12.4 (for GPU services)
- **ML images**: vLLM, PaddleOCR, etc. as separate base images
- **No monolith**: every service is a separate Docker image
- **Multi-stage builds**: compile deps in builder stage → copy to runtime (smaller images)
- **GPU support**: `nvidia-docker` runtime, `NVIDIA_VISIBLE_DEVICES=0|1` pinning

### Resource Limits (per service, Kubernetes)

| Service | CPU | Memory | GPU | Replicas |
|---------|-----|--------|-----|----------|
| S1 Ingestion | 2 | 2Gi | — | 2 |
| S2 Normalize | 8 | 4Gi | — | 4 |
| S3 Classify | 2 | 4Gi | 1 (GPU1) | 1 |
| S4 Text Quality | 4 | 2Gi | — | 2 |
| S5 Enhance | 4 | 4Gi | 0.5 (GPU1) | 2 |
| S6 OCR Ensemble | 4 | 8Gi | 1 (GPU1) | 1 |
| S7 Consensus | 2 | 2Gi | — | 2 |
| S8 Layout | 2 | 4Gi | 1 (GPU1) | 1 |
| S9 Table | 2 | 4Gi | 0.5 (GPU1) | 1 |
| S10 Figure | 2 | 4Gi | 0.5 (GPU1) | 1 |
| S11 Semantic | 2 | 4Gi | — | 2 |
| S12 Extract (A) | 2 | 4Gi | 1 (GPU1) | 1 |
| S12 Extract (B) | 4 | 16Gi | 1 (GPU0) | 1 |
| S13 Reconcile | 2 | 2Gi | — | 2 |
| S14 Validation | 2 | 4Gi | — | 2 |
| S15 Confidence | 2 | 2Gi | — | 2 |
| S16 Routing | 2 | 2Gi | — | 2 |
| S17 Feedback | 2 | 4Gi | — | 1 |
| S18 Orchestrator | 2 | 2Gi | — | 2 |

> **GPU scheduling note**: A100 40GB cards are **not** partitioned. S3/S5/S6/S8/S9/S10/S12A share GPU1 via CUDA_VISIBLE_DEVICES; S12B/S14 (VLM) use GPU0. Kubernetes must use `NVIDIA device plugin` with `nvidia.com/gpu` resource type.

---

# PART V — DOCKER COMPOSE (Local Development)

```yaml
# docker-compose.yml
version: "3.9"

services:
  # ── Infrastructure ──────────────────────────────
  kafka:
    image: confluentinc/cp-kafka:7.6.0
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_LOG_DIRS: /var/lib/kafka/data
      CLUSTER_ID: MkU3OEVBNTcwNTJENDM2Qk
    ports: ["9092:9092"]
    volumes: [kafka_data:/var/lib/kafka/data]
    healthcheck:
      test: kafka-topics --bootstrap-server localhost:9092 --list
      interval: 10s
      retries: 5

  zookeeper:
    image: confluentinc/cp-zookeeper:7.6.0
    environment:
      ZOOKEEPER_CLIENT_PORT: 2181
    volumes: [zk_data:/var/lib/zookeeper/data]

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: idp
      POSTGRES_USER: idp
      POSTGRES_PASSWORD: ${PG_PASSWORD:-changeme}
    ports: ["5432:5432"]
    volumes: [pg_data:/var/lib/postgresql/data]

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports: ["9000:9000", "9001:9001"]
    volumes: [minio_data:/data]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: [redis_data:/data]

  mlflow:
    image: ghcr.io/mlflow/mlflow:v2.16.0
    command: mlflow server --host 0.0.0.0 --port 5000 --backend-store-uri postgresql://idp:${PG_PASSWORD:-changeme}@postgres:5432/idp
    ports: ["5000:5000"]
    depends_on: [postgres]

  # ── Observability ────────────────────────────────
  loki:
    image: grafana/loki:3.1.0
    ports: ["3100:3100"]
    volumes: [./infra/loki-config.yml:/etc/loki/local-config.yaml]

  prometheus:
    image: prom/prometheus:v2.54.0
    ports: ["9090:9090"]
    volumes: [./infra/prometheus.yml:/etc/prometheus/prometheus.yml]

  grafana:
    image: grafana/grafana:11.2.0
    ports: ["3000:3000"]
    environment:
      GF_SECURITY_ADMIN_PASSWORD: admin
    volumes: [grafana_data:/var/lib/grafana]

  tempo:
    image: grafana/tempo:2.6.0
    ports: ["3200:3200"]

  # ── Services (CPU) ───────────────────────────────
  s1-ingestion:
    build: ./services/s1-ingestion
    ports: ["8001:8001", "9001:9001"]
    environment:
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
      PG_DSN: postgresql://idp:${PG_PASSWORD:-changeme}@postgres:5432/idp
      REDIS_URL: redis://redis:6379
    depends_on:
      kafka: { condition: service_healthy }
      postgres: { condition: service_healthy }
      minio: { condition: service_started }

  s2-normalize:
    build: ./services/s2-normalize
    environment:
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
      PG_DSN: postgresql://idp:${PG_PASSWORD:-changeme}@postgres:5432/idp
    depends_on: [kafka, postgres, minio]

  s4-text-quality:
    build: ./services/s4-text-quality
    environment:
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s7-ocr-consensus:
    build: ./services/s7-ocr-consensus
    environment:
      KAFKA_BROKER: kafka:9092
    depends_on: [kafka]

  s11-semantic:
    build: ./services/s11-semantic
    environment:
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s13-reconcile:
    build: ./services/s13-reconcile
    environment:
      KAFKA_BROKER: kafka:9092
    depends_on: [kafka]

  s14-validation:
    build: ./services/s14-validation
    environment:
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s15-confidence:
    build: ./services/s15-confidence
    environment:
      KAFKA_BROKER: kafka:9092
      PG_DSN: postgresql://idp:${PG_PASSWORD:-changeme}@postgres:5432/idp
    depends_on: [kafka, postgres]

  s16-routing:
    build: ./services/s16-routing
    ports: ["8016:8016"]
    environment:
      KAFKA_BROKER: kafka:9092
      PG_DSN: postgresql://idp:${PG_PASSWORD:-changeme}@postgres:5432/idp
    depends_on: [kafka, postgres]

  s18-orchestrator:
    build: ./services/s18-orchestrator
    environment:
      KAFKA_BROKER: kafka:9092
      PG_DSN: postgresql://idp:${PG_PASSWORD:-changeme}@postgres:5432/idp
      REDIS_URL: redis://redis:6379
    depends_on: [kafka, postgres, redis]

  # ── Services (GPU) ───────────────────────────────
  s3-classify:
    build: ./services/s3-classify
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      CUDA_VISIBLE_DEVICES: "1"
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s5-enhance:
    build: ./services/s5-enhance
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      CUDA_VISIBLE_DEVICES: "1"
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s6-ocr-ensemble:
    build: ./services/s6-ocr-ensemble
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      CUDA_VISIBLE_DEVICES: "1"
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s8-layout:
    build: ./services/s8-layout
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      CUDA_VISIBLE_DEVICES: "1"
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s9-table:
    build: ./services/s9-table
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      CUDA_VISIBLE_DEVICES: "1"
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s10-figure:
    build: ./services/s10-figure
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      CUDA_VISIBLE_DEVICES: "1"
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
    depends_on: [kafka, minio]

  s12-extraction:
    build: ./services/s12-extraction
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      CUDA_VISIBLE_DEVICES: "0"
      KAFKA_BROKER: kafka:9092
      MINIO_ENDPOINT: minio:9000
      VLM_MODEL_PATH: /models/Qwen2.5-VL-32B-AWQ
    depends_on: [kafka, minio]
    volumes:
      - /models:/models:ro

volumes:
  kafka_data:
  zk_data:
  pg_data:
  minio_data:
  redis_data:
  grafana_data:
```

---

# PART VI — KUBERNETES

### Kustomize Structure

```
infra/kubernetes/
├── base/
│   ├── namespace.yaml
│   ├── kafka/                    # Strimzi Kafka operator
│   ├── postgres/                 # StatefulSet + PVC
│   ├── minio/                    # StatefulSet + PVC
│   ├── redis/                    # StatefulSet
│   ├── mlflow/                   # Deployment + PVC
│   ├── observability/            # Loki, Prometheus, Grafana, Tempo
│   └── services/                 # All 18 services (Kustomize)
│       ├── s1-ingestion/
│       │   ├── deployment.yaml
│       │   ├── service.yaml
│       │   ├── hpa.yaml          # HorizontalPodAutoscaler
│       │   └── kustomization.yaml
│       ├── s2-normalize/
│       │   └── ...
│       └── ... (all 18)
│
└── overlays/
    ├── dev/
    │   ├── kustomization.yaml    # replicas=1, no GPU, debug logging
    │   └── patches/
    └── prod/
        ├── kustomization.yaml    # replicas=N, GPU, alerts, mTLS
        ├── network-policies.yaml
        ├── pod-disruption-budgets.yaml
        └── resource-quotas.yaml
```

### GPU Scheduling (Production)

```yaml
# Example: s3-classify deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: s3-classify
spec:
  replicas: 1  # GPU services: 1 replica per GPU
  template:
    spec:
      containers:
      - name: s3-classify
        resources:
          limits:
            nvidia.com/gpu: 1
          requests:
            cpu: "2"
            memory: "4Gi"
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: nvidia.com/gpu.product
                operator: In
                values: ["NVIDIA-A100-SXM4-40GB"]
        # Pin GPU1 services to same node
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchLabels:
                idp-gpu-pool: "gpu1"
            topologyKey: kubernetes.io/hostname
```

### HPA Configuration

```yaml
# CPU services: scale on CPU/memory
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: s2-normalize
spec:
  minReplicas: 2
  maxReplicas: 8
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Pods
    pods:
      metric:
        name: kafka_consumer_lag
      target:
        type: AverageValue
        averageValue: "100"
```

---

# PART VII — CI/CD

### Pipeline Stages

```
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│  Lint +   │→ │  Unit    │→ │ Contract │→ │   E2E    │→ │  Build + │
│  TypeCheck│  │  Tests   │  │  Tests   │  │  Tests   │  │  Push    │
└──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘
                                                              │
                                                              ▼
                                                       ┌──────────┐
                                                       │  Deploy  │
                                                       │  (manual │
                                                       │  gate)   │
                                                       └──────────┘
```

### Stage Details

| Stage | Tool | Trigger | Timeout | Gate |
|-------|------|---------|---------|------|
| Lint + TypeCheck | ruff, mypy | Every push | 5 min | Block merge |
| Unit Tests | pytest + coverage | Every push | 10 min | Block merge |
| Contract Tests | buf breaking + grpcurl | Every push | 5 min | Block merge |
| Integration Tests | pytest (docker-compose) | PR only | 20 min | Block merge |
| E2E Tests | pytest (full pipeline) | Nightly + manual | 60 min | Block release |
| Build + Push | Docker buildx | On merge to main | 15 min | Auto |
| Deploy (dev) | ArgoCD sync | Auto on main | 5 min | Auto |
| Deploy (prod) | ArgoCD sync | Manual approval | 10 min | Manual gate |

### Quality Gates

```yaml
quality_gates:
  unit_tests:
    coverage_threshold: 80%
    fail_on_regression: true
  lint:
    max_line_length: 120
    errors: 0
    warnings_threshold: 50
  mypy:
    strict: true
    errors: 0
  contract:
    breaking_changes: block
  e2e:
    pipeline_success_rate: 95%
    accuracy_on_golden_set: 85%
```

### Branch Strategy

- **trunk-based development** (single `main` branch)
- Feature branches: `feat/s3-classification`, `fix/ocr-timeout`
- PR required for all changes to `main`
- Auto-merge after CI passes + 1 review
- Release tags: `v1.0.0`, `v1.1.0` (semver)

---

# PART VIII — TESTING SYSTEM

### Test Pyramid

```
                    ┌─────────┐
                    │  E2E    │  (10 tests, nightly)
                    │  tests  │  Full pipeline: upload→output
                   ─┴─────────┴─
                  ┌─────────────┐
                  │ Integration  │  (50 tests, per PR)
                  │   tests      │  Cross-service via Kafka
                 ─┴──────────────┴─
                ┌──────────────────┐
                │   Contract tests  │  (100 tests, per push)
                │  (gRPC + Kafka)   │  Schema validation
               ─┴──────────────────┴─
              ┌────────────────────────┐
              │     Unit tests          │  (500+ tests, per push)
              │  Per-service, mocked    │  Logic, edge-cases
              └────────────────────────┘
```

### Test Categories

| Category | Scope | Runs | Assertion |
|----------|-------|------|-----------|
| **Unit** | Single function/class, mocked deps | Every push | Code coverage ≥ 80% |
| **Contract** | gRPC proto compatibility | Every push | buf breaking = 0 |
| **Integration** | 2+ services via Kafka (testcontainers) | Per PR | End-to-end flow |
| **E2E** | Full pipeline: upload → output | Nightly | Golden set accuracy |
| **Performance** | Load test: throughput, latency | Weekly | SLA thresholds |
| **Chaos** | Service crash, Kafka partition, GPU OOM | Monthly | Graceful degradation |
| **Calibration** | Confidence quality | Weekly | ECE < 0.05 |

### Golden Test Set

```
tests/fixtures/
├── pdf/
│   ├── digital/        # 20 documents
│   ├── scan/           # 20 documents
│   └── hybrid/         # 20 documents
├── image/
│   ├── tiff/           # 10 documents
│   ├── png/            # 10 documents
│   └── jpg/            # 10 documents
├── office/
│   ├── docx/           # 10 documents
│   ├── xlsx/           # 10 documents
│   └── pptx/           # 10 documents
├── web/
│   ├── html/           # 10 documents
│   └── email/          # 10 documents
└── ground_truth/
    ├── fields.json     # Expected extraction results
    └── layout.json     # Expected layout regions
```

### Test Infrastructure

- **Testcontainers** for Kafka, PG, Redis, MinIO (isolated per test run)
- **GPU mocking** for unit tests (mock inference server)
- **Synthetic document generator** for on-demand test data
- **Snapshot testing** for proto outputs and JSON schemas

---

# PART IX — MONITORING SYSTEM

### Metrics Hierarchy

```
RED (per service)          Domain metrics              System metrics
├── Rate (req/s)          ├── docs_in_queue            ├── GPU util (per card)
├── Errors (count)        ├── docs_per_state           ├── GPU VRAM used
└── Duration (latency)    ├── auto_accept_rate         ├── CPU/Memory per pod
                          ├── review_rate              ├── Kafka lag per consumer
                          ├── reject_rate              ├── MinIO disk usage
                          ├── self_correct_rate        ├── PG connections
                          ├── ocr_cer_avg              ├── Redis memory
                          ├── layout_map               ├── model_inference_ms
                          ├── table_teds               
                          ├── confidence_ece            Business metrics
                          ├── confidence_brier          ├── cost_per_document
                          ├── dual_path_agreement       ├── throughput_docs_per_hour
                          ├── raV_fidelity              ├── human_hours_saved
                          └── pipeline_success_rate     └── accuracy_trend
```

### Grafana Dashboards

| Dashboard | Contents |
|-----------|----------|
| **Document Funnel** | Documents at each pipeline stage; drop-off rates; stuck documents |
| **OCR Performance** | Per-engine CER, consensus vs single-engine; disagreement distribution |
| **Layout Quality** | mAP, reading-order accuracy, table-detection recall |
| **Extraction Quality** | Per-field F1, dual-path agreement, hallucination rate |
| **Confidence** | ECE, Brier, confidence distribution, calibration curves |
| **HITL** | Review queue depth, auto-accept rate, human throughput |
| **Infrastructure** | GPU util/VRAM, Kafka lag, PG/Redis health, MinIO disk |
| **SLA** | End-to-end latency p50/p95/p99, throughput, pipeline success rate |

### Alerting Rules

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| Pipeline stuck | Document in state > 5 min | Warning | Check service health |
| Pipeline stuck (critical) | Document in state > 30 min | Critical | Page on-call |
| GPU OOM | VRAM > 95% | Critical | Kill oldest inference |
| Confidence drift | ECE > 0.05 | Warning | Recalibrate |
| Confidence drift (critical) | ECE > 0.10 | Critical | Stop auto-accept |
| Kafka lag | Consumer lag > 1000 | Warning | Scale consumers |
| DLQ growing | DLQ messages > 100/hour | Warning | Investigate |
| Accuracy regression | F1 drop > 5pp vs baseline | Critical | Rollback model |
| DLQ spike | DLQ messages > 1000/hour | Critical | Service down |

### Log Schema

```json
{
  "timestamp": "2026-07-08T11:00:00Z",
  "level": "INFO",
  "trace_id": "abc123",
  "span_id": "def456",
  "doc_id": "uuid",
  "stage": "s6-ocr-ensemble",
  "attempt": 1,
  "service_version": "2.3.1",
  "latency_ms": 150,
  "outcome": "success",
  "engine": "paddleocr-vl",
  "gpu_id": 1,
  "message": "OCR completed for page 3, region r2"
}
```

> **PII policy**: document content NEVER appears in logs. Only refs (doc_id, page, region_id), hashes, and confidence scores.

---

# PART X — IMPLEMENTATION CHECKLIST

## Foundation (Phase 0)
- [ ] 0.1  Git monorepo initialized with branch strategy
- [ ] 0.2  Proto contracts frozen for all 18 services
- [ ] 0.3  Kafka Docker Compose + topic naming conventions
- [ ] 0.4  PostgreSQL schema + migrations (Alembic)
- [ ] 0.5  MinIO buckets + SDK wrapper
- [ ] 0.6  Redis config (dedup, idempotency, locks, circuit breaker)
- [ ] 0.7  MLflow offline mode configured
- [ ] 0.8  `idp-common` library (envelope, kafka, storage, retry, observability)
- [ ] 0.9  Docker Compose (full local stack)
- [ ] 0.10 CI/CD pipeline (lint, typecheck, test, build)
- [ ] 0.11 Observability stack (Loki, Prometheus, Grafana, Tempo)
- [ ] 0.12 Synthetic data generator (all formats + ground truth)

## Skeleton Pipeline (Phase 1)
- [ ] 1.1  S1 Ingestion (HTTP upload, SHA-256 dedup, MinIO, Kafka)
- [ ] 1.2  S2 Format Normalization (all 10 formats)
- [ ] 1.3  Basic Orchestrator (linear flow)
- [ ] 1.4  Contract tests (upload → output)
- [ ] 1.5  Synthetic test corpus (100 docs/type)

## Classification & Quality (Phase 2)
- [ ] 2.1  S3 Document Classification (LayoutLMv3, BIO, OOD)
- [ ] 2.2  S4 Text Layer Quality Estimator (5 detectors)
- [ ] 2.3  Classification model training
- [ ] 2.4  Text quality calibration dataset
- [ ] 2.5  Processing profiles (YAML schemas)

## OCR Pipeline (Phase 3)
- [ ] 3.1  S5 Image Enhancement (deskew, denoise, SR)
- [ ] 3.2  S6 OCR Ensemble (PaddleOCR-VL + Tesseract + TrOCR)
- [ ] 3.3  S7 OCR Consensus (ROVER + voting)
- [ ] 3.4  Per-engine calibration
- [ ] 3.5  Enhancement A/B evaluation

## Layout & Elements (Phase 4)
- [ ] 4.1  S8 Layout Reconstruction (RT-DETR + Pointer)
- [ ] 4.2  S9 Table Reconstruction (TATR/POTATR + cross-page)
- [ ] 4.3  S10 Figure Processing (chart/diagram/signature)
- [ ] 4.4  S11 Semantic Reconstruction (assembly + stitching)
- [ ] 4.5  DocLayNet benchmark (mAP ≥ 0.85)
- [ ] 4.6  PubTables-v2 benchmark (TEDS ≥ 0.85)

## Entity Extraction (Phase 5)
- [ ] 5.1  S12 Path A: LayoutLMv3 specialist
- [ ] 5.2  S12 Path B: Qwen2.5-VL-32B AWQ (vLLM, GPU0)
- [ ] 5.3  Extraction schemas (JSON Schema per doc-type)
- [ ] 5.4  LayoutLMv3 training pipeline
- [ ] 5.5  VLM prompt engineering + structured output
- [ ] 5.6  Grounding (bbox + page + citation)

## Validation & Confidence (Phase 6)
- [ ] 6.1  S13 Reconciliation (exact/fuzzy/numeric/semantic)
- [ ] 6.2  S14 Validation Engine (format + cross-field + RaV + external)
- [ ] 6.3  S15 Confidence Fusion (CatBoost + isotonic)
- [ ] 6.4  Calibration dataset (≥500 per doc-type)
- [ ] 6.5  RaV implementation
- [ ] 6.6  ECE/Brier evaluation pipeline

## Routing & HITL (Phase 7)
- [ ] 7.1  S16 Routing & HITL (confidence router + self-correct)
- [ ] 7.2  S17 Feedback/Training (corrections → retrain)
- [ ] 7.3  HITL REST API + Swagger UI
- [ ] 7.4  Output API (GET /documents/{id}/output)
- [ ] 7.5  Self-correct loop (max 2 retries)
- [ ] 7.6  Feedback loop (corrections → dataset → retrain → promote)

## Orchestrator (Phase 8)
- [ ] 8.1  S18 Full Orchestrator (DAG, timeouts, saga)
- [ ] 8.2  State machine (lifecycle transitions)
- [ ] 8.3  Fan-out/fan-in (parallel branches)
- [ ] 8.4  Observability dashboard (funnel + stuck detection)
- [ ] 8.5  Retry orchestration (self-correct triggers)

## Production Hardening (Phase 9)
- [ ] 9.1  Performance optimization (GPU batching, async I/O)
- [ ] 9.2  Security hardening (mTLS, JWT, encryption, PII scrubbing)
- [ ] 9.3  Air-gap provisioning (offline weights, wheels, images)
- [ ] 9.4  Monitoring & alerting (Grafana dashboards, Prometheus alerts)
- [ ] 9.5  Documentation (API docs, runbooks, onboarding)
- [ ] 9.6  Load testing (≥1000 docs/hour)
- [ ] 9.7  Disaster recovery (backup, restore, RTO/RPO)

## Continuous Learning (Phase 10)
- [ ] 10.1 Dataset versioning (DVC or custom)
- [ ] 10.2 Automated retraining pipeline
- [ ] 10.3 Gate evaluation (regression guard)
- [ ] 10.4 Canary deployment
- [ ] 10.5 Active learning (uncertainty sampling)
- [ ] 10.6 Synthetic data augmentation

---

# PART XI — DEPENDENCY GRAPH

```
Phase 0 (Foundation)
    │
    ├──→ Phase 1 (Skeleton) ──→ Phase 2 (Classify) ──→ Phase 3 (OCR)
    │                                                              │
    │                                                              ├──→ Phase 4 (Layout)
    │                                                              │         │
    │                                                              │         ├──→ Phase 5 (Extract)
    │                                                              │         │         │
    │                                                              │         │         ├──→ Phase 6 (Validate)
    │                                                              │         │         │         │
    │                                                              │         │         │         ├──→ Phase 7 (HITL)
    │                                                              │         │         │         │         │
    │                                                              │         │         │         │         ├──→ Phase 8 (Orchestrator)
    │                                                              │         │         │         │         │         │
    │                                                              │         │         │         │         │         ├──→ Phase 9 (Hardening)
    │                                                              │         │         │         │         │         │         │
    │                                                              │         │         │         │         │         │         ├──→ Phase 10 (Learning)
    │
    └──→ Phase 4 (Layout) [can start in parallel with Phase 3]
         (Layout models are independent of OCR ensemble)
```

### Parallelization Opportunities (20-person team)

| Phase | Serial dependency | Parallelizable work | Max parallel workers |
|-------|-------------------|---------------------|---------------------|
| 0 | Proto contracts → Common lib → Services | Infra (Kafka/PG/MinIO) ‖ Proto ‖ Synthetic data | 5 |
| 1 | S1 → S2 | S1 ‖ S2 (with mock input) | 4 |
| 2 | S3 + S4 (independent) | Classification model ‖ Quality dataset ‖ Profiles | 6 |
| 3 | S5 → S6 → S7 | S5 Enhancement ‖ S6 OCR Engines (parallel) | 6 |
| 4 | S8 → S9, S10, S11 | S8 Layout ‖ S9 Table ‖ S10 Figure (parallel after layout) | 8 |
| 5 | S12A ‖ S12B | Path A training ‖ Path B prompt engineering ‖ Schemas | 6 |
| 6 | S13 → S14 → S15 | Reconcile ‖ Validation rules ‖ Confidence model | 6 |
| 7 | S16 + S17 | HITL API ‖ Feedback pipeline | 4 |
| 8 | S18 (serial, depends on all) | Orchestrator ‖ Observability ‖ Documentation | 6 |
| 9 | All services built | Security ‖ Performance ‖ Air-gap ‖ Load test | 8 |

---

*End of implementation plan.*
