# Автономный конвейер PDF/DOCX → Markdown с VLM

Минимальный пайплайн: **PDF → VLM → Markdown → сущности**.
DOCX конвертируется в Markdown через `mammoth`.
Без PostgreSQL, MinIO, MinerU, PaddleOCR, SwinIR, Controller, Operator, Fenic.

## Режимы работы

| Режим | Команда | Что делает |
|---|---|---|
| Каталог | `idp` | Обрабатывает `data/input/` рекурсивно, складывает результаты в `data/output/` (`--artifacts` добавляет `<stem>.md`, `--force` переобрабатывает) |
| Оператор | `idp-operator --input page.pdf` | Постраничный review черновиков VLM, исправления идут в few-shot feedback для будущих прогонов (`--force` — ревьюить заново, не теряя правки) |
| Один файл | `idp-web --root data/ui --port 8080` | Веб-интерфейс: перетащил файл в бокс, нажал «Обработать»/«DOCX»/«PDF»/«TXT», скачал результат. Добавь `--stub`, чтобы работать без GPU (заглушка вместо VLM) |
| Сборка | `idp-reconstruct [--bundle DIR] [--format docx\|pdf\|vsdx]` | Пересобирает DOCX/PDF/Visio из бандла без VLM |
| Visio | `idp-visio --out drawing.vsdx <png-файлы или папка>` | Собирает Visio-чертеж (.vsdx) из PNG-картинок без VLM; `--mode document` — редактируемый документ из текста бандла |

Подробности и переменные окружения — в [docs/commands.md](docs/commands.md).

## Архитектура

| Сервис | Назначение | GPU |
|---|---|
| `vllm-vl` | Локальный vLLM/vLLM-совместимый бэкенд с VL-моделью | GPU0 |
| `sglang-vl` | Альтернативный SGLang-бэкенд для ускорения инференса | GPU0/GPU1 |
| `worker` | Python-код, монтируемый в контейнер | CPU |

## Результаты

В `data/output/` (пути повторяют структуру `data/input/`, поэтому одинаковые имена
файлов из разных подпапок не перезаписывают друг друга):
- `<относительный/путь>/<stem>.md` — реконструированный Markdown с сохранением абзацев (пишется только с `--artifacts`)
- `entities.json` — все сущности с привязкой к файлу-источнику
- `stats.jsonl` — статистика по каждому файлу
- `txts/<относительный/путь>/<stem>/` — бандл для восстановления DOCX/PDF:
  - `text.txt` — текст документа с гиперссылками на картинки: `[Image: <тип>: <описание> | bbox: ...]` и `[HANDWRITTEN: <текст> | bbox: ...]` превращаются в вырезанные из рендера страницы области `figure_*.png` / `handwritten_*.png`
  - `images/` — вырезанные области (`figure_*`, `handwritten_*`), рендеры страниц (`page_*`), встроенные в PDF картинки (`img_*`) и медиа из DOCX/PPTX/HTML; полно-страничные сканы в `img_*` не попадают
  - `tables.json` — таблицы структурированными строками (в `text.txt` они остаются markdown-таблицами)
  - `meta.json` — источник, тип, страницы, аннотация и `regions` (файл, тип области, страница, bbox 0..1, размеры в пикселях)
  - `reconstruct.docx` / `reconstruct.pdf` — собранные из бандла документы: Times New Roman 12, заголовки 14, полуторный интервал, картинки и таблицы по местам, разрывы страниц по маркерам (отключается через `IDP_RECONSTRUCT_FORMATS=`)
  - `reconstruct.vsdx` — Visio-чертеж из рендеров страниц (`--visio` у воркера или `IDP_RECONSTRUCT_FORMATS=docx,pdf,vsdx`): каждая страница PNG становится отдельной страницей Visio; с `IDP_VISIO_CONTENT=document` — редактируемый документ (текст, таблицы, рисунки)
- `rendered/<относительный/путь>/<stem>/` — рендеры страниц PDF (источник для `page_*` в бандлах)
- `entity_schema.json` — рабочая копия схемы сущностей; если VLM нашёл новый тип, он дописывается сюда (путь переопределяется через `IDP_ENTITY_SCHEMA_PATH`). Исходная `src/idp/entity_schema.json` остаётся шаблоном только для чтения
- `results.jsonl` / `results_readable.json` — результаты по каждому файлу

Идемпотентность: файл пропускается, если он уже есть в `results.jsonl` со статусом
`ok` или `skip` (полный сброс — `idp --force`).

## Модели

Скачай модели сам с HuggingFace и положи в:

```
transfer/models/
└── new_vl/  # VL-модель для реконструкции PDF → Markdown и извлечения сущностей
```

Каждая подпапка должна содержать полный набор файлов модели: `config.json`, `model.safetensors`, `tokenizer.json` и т.д.

Внутри подпапки должен лежать полный набор файлов скачанной модели, а не сама скачанная папка. Например:
```
transfer/models/new_vl/
├── config.json
├── model.safetensors.index.json
├── model-00001-of-00008.safetensors
├── model-00002-of-00008.safetensors
├── preprocessor_config.json
└── tokenizer.json
```

