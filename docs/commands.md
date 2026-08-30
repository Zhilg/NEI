# Команды и настройка

## Быстрый старт

```bash
docker compose -f infra/compose/local.yml up -d
```

## Проверка статуса

```bash
docker compose -f infra/compose/local.yml ps
docker compose -f infra/compose/local.yml logs -f worker
```

## Остановка

```bash
docker compose -f infra/compose/local.yml down
```

## Репликация vLLM на 2 GPU

Сейчас настроено 2 независимых инстанса vLLM, каждый на своей видеокарте:

- **vllm-vl-0** — GPU 0, порт `8000`
- **vllm-vl-1** — GPU 1, порт `8001`

Worker балансирует запросы между ними через round-robin (`IDP_VL_ENDPOINTS`).

### Проверка что оба инстанса работают

```bash
curl http://localhost:8000/v1/models
curl http://localhost:8001/v1/models
```

Оба должны вернуть модель `Qwen2.5-VL-32B-Instruct-AWQ`.

## Настройка GPU

В `.env`:

```env
# Какую видеокарту использовать для worker (обычно 0)
IDP_WORKER_GPU=0

# Репликация: GPU для каждого vLLM инстанса
IDP_VLLM_VL_GPU_0=0
IDP_VLLM_VL_GPU_1=1
```

Если у тебя только одна карта, удали один из инстансов из `infra/compose/local.yml` и оставь только `vllm-vl-0`.

## Параметры vLLM

В `.env`:

```env
# Квантование модели (awq/gptq/none)
IDP_VLLM_QUANTIZATION=awq

# Тип данных (half/bfloat16)
IDP_VLLM_DTYPE=half

# Доля видеопамяти на модель (0.5-1.0)
IDP_VLLM_GPU_MEMORY_UTILIZATION=0.9

# Максимальная длина контекста в токенах
IDP_VLLM_MAX_MODEL_LEN=32768

# Фиксированный размер KV-cache в байтах
IDP_VLLM_KV_CACHE_MEMORY=15000000000
```

## Очередь запросов

В `.env`:

```env
# Количество одновременных запросов к VLM
IDP_VL_CONCURRENCY=8
```

## Вход/выход

В `.env`:

```env
IDP_INPUT_ROOT=../../data/input
IDP_OUTPUT_ROOT=../../data/output
IDP_MODELS_ROOT=../../transfer/models
```

## Режимы работы

### Без флага `--artifacts` (быстрый, экономия токенов)

```bash
docker compose -f infra/compose/local.yml up -d
```

В этом режиме:
- **Визуальные страницы PDF** → отправляются на VLM с промптом **только для извлечения сущностей** (entity-only). Markdown не генерируется.
- **Параграфы** берутся из нативного текстового слоя PDF.
- **Экономия**: ~30-40% токенов на визуальных страницах, так как VLM не тратит токены на реконструкцию markdown.

**Когда качество может упасть:**
- Сканированные PDF без текстового слоя → параграфы будут пустые
- PDF с таблицами, колонками, врезками → нативный текст не сохраняет reading order
- Документы с рукописным текстом → нативный текст его не извлекает

### С флагом `--artifacts` (качество, полный markdown)

```bash
docker compose -f infra/compose/local.yml up -d
# В другом терминале:
docker compose exec worker idp --artifacts
```

Или добавь в `.env`:
```env
IDP_ARTIFACTS_MODE=true
```

В этом режиме:
- **Визуальные страницы PDF** → комбинированный запрос: реконструкция markdown + извлечение сущностей
- **Параграфы** из VLM-реконструкции + нативного текста
- **Качество paragraphs**: максимальное, VLM понимает структуру, таблицы, reading order

## Настройка скорости и качества

### Увеличить скорость

В `.env`:

```env
# Больше изображений за один VLM-запрос (по умолчанию 2)
IDP_VL_MAX_IMAGES=2

# Больше параллельных запросов (по умолчанию 12, максимум 64)
IDP_VL_CONCURRENCY=16

# Для 2 GPU: убедись что оба используются
IDP_VLLM_VL_GPU_0=0
IDP_VLLM_VL_GPU_1=1
```

В `src/idp/config.py` можно также увеличить `vl_max_tokens` если нужно более длинный вывод.

### Увеличить качество

```env
# Меньше images per request → меньше шанса переполнения контекста
IDP_VL_MAX_IMAGES=1

# Меньше параллелизма → меньше нагрузки на GPU, стабильнее ответы
IDP_VL_CONCURRENCY=4

# Повысить уверенность модели (меньше мусора, но может терять редкие сущности)
IDP_MIN_ENTITY_CONFIDENCE=0.5
```

### Баланс

```env
IDP_VL_MAX_IMAGES=2
IDP_VL_CONCURRENCY=12
IDP_MIN_ENTITY_CONFIDENCE=0.3
```

## Результаты

- `data/output/results.jsonl` — одна строка на документ, для программного чтения
- `data/output/results_readable.json` — массив с pretty-print, для человека
- `data/output/entities.json` — все сущности сгруппированные по файлу
- `data/output/stats.jsonl` — статистика по обработке

## Проблемы

### Context length exceeded

Если видишь `Input length exceeds model's maximum context length`:

1. Уменьши `IDP_VL_MAX_IMAGES` до 1
2. Уменьши `IDP_VLLM_MAX_MODEL_LEN` до 16384 или 8192
3. Проверь что модель корректно сконвертирована в AWQ

### Медленная обработка

1. Увеличь `IDP_VL_CONCURRENCY` (по умолчанию 12)
2. Убедись что оба GPU используются (`nvidia-smi`)
3. Увеличь `IDP_VL_MAX_IMAGES` до 2-3 (если хватает контекста)

### Нет фамилий в сущностях

- Промпт уже усилен для извлечения ФИО без должностей
- Проверь что текст не обрезается при парсинге HTML
- Увеличь `IDP_MIN_ENTITY_CONFIDENCE` если слишком много мусора, или уменьши если теряются редкие сущности

### Много галлюцинаций

- Включи `--artifacts` — VLM генерирует entities с привязкой к реальному markdown
- Увеличь `IDP_MIN_ENTITY_CONFIDENCE` до 0.5-0.7
- Проверь `trash_path` — туда пишутся сырые ответы VLM для анализа

## Структура проекта

```
src/idp/           # Код пайплайна
  config.py        # Настройки (IDP_* env vars)
  worker.py        # Оркестратор
  vlm_client.py    # Клиент к vLLM, промпты, парсинг
  renderer.py      # PDF → PNG
  html_converter.py # HTML → текст
  docx_converter.py # DOCX → текст
  pptx_converter.py # PPTX → текст
  result_writer.py # Запись результатов
  entity_store.py  # Хранилище сущностей

infra/compose/
  local.yml        # Docker Compose для production
  local-test.yml   # Для тестов

data/
  input/           # П-drop файлов
  output/          # Результаты
```
