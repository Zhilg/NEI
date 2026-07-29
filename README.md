# Автономный конвейер PDF → Markdown с VLM

Минимальный пайплайн: **PDF → VLM → Markdown → LLM → сущности**.
DOCX конвертируется в Markdown через `mammoth`.
Без PostgreSQL, MinIO, MinerU, PaddleOCR, SwinIR, Controller, Operator, Fenic.

## Архитектура

| Сервис | Назначение | GPU |
|---|---|---|
| `vllm-vl` | Локальный vLLM с VL-моделью | GPU0 |
| `vllm-llm` | Локальный vLLM с LLM для сущностей | GPU1 |
| `worker` | Python-код, монтируемый в контейнер | CPU |

## Запуск

```bash
docker compose -f infra/compose/local.yml up -d
```

Контейнеры всегда работают без интернета. Docker-сеть имеет `internal: true`, что блокирует весь внешний egress.

## Результаты

В `data/output/`:
- `<stem>.md` — реконструированный Markdown
- `entities.jsonl` — все сущности (append-only)
- `stats.jsonl` — статистика по каждому файлу

Идемпотентность: если `<stem>.md` уже существует, файл пропускается.

## Два сценария развёртывания

### 1. Linux production (модели на хосте)

Модели монтируются read-only из `transfer/models/`:
```
transfer/models/
  vl/    # VL-модель для Markdown
  llm/   # LLM для извлечения сущностей
```

Подготовьте на хосте каталоги:
```
data/input/      # PDF и DOCX для обработки
data/output/     # результаты
transfer/models/ # модели
```

Запуск:
```bash
docker compose -f infra/compose/local.yml --profile linux up -d
```

Импорт на Linux (если используете архив с Windows):
```bash
chmod +x scripts/import-images-linux.sh
./scripts/import-images-linux.sh linux
```

### 2. Windows E2E тестирование (модели внутри образов)

Для end-to-end тестирования на Windows используются маленькие модели, запечённые прямо в Docker-образы. Никаких монтирований моделей не требуется.

Подготовьте на хосте каталоги:
```
data/input/      # PDF и DOCX для обработки
data/output/     # результаты
```

Экспорт с Windows:
```powershell
.\scripts\export-images-windows.ps1
```

Скрипт собирает:
- `local/idp-app:latest` — worker с кодом
- `local/vllm-win-vl:latest` — vLLM с `Qwen2.5-VL-2B-Instruct` (запечена)
- `local/vllm-win-llm:latest` — vLLM с `Qwen2.5-1.5B-Instruct` (запечён)

Все образы сохраняются в `transfer/idp-images.tar`.

Запуск на Linux (или Windows через WSL2) с Windows-образами:
```bash
chmod +x scripts/import-images-linux.sh
./scripts/import-images-linux.sh win-test
```

Или напрямую через Compose:
```bash
docker compose -f infra/compose/local.yml --profile win-test up -d
```

В этом режиме модели уже внутри контейнеров, интернет не нужен, `/models` монтируется но не используется.

## Переключение между сценариями

- Linux production: `--profile linux`
- Windows E2E: `--profile win-test`

По умолчанию (без профиля) ничего не запускается, чтобы случайно не поднять старые сервисы.

## Как подать документы на обработку

Скопируйте PDF или DOCX в `data/input/` до запуска Compose. Worker автоматически обработает все файлы при старте.

```bash
cp ~/Downloads/document.pdf data/input/
docker compose -f infra/compose/local.yml --profile linux up -d
```

Или для Windows E2E:
```bash
cp ~/Downloads/document.pdf data/input/
docker compose -f infra/compose/local.yml --profile win-test up -d
```

## Мониторинг

Worker выводит прогресс-бар (`tqdm`) в консоль:
- общий прогресс по файлам
- этапы обработки каждого файла (рендеринг, VLM, LLM, сохранение)

Логи:
```bash
docker compose -f infra/compose/local.yml logs -f worker
```

## Остановка

```bash
docker compose -f infra/compose/local.yml down
```

Данные сохраняются в `data/output/`.
