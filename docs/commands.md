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

### Смена модели (например, для сравнения скорости)

```bash
# скачать другой билд рядом с основным
python scripts/download_vl_model.py --repo cyankiwi/Qwen3.8-7B-AWQ-INT4 --model-dir new_vl_7b

# запустить стек на ней
IDP_VL_MODEL_DIR=new_vl_7b IDP_VL_MODEL=Qwen3.8-7B \\
  docker compose -f infra/compose/local.yml up -d
```

`IDP_VL_MODEL_DIR` переключает папку `/models/<dir>` в обоих инстансах vLLM/SGLang
(и их `HF_HOME`), `IDP_VL_MODEL` — имя модели, которое воркер подставляет в запросы.
Оба параметра работают и для оверлея `sglang.yml`.

Сравнить поведение моделей (latency, decode tok/s, страницы/с) можно бенчмарком:

```bash
python scripts/bench_vlm.py \\
  --endpoint http://localhost:8000/v1 --model Qwen3.8-27B \\
  --pages data/output/rendered/<путь>/<stem>/page_0000*.png \\
  --images-per-request 2 --repeat 3 --report data/bench_27b.jsonl

python scripts/bench_vlm.py \\
  --endpoint http://localhost:8000/v1 --model Qwen3.8-7B \\
  --pages data/output/rendered/<путь>/<stem>/page_0000*.png \\
  --images-per-request 2 --repeat 3 --report data/bench_7b.jsonl
```

Если страницы не указаны, бенчмарк генерирует три тестовых PNG сам.
Важно: бенчмарк стримит ответ (`stream: true`) vLLM/SGLang умеют это из коробки.

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
| `IDP_VL_MODEL_DIR` | `new_vl` | папка модели внутри `/models` для vLLM/SGLang (compose) |
| `IDP_VL_MODEL` | `Qwen3.8-27B` | имя модели в запросах к VLM (compose) |
| `IDP_VL_MAX_IMAGES` | `2` | сколько страниц отправлять в одном VLM-запросе (compose) |

## CLI (консольный интерфейс)

Все точки входа объявлены в `pyproject.toml` (`[project.scripts]`): `idp`, `idp-web`,
`idp-reconstruct`, `idp-operator`, `idp-finetune-dataset`. Работают и в контейнере,
и на хосте (после `pip install -e .`).

CLI — полноценная замена веб-интерфейсу: `idp-web` внутри вызывает тот же `Worker`,
что и `idp`. Если оболочка не поднялась, все задачи доступны из консоли.

### Прогнать все файлы

`idp` обрабатывает **каталог** `IDP_INPUT_ROOT` целиком, идемпотентно (файл пропускается,
если он в `results.jsonl` со статусом `ok`/`skip`):

```bash
docker compose -f infra/compose/local.yml run --rm worker idp --artifacts
docker compose -f infra/compose/local.yml run --rm worker idp --force     # переобработать всё
docker compose -f infra/compose/local.yml exec worker idp --artifacts     # в уже запущенном контейнере
```

| Флаг | Действие |
|---|---|
| `--artifacts` | дополнительно писать `<stem>.md` (без него `.md` на диск не сохраняется) |
| `--force` | игнорировать кэш обработанных и переобработать всё заново |

Каталог задаётся переменной окружения: `IDP_INPUT_ROOT` / `IDP_OUTPUT_ROOT`
(в `local.yml` это `/input` и `/output`, смонтированные из `data/input` и `data/output`).

### Один файл

Отдельного флага для одного файла в `idp` **нет** — воркер всегда берёт каталог. Два варианта:

```bash
# вариант 1: положить файл в подкаталог и направить воркер туда
mkdir -p data/input/single && cp отчёт.pdf data/input/single/
docker compose -f infra/compose/local.yml run --rm -e IDP_INPUT_ROOT=/input/single worker idp --artifacts

# вариант 2: веб-интерфейс — туда файл перетаскивают мышью (см. ниже)
idp-web --root data/ui --port 8080
```

### Собрать PDF / DOCX / TXT из готовых бандлов

DOCX и PDF пересобираются из бандла `txts/<имя файла>/` **без обращения к VLM**
(поэтому это работает даже когда модель недоступна):

```bash
docker compose -f infra/compose/local.yml run --rm worker idp-reconstruct                        # все бандлы
docker compose -f infra/compose/local.yml run --rm worker idp-reconstruct --bundle data/output/txts/scan.pdf
docker compose -f infra/compose/local.yml run --rm worker idp-reconstruct --format pdf           # docx, pdf или оба
```

| Флаг | Действие |
|---|---|
| `--bundle <путь>` | пересобрать один бандл |
| `--root <путь>` | корень бандлов, по умолчанию `<output_root>/txts` |
| `--format` | `docx`, `pdf` или `docx,pdf`; пусто — оба |

**TXT пересобрать нельзя** — `text.txt` уже лежит в бандле и пишется во время обработки;
`idp-reconstruct` умеет только `docx` и `pdf`.

### CLI без GPU

Воркер — обычный HTTP-клиент к VLM (в образе нет `torch`/CUDA), поэтому консольные
команды запускаются без видеокарты:

```bash
docker compose -f infra/compose/local.yml run --rm --no-deps worker idp --help
docker compose -f infra/compose/local.yml run --rm --no-deps worker idp-reconstruct --help
```

`--no-deps` не поднимает `vllm-vl-0/1`, так что команда не падает даже если GPU недоступна.

