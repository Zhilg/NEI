# Автономный конвейер PDF/DOCX → Markdown с VLM

Минимальный пайплайн: **PDF → VLM → Markdown → LLM → сущности**.
DOCX конвертируется в Markdown через `mammoth`.
Без PostgreSQL, MinIO, MinerU, PaddleOCR, SwinIR, Controller, Operator, Fenic.

## Архитектура

| Сервис | Назначение | GPU |
|---|---|---|
| `vllm-vl` | Локальный vLLM с VL-моделью | GPU0 |
| `vllm-llm` | Локальный vLLM с LLM для сущностей | GPU1 |
| `worker` | Python-код, монтируемый в контейнер | CPU |

## Результаты

В `data/output/`:
- `<stem>.md` — реконструированный Markdown с сохранением абзацев
- `entities.jsonl` — все сущности с привязкой к параграфу (append-only)
- `stats.jsonl` — статистика по каждому файлу

Идемпотентность: если `<stem>.md` уже существует, файл пропускается.

## Модели

### Сценарий 1: Linux production (нормальные модели)

**Скрипт `export-images-windows.ps1` собирает образы для обоих сценариев:**
- **Windows E2E** — tiny-модели запекаются в образы во время сборки
- **Linux production** — Linux vLLM-образы собираются с последней версией transformers, модели должны быть уже в `transfer/models/`

```
transfer/
├── idp-images.tar          # все образы в одном архиве
├── idp-images.tar.json     # метаданные
├── EXPORT-COMPLETE.txt     # маркер завершения
└── models/
    ├── vl/                 # сюда содержимое папки VL-модели (веса, config.json и т.д.)
    └── llm/                # сюда содержимое папки LLM-модели
```

Если модели gated — используй `huggingface-cli login` или переменную `HF_TOKEN`.

**Каждая подпапка должна содержать полный набор файлов модели:** `config.json`, `model.safetensors`, `tokenizer.json` и т.д.

**Рекомендации по моделям:**

| GPU VRAM | VL-модель (в `vl/`) | LLM-модель (в `llm/`) |
|---|---|---|
| 8 GB | `Qwen/Qwen2-VL-2B-Instruct` | `Qwen/Qwen2.5-0.5B-Instruct` |
| 12 GB | `Qwen/Qwen2.5-VL-7B-Instruct` | `Qwen/Qwen2.5-7B-Instruct` |
| 24 GB | `Qwen/Qwen2.5-VL-32B-Instruct` | `Qwen/Qwen3-14B-Instruct` |

### Сценарий 2: Windows E2E тестирование (крошечные модели запечены в образы)

Для end-to-end тестирования используются маленькие модели, уже запечённые в Docker-образы. Никаких монтирований не требуется.

## Запуск

### Linux production

```bash
# 1. Собери worker-образ
docker build -t local/idp-app:latest .

# 2. Положи модели в transfer/models/vl/ и transfer/models/llm/
# 3. Подними контейнеры
docker compose -f infra/compose/local.yml --profile linux up -d
```

### Windows E2E

На Windows собери и экспортируй образы:

```powershell
# Если модели на HuggingFace gated — передай токен
$env:HF_TOKEN = "hf_..."
.\scripts\export-images-windows.ps1
```

Перенеси папку проекта на Linux/WSL2 и импортируй:

```bash
chmod +x scripts/import-images-linux.sh
./scripts/import-images-linux.sh win-test
```

Или напрямую:
```bash
docker compose -f infra/compose/local.yml --profile win-test up -d
```

## Переключение между сценариями

```bash
# Linux production
docker compose -f infra/compose/local.yml --profile linux up -d

# Windows E2E
docker compose -f infra/compose/local.yml --profile win-test up -d
```

По умолчанию (без профиля) ничего не запускается.

### GPU устройства

По умолчанию все сервисы используют GPU 0. Если у тебя несколько GPU, переопредели:

```bash
IDP_WORKER_GPU=0 IDP_VLLM_VL_GPU=0 IDP_VLLM_LLM_GPU=1 docker compose -f infra/compose/local.yml --profile linux up -d
```

## Подача документов

Скопируй PDF или DOCX в `data/input/` до или во время работы контейнера. Worker автоматически обработает новые файлы.

```bash
cp ~/Downloads/document.pdf data/input/
docker compose -f infra/compose/local.yml --profile linux up -d
```

## Мониторинг

Worker выводит прогресс-бар с текущим файлом и этапом:
```
Files: 100%|████| 5/5 [03:24<00:00, 40.80s/file, file=doc.pdf, stage=done]
```

Логи:
```bash
docker compose -f infra/compose/local.yml --profile linux logs -f worker
```

## Остановка

```bash
docker compose -f infra/compose/local.yml --profile linux down
```

Данные сохраняются в `data/output/`.

## Важно

- **Контейнеры никогда не имеют доступа к интернету** — сеть `internal: true`
- **Никаких SHA-256, версионирования, whl-файлов** — всё максимально просто
- **Скрипт собирает образы для Windows E2E и Linux** — просто запусти `export-images-windows.ps1`
- **RTX 5070 12GB** — win-test модели (Qwen2-VL-2B + Qwen2.5-0.5B-Instruct) влезают comfortably. Linux модели (Qwen2.5-VL-32B-AWQ + Qwen3-14B-AWQ) требуют AWQ-квантизацию и обрезку контекста до 32768 токенов
