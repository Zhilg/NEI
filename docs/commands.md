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

Оба должны вернуть модель `Qwen3.8-27B`.

## Запуск через SGLang

SGLang может дать более высокий throughput при той же модели. Для этого используйте дополнительный compose-файл поверх `infra/compose/local.yml`:

```bash
docker compose -f infra/compose/local.yml -f infra/compose/sglang.yml up -d
```

По умолчанию используется образ `${IDP_SGLANG_IMAGE:-lmsysorg/sglang:latest}`. Модель лежит в `transfer/models/new_vl/`, внутри контейнера это `/models/new_vl`.

Этот оверлей переключает существующие сервисы `vllm-vl-0` и `vllm-vl-1` на SGLang, не создавая дополнительных контейнеров. Порты остаются `8000` и `8001`.

### Проверка

```bash
curl http://localhost:8000/v1/models
curl http://localhost:8001/v1/models
```

## Модель

Модель скачивается скриптом:
```bash
python scripts/download_vl_model.py
```

Модель попадает в `transfer/models/new_vl/`, а compose подключает её как `/models/new_vl`.

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

### Без флага `--artifacts` (быстрый, экономия диска)

```bash
docker compose -f infra/compose/local.yml up -d
```

В этом режиме pipeline выполняет всё, кроме записи `<stem>.md`: рендерит страницы,
строит Markdown, извлекает сущности, пишет бандлы и собирает reconstruct.docx/pdf,
но сам `.md` на диск не сохраняется.

### С флагом `--artifacts` (полный markdown)

```bash
docker compose -f infra/compose/local.yml up -d
# В другом терминале:
docker compose exec worker idp --artifacts
```

Или добавь в `.env`:
```env
IDP_ARTIFACTS_MODE=true
```

В этом режиме дополнительно пишется `<stem>.md` — реконструированный Markdown
(текстовый слой PDF никогда не читается, всё идёт через VLM).

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `IDP_ENTITY_SCHEMA_PATH` | `<output_root>/entity_schema.json` | куда дописывать новые типы сущностей (исходная схема в `src/idp/` — шаблон только для чтения) |
| `IDP_LOG_LEVEL` | `INFO` | уровень логов (`DEBUG` включает размеры страниц, URL и превью ответов VLM) |
| `IDP_ARTIFACTS_MODE` | `false` | аналог флага `--artifacts` |
| `IDP_RECONSTRUCT_FORMATS` | `docx,pdf` | какие форматы собирать из бандлов (пусто — не собирать) |

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

Пути вывода повторяют структуру `data/input/`:

- `data/output/<путь>/<stem>.md` — реконструированный Markdown (только с `--artifacts`)
- `data/output/results.jsonl` — одна строка на документ, для программного чтения
- `data/output/results_readable.json` — pretty-print, для человека (перезаписывается один раз за прогон)
- `data/output/entities.json` — все сущности, сгруппированные по файлу (сбрасывается на диск раз в прогон и каждые 50 файлов)
- `data/output/stats.jsonl` — статистика по обработке
- `data/output/rendered/<путь>/<stem>/` — рендеры страниц PDF
- `data/output/txts/<путь>/<stem>/` — бандл для восстановления DOCX/PDF: `text.txt` (текст + гиперссылки на картинки), `images/` (рендеры страниц и встроенные изображения), `tables.json`, `meta.json`, `reconstruct.docx`, `reconstruct.pdf`
- `data/output/entity_schema.json` — рабочая копия схемы сущностей (см. `IDP_ENTITY_SCHEMA_PATH`)

Повторный запуск пропускает файлы, которые уже есть в `results.jsonl` со статусом
`ok` или `skip`; `idp --force` обрабатывает всё заново.

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

## Области изображений в бандлах

VLM размечает каждую картинку и рукописный фрагмент bbox'ом (0..1000, начало координат — верхний левый угол страницы), после чего область вырезается из рендера страницы:

```
[Image: photo: портрет Иванова в рамочке | bbox: 121 143 793 618]
Сумма [HANDWRITTEN: 1234 | bbox: 610 60 780 110] рублей.
```

В `text.txt` эти маркеры становятся ссылками на вырезанные файлы: `![...](images/figure_0001.png)` и `[HANDWRITTEN: ...](images/handwritten_0001.png)`. В `<stem>.md` bbox-аннотации вырезаются, текстовые потребители (абзацы, сущности) видят чистый текст. Полные страницы (`page_*`) остаются файлами в `images/` и в `meta.json` → `regions`, но инлайном в текст не подставляются.

## Оформление DOCX/PDF

Собранные документы идут в стандартном виде:

- Times New Roman 12 pt, полуторный межстрочный интервал, поля 2 см
- заголовки 14 pt полужирные, уровень сохраняется в структуре документа
- абзацы — выключка по ширине и красная строка 1.25 см, списки со смещением
- таблицы — сетка, шапка полужирная, интервал 1.0
- рукописные области вставляются картинками, транскрипция идёт курсивной подписью
- размер картинки берётся из её bbox в `meta.json` → `regions` (доля страницы),
  а не угадывается по DPI; там же лежат пиксельные размеры и позиция
- перенос строк внутри ссылки не ломает картинку (`![alt](images/\nfig.png)` склеивается обратно)
- VLM просят размечать заголовки (`#`/`##`) и списки, маркеры списков и нумерация
  больше не вырезаются постобработкой

Шрифт для PDF — Liberation Serif (метрически совместим с Times New Roman, есть кириллица), лежит в `src/idp/fonts/`; при наличии системного Liberation/DejaVu Serif используется он.

## Восстановление DOCX/PDF из бандлов

Каждый бандл `txts/<имя файла>/` после обработки автоматически собирается обратно в документы:

```bash
docker compose exec worker idp-reconstruct                     # все бандлы в data/output/txts
docker compose exec worker idp-reconstruct --bundle data/output/txts/scan.pdf
docker compose exec worker idp-reconstruct --format pdf        # только PDF (или docx)
```

В бандле появятся `reconstruct.docx` и `reconstruct.pdf` с картинками по местам, заголовками, списками, таблицами (с повторением шапки при переносе) и разрывами страниц по маркерам `<!-- page N -->`.

Отключить авто-сборку в воркере:

```env
IDP_RECONSTRUCT_FORMATS=
```

## Структура проекта

```
src/idp/           # Код пайплайна
  config.py        # Настройки (IDP_* env vars)
  worker.py        # Оркестратор
  vlm_client.py    # Клиент к vLLM, промпты, парсинг
  renderer.py      # PDF → PNG
  html_converter.py # HTML → markdown
  docx_converter.py # DOCX → markdown
  pptx_converter.py # PPTX → markdown
  document_classifier.py  # Определение типа документа
  result_writer.py # Запись результатов
  stats_writer.py  # stats.jsonl
  entity_store.py  # Хранилище сущностей
  feedback_store.py # Коррекции оператора (few-shot для VLM)
  finetune_dataset.py # Датасет дообучения из коррекций
  metadata_extractor.py # Метаданные DOCX/PPTX
  txt_export.py    # Экспорт txts-бандлов с гиперссылками на картинки
  reconstructor.py # Сборка DOCX/PDF из бандлов
  operator_client.py # Терминальный инструмент review'а оператора
  prompts.py       # Промпты для VLM

infra/compose/
  local.yml        # Docker Compose (worker + 2 инстанса vLLM VL)
  sglang.yml       # Оверлей: замена vLLM на SGLang

data/
  input/           # Каталог входных файлов
  output/          # Результаты (структура повторяет input)
```