## Веб-интерфейс для отдельных файлов

Отдельный режим для одного документа: файл перетаскивают в окно браузера и нажимают
кнопки — пайплайн обрабатывает его без просмотра каталогов.

```bash
# на хосте (нужны зависимости проекта: pip install -e .)
idp-web --root data/ui --host 127.0.0.1 --port 8080

# в контейнере: сервис web из infra/compose/local.yml
docker compose -f infra/compose/local.yml up -d web
```

Чтобы запустить интерфейс без GPU (с заглушкой вместо vLLM), добавьте `--stub`:

```bash
idp-web --stub --root data/ui --port 8080
```

Заглушка `tests/vlm_stub.py` стартует в процессе и отдаёт детерминированный markdown/JSON,
так что весь пайплайн (render → VLM → entities → bundle → reconstruct) работает офлайн.

Отдельный `docker-compose.yml` в корне репо поднимает интерфейс и заглушку на образе
`worker:latest` — без GPU и без `infra/compose/local.yml`:

```bash
docker compose up -d                       # прод, VLM по умолчанию http://vllm-vl:8000/v1
docker compose --profile stub up -d        # интерфейс + vlm-stub
```

Заглушка включается **только** через `--stub` / профиль `stub`; в `infra/compose/local.yml`
её нет вообще, поэтому на прод-стек она не влияет.

Что умеет:

- принять файл (drag & drop или выбор через проводник): PDF, DOCX, XLSX, PPTX, HTML, изображения;
- кнопка **Обработать всё** — прогоняет пайплайн по всем dropped-файлам сразу (Markdown + DOCX/PDF по умолчанию);
- кнопка **Обработать** — обрабатывает отдельный файл;
- кнопки **DOCX / PDF / TXT** — пересобирают выбранный артефакт из бандла, не запуская VLM повторно;
- ссылки на скачи Markdown, TXT-бандла и `reconstruct.docx` / `reconstruct.pdf`.

Сервер работает только на стандартной библиотеке (новых пакетов в air-gapped образ
не добавляется), файлы и результаты складываются под `--root` (`uploads/<id>/`,
`output/<id>/`).

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

### Сравнение моделей (27B против маленькой)

Скорость в первую очередь зависит от размера модели, поэтому рядом с 27B можно
поставить компактный билд и сравнить пропускную способность (см. «Смена модели»):

```bash
# оконный стенд: измерить текущую скорость
python scripts/bench_vlm.py --endpoint http://localhost:8000/v1 --model Qwen3.8-27B \
  --pages data/output/rendered/<путь>/<stem>/page_0000*.png --repeat 3

# поменять модель в .env и повторить
IDP_VL_MODEL_DIR=new_vl_7b IDP_VL_MODEL=Qwen3.8-7B docker compose -f infra/compose/local.yml up -d
python scripts/bench_vlm.py --endpoint http://localhost:8000/v1 --model Qwen3.8-7B \
  --pages data/output/rendered/<путь>/<stem>/page_0000*.png --repeat 3
```

Смотрите `pages/s` и `tokens_per_sec` между прогонами, а качество — глазами по
`data/output/*.md` на одном и том же же наборе файлов.

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

## Тесты (без GPU)

Тесты не требуют видеокарты: VLM подменяется заглушкой `tests/vlm_stub.py`
(OpenAI-совместимый сервер, отдаёт детерминированный markdown/JSON на базе хешей
присланных страниц), входные PDF/DOCX/HTML генерируются в `tests/fixtures.py`.

```bash
python3 -m pip install -e ".[dev]"     # pymupdf, pillow, mammoth, docx, pptx, httpx, pytest, pytest-timeout
python3 -m pytest tests/ -q --timeout 150 --timeout-method=thread
```

Покрыто: сборка txts-бандлов и reconstruct.docx/pdf на заглушке (включая 40-страничный
документ — все заголовки должны дожить до вывода), wide-таблицы (краш
`text box must be finite and not empty`), склейка перенесённых ссылок, поток оператора
(`--force`, EOF, пробелы), бенчмарк и веб-интерфейс (upload/process/formats/download).

Заглушку можно запустить и вручную, чтобы дергать воркер/оператор без vLLM:

```bash
python3 tests/vlm_stub.py --port 8123
IDP_VL_ENDPOINTS=http://127.0.0.1:8123/v1 IDP_VL_MODEL=stub-vl idp --artifacts
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
- таблицы — сетка, шапка полужирная, интервал 1.0; ширина столбцов подбирается автоматически
  по содержимому (с минимумом по ширине), поэтому длинная ячейка не сплющивает остальные
  столбцы, а очень широкая таблица не роняет сборку
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
  webui.py         # Веб-интерфейс для отдельных файлов (stdlib http.server)
  prompts.py       # Промпты для VLM

infra/compose/
  local.yml        # Docker Compose (worker + 2 инстанса vLLM VL)
  sglang.yml       # Оверлей: замена vLLM на SGLang

scripts/
  download_vl_model.py # Скачивание модели (--repo/--model-dir для другой сборки)
  bench_vlm.py         # Замер скорости VLM (TTFT, tok/s, pages/s)

tests/               # Офлайн-тесты: VLM-заглушка вместо GPU (tests/vlm_stub.py)
  fixtures.py       # Генераторы тестовых PDF/DOCX/HTML/PPTX
  conftest.py       # Старт заглушки и изолированные input/output

data/
  input/           # Каталог входных файлов
  output/          # Результаты (структура повторяет input)
```
