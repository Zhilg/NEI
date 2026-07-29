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

Скачай модели сам с HuggingFace и положи в:

```
transfer/models/
├── vl/    # VL-модель для реконструкции PDF → Markdown
└── llm/   # LLM для извлечения сущностей
```

Каждая подпапка должна содержать полный набор файлов модели: `config.json`, `model.safetensors`, `tokenizer.json` и т.д.

**Рекомендации по моделям:**

| GPU VRAM | VL-модель (в `vl/`) | LLM-модель (в `llm/`) |
|---|---|---|
| 8 GB | `Qwen/Qwen2-VL-2B-Instruct` | `Qwen/Qwen2.5-0.5B-Instruct` |
| 12 GB | `Qwen/Qwen2.5-VL-7B-Instruct` | `Qwen/Qwen2.5-7B-Instruct` |
| 24 GB | `Qwen/Qwen2.5-VL-32B-Instruct` | `Qwen/Qwen3-14B-Instruct` |

## Запуск

```bash
# 1. Собери worker-образ
docker build -t local/idp-app:latest .

# 2. Положи модели в transfer/models/vl/ и transfer/models/llm/
# 3. Подними контейнеры
docker compose -f infra/compose/local.yml up -d
```

## Подача документов

Скопируй PDF или DOCX в `data/input/` до или во время работы контейнера. Worker автоматически обработает новые файлы.

```bash
cp ~/Downloads/document.pdf data/input/
docker compose -f infra/compose/local.yml up -d
```

## Мониторинг

Worker выводит прогресс-бар с текущим файлом и этапом:
```
Files: 100%|████| 5/5 [03:24<00:00, 40.80s/file, file=doc.pdf, stage=done]
```

Логи:
```bash
docker compose -f infra/compose/local.yml logs -f worker
```

## Остановка

```bash
docker compose -f infra/compose/local.yml down
```

Данные сохраняются в `data/output/`.

## Важно

- **Контейнеры никогда не имеют доступа к интернету** — сеть `internal: true`
- **Никаких SHA-256, версионирования, whl-файлов** — всё максимально просто
- **Модели качаешь сам** — никакие скрипты это не делают
- **Linux vLLM-образы собираются с последней версией transformers** — достаточно `docker build`
- **RTX 5070 12GB** — модели (Qwen2.5-VL-32B-AWQ + Qwen3-14B-AWQ) требуют AWQ-квантизацию и обрезку контекста до 32768 токенов
