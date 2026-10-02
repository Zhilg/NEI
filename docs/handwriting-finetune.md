# Рукопись: операторский цикл и fine-tuning

Документ описывает рабочий цикл для рукописных страниц: VLM извлекает Markdown и сущности,
оператор проверяет и исправляет результат, исправления используются как feedback для следующих
запросов и как датасет для дообучения модели.

## Проблема: confidence всегда 1.0

VLM (`Qwen3.8-27B`) возвращал `confidence: 1.0` для любой сущности, потому что:

1. промпт не требовал оценивать визуальное качество рукописного текста;
2. у LLM нет калиброванной шкалы уверенности — без явной инструкции выводится 1.0;
3. порог `min_entity_confidence` отбрасывал/оставлял сущности одинаково, а комментарии
   оператора появлялись только при `confidence < 0.5`, то есть никогда.

## Что изменено

| Изменение | Файл |
|---|---|
| Инструкция по оценке confidence (не дефолтить 1.0) в test-mode промптах | `src/idp/prompts.py` |
| Эвристическая confidence для рукописных сущностей + подробные комментарии | `src/idp/vlm_client.py` |
| Настройки оператора и fine-tuning | `src/idp/config.py` |
| Терминальный инструмент оператора | `src/idp/operator_client.py` |
| Хранилище исправлений (JSONL) | `src/idp/feedback_store.py` |
| Генератор датасета для LoRA | `src/idp/finetune_dataset.py` |

### Эвристика confidence

Если сущность помечена как рукописная и VLM вернул `confidence == 1.0` (или не вернул поле),
confidence вычисляется эвристикой и попадает в диапазон `0.35–0.70`, после чего применяется общий
штраф `0.85x`. На оценку влияют:

- длина evidence (короткие фрагменты неувереннее);
- неоднозначные символы (цифра/буква, кириллица/латиница) в evidence;
- количество маркеров `[HANDWRITTEN: ...]` на странице;
- несовпадение evidence с исходным текстом (дополнительный `0.7x`).

Явно оценённое VLM значение меньше 1.0 уважается, но всё равно получает штраф `0.85x`.

Комментарии оператора в test-mode (`confidence < 0.5`):

| Confidence | Комментарий |
|---|---|
| `< 0.3` | Рукопись плохо читается, требуется проверка оператором |
| `0.3–0.5` | Рукопись частично нечёткая, рекомендуется проверить |
| `0.5–0.7` | Рукопись допускает неточности, рекомендуется уточнить |

Порог `min_entity_confidence` в test-mode по-прежнему не применяется: в выдачу попадают все
сущности, чтобы оператор увидел всё.

## Поток данных

```
PDF → рендер PNG → VLM (Markdown + сущности + confidence + комментарии)
                          ↓
                   оператор (idp-operator)
                          ↓
        operator_corrections.jsonl (исправленный результат)
                          ↓
              feedback_store.py — последние N исправлений
                          ↓
        следующий запрос VLM получает их как few-shot примеры
                          ↓
         finetune_dataset.py → finetune_data.jsonl (для LoRA)
```

## Настройки

Переменные окружения (префикс `IDP_`):

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `IDP_FINETUNE_FEEDBACK_ENABLED` | `false` | Включает подстановку исправлений в промпты VLM |
| `IDP_FINETUNE_FEEDBACK_DIR` | `/output/finetune` | Каталог артефактов обучения |
| `IDP_FINETUNE_MAX_FEEDBACK_EXAMPLES` | `10` | Сколько последних исправлений попадает в промпт |
| `IDP_OPERATOR_CORRECTIONS_PATH` | `data/output/operator_corrections.jsonl` | Файл исправлений |
| `IDP_FINETUNE_DATASET_PATH` | `data/output/finetune_data.jsonl` | Файл датасета |

## Работа оператора

```bash
idp-operator --input data/input/test_handwrite.pdf
```

Флаги:

| Флаг | Назначение |
|---|---|
| `--input` | PDF, изображение или каталог страниц |
| `--document-type` | Подсказка типа документа для промпта (`other`, `directive`, `ttkh`, …) |
| `--limit N` | Ограничить количество страниц |
| `--drafts-dir` | Каталог редактируемых draft-файлов |
| `--open-image` | Открывать страницу в системном просмотрщике |
| `--production-prompts` | Использовать production-промпты вместо test-mode |

Для каждой страницы оператор получает в терминале: путь к изображению, реконструированный
Markdown и список сущностей с confidence, evidence и комментарием. Маркеры в выводе:
`!! < 0.3`, `! < 0.5`, `~ < 0.7`.

Дальше доступны действия:

- `a` — принять результат VLM как есть (исправление сохраняется с тем же содержимым);
- `e` — редактировать draft JSON (`markdown`, `operator_corrected_entities`) в своём редакторе;
- `r` — перечитать draft после правки;
- `s` — пропустить страницу (исправление не сохраняется);
- `q` — выйти.

Формат `operator_corrections.jsonl` (одна запись на страницу):

```json
{"page_image": "page_00001.png", "markdown": "...", "vlm_entities": [], "operator_corrected_entities": [], "timestamp": 1750000000.0}
```

`vlm_entities` хранит исходный ответ модели, `operator_corrected_entities` — итог после правки.
Оба поля нужны для обучения (до/после), поэтому запись сохраняется даже при полном принятии.

## Feedback в промптах

При `IDP_FINETUNE_FEEDBACK_ENABLED=true` последние N исправлений добавляются в system prompt
основного пайплайна (`idp`) и инструмента оператора:

```
Recent corrections from operator (VLM was wrong, operator corrected it — follow the operator's values, they are authoritative):
<EXAMPLE 1>
VLM output entities:
- amount: 8OO (confidence: 0.45)
Operator correction:
- amount: 800 (confidence: 1.00)
</EXAMPLE 1>
```

Блок кэшируется по `mtime`/`size` файла исправлений, поэтому повторные страницы в одном запуске
не перечитывают файл.

## Генерация датасета

```bash
idp-finetune-dataset --input data/output/operator_corrections.jsonl --output data/output/finetune_data.jsonl
```

Каждая строка — один обучающий пример:

```json
{"images": ["page_00001.png"], "text": "<system prompt>", "target": "{\"markdown\": \"...\", \"entities\": [...]}"}
```

`text` собирается из тех же test-mode промптов, что использует операторский цикл, чтобы обучение
совпадало с инференсом. Записи без сущностей в `operator_corrected_entities` пропускаются.

## Дообучение (документация, не реализовано)

Рекомендуемый путь — QLoRA поверх текущих весов Qwen:

1. взять `finetune_data.jsonl`, привести изображения к единому разрешению (те же `render_dpi`,
   `max_image_dimension`, что и в рендерере), положить рядом с датасетом;
2. обучать LoRA-адаптер через `transformers` + `peft` + `bitsandbytes` (4-bit), целевая
   конфигурация рассчитана на 12 GB VRAM: `r=16`, `alpha=32`, `dropout=0.05`, `lr=1e-4`,
   batch size 1 с gradient accumulation;
3. маскировать loss на prompt-токенах, обучать только на `target`;
4. после обучения переиграть операторскую выборку и сравнить долю исправлений до/после — это и
   есть метрика качества цикла.

Обучение запускается отдельно от пайплайна и не входит в `idp`.