**Рекомендации по моделям:**

| GPU VRAM | VL-модель (в `new_vl/`) |
|---|---|
| 8 GB | `Qwen/Qwen2-VL-2B-Instruct` |
| 12 GB | `Qwen/Qwen3-VL-7B-Instruct` |
| 24 GB | `cyankiwi/Qwen3.8-27B-AWQ-INT4` |

## Деплой

### Windows (сборка и экспорт образов)

На Windows (PowerShell 7+):

```powershell
# Если модели на HuggingFace gated — передай токен
$env:HF_TOKEN = "hf_..."
.\scripts\export-images-linux.ps1
```

Скрипт собирает:
- `local/idp-app:latest` — worker с кодом
- `local/vllm-vl:latest` — vLLM VL с последней версией transformers
- `local/sglang:latest` — SGLang VL для ускоренного инференса

Все образы сохраняются в `transfer/idp-images-linux.tar`.

Перенеси папку проекта на Linux/WSL2.

### Linux (импорт и запуск)

```bash
chmod +x scripts/import-images-linux.sh
./scripts/import-images-linux.sh
```

По умолчанию импортируется `transfer/idp-images-linux.tar`. Если архив имеет другое имя:

```bash
./scripts/import-images-linux.sh idp-images-linux.tar
```

Скрипт загружает образы и поднимает контейнеры.

Или вручную:

```bash
docker build -t local/idp-app:latest .
docker build -t local/vllm-vl:latest ./infra/dockerfiles/vllm-vl
docker build -t local/sglang:latest ./infra/dockerfiles/sglang
docker compose -f infra/compose/local.yml up -d
```

## Подача документов

Скопируй PDF или DOCX в `data/input/` до или во время работы контейнера. Worker автоматически обработает новые файлы.

```bash
cp ~/Downloads/document.pdf data/input/
docker compose -f infra/compose/local.yml up -d
```

Если нужно обработать один файл «по кнопке», использй веб-интерфейс
`idp-web --root data/ui --port 8080`: файл перетаскивается в окно, далее кнопка
«Обработать всё» (все файлы сразу) или «Обработать» (отдельный файл), затем
«DOCX», «PDF», «TXT» и ссылки на скачивание.

`idp-web --stub --root data/ui --port 8080` — тот же интерфейс, но без GPU:
заглушка из `tests/vlm_stub.py` подменяет VLM, так что весь пайплайн работает офлайн.

## PNG → Visio (.vsdx)

Рендеры страниц (или любые PNG) собираются в Visio-чертеж без обращения к VLM:

```bash
# одна картинка — одна страница Visio
idp-visio --out drawing.vsdx data/output/rendered/report/page_00001.png

# целая папка с рендерами
idp-visio --out drawing.vsdx data/output/rendered/report/

# все страницы на одном листе (контактный лист)
idp-visio --out sheet.vsdx --mode sheet --columns 3 data/output/rendered/report/

# фиксированный формат страницы (дюймы), картинка вписывается с сохранением пропорций
idp-visio --out a4.vsdx --page-size 8.27x11.69 data/output/rendered/report/

# текст документа в редактируемый Visio (заголовки, списки, таблицы, картинки)
idp-visio --bundle data/output/txts/report.pdf --mode document --out report.vsdx
idp-visio --text report.md --out report.vsdx
```

Тот же флаг есть у воркера — `idp --visio` кладёт `reconstruct.vsdx` в бандл
каждого документа (страницы PNG → страницы Visio). Формат `.vsdx` пишется
только стандартной библиотекой, новых пакетов в air-gapped образ не нужно.
Готовый файл проверяется по официальной схеме VisioSchema15:
`python scripts/validate_visio_vsdx.py drawing.vsdx` (схема лежит в `tests/schemas/`).

В режиме `document` в чертеж попадает не картинка страницы, а сам документ:
заголовки и абзацы становятся текстовыми фигурами (их можно править в Visio),
таблицы — сеткой ячеек, рисунки — встроенными картинками. Содержимое чертежа
выбирается переменной `IDP_VISIO_CONTENT` (`pages` — по умолчанию, `sheet`,
`document`), она же действует для `idp-reconstruct --format vsdx`.

В Docker код подхватывается из смонтированной папки репозитория (`/workspace/src`),
так что пересборка образа не нужна — хватит `docker compose -f infra/compose/local.yml
restart worker`. Уже обработанные файлы пропускаются, поэтому для пересборки
нужен `--force`:

```bash
docker compose -f infra/compose/local.yml run --rm worker idp --visio --visio-content document --force
```

## Тесты (без GPU)

VLM подменяется заглушкой, тесты идут без видеокарты:

```bash
python3 -m pytest tests/ -q --timeout 150 --timeout-method=thread
```

## Запуск через SGLang

Для ускорения инференса можно использовать SGLang вместо vLLM. Поднимите основной стек, а поверх него добавьте `infra/compose/sglang.yml`:

```bash
docker compose -f infra/compose/local.yml -f infra/compose/sglang.yml up -d
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
- **RTX 5070 12GB** — модель Qwen3.8-27B-AWQ-INT4 требует обрезку контекста до 32768 токенов
