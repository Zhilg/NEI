# IDP — перенос на сервер без интернета

## Что в архиве
- `src/idp/` — весь Python-код конвейера
- `infra/compose/local.yml` — Docker Compose (только vLLM, без SGLang)
- `infra/dockerfiles/vllm-vl/Dockerfile` — образ vLLM VL
- `scripts/download_vl_model.py` — скачка модели
- `up`, `down`, `go`, `web`, `dl`, `logs`, `ps`, `status` — короткие команды запуска
- `.env.qwen`, `.env.xiaomi` — конфиги под каждую модель
- `tests/vlm_stub.py` — стаб для тестов без GPU

## Что нужно на целевом сервере
1. Docker + NVIDIA Container Toolkit (для GPU)
2. Папка `data/input/` с файлами для обработки
3. Модель в `transfer/models/new_vl/` (скачайте один раз на машине с интернетом через `./dl qwen`)

## Порядок действий
```bash
# На машине с интернетом:
./pack_offline.sh        # создаёт idp_offline_YYYYMMDD_HHMMSS.tar.gz

# Перенести архив на сервер (флеш-накопитель, scp, и т.д.)
scp idp_offline_*.tar.gz user@server:~/

# На сервере:
tar xzf idp_offline_*.tar.gz
cd <распакованная папка>

# Если модель не вошла в архив (крупная), скачать отдельно и положить в transfer/models/new_vl/
./dl qwen

# Поднять стек
./up qwen

# Проверить
./status

# Обработать файлы
./go --force

# Веб-интерфейс
./web qwen
```

## Команды
| Команда | Что делает |
|---------|------------|
| `./up qwen` | Переключиться на Qwen-3.8 и поднять стек |
| `./up xiaomi` | Переключиться на Xiaomi-OCR-0.8 и поднять стек |
| `./down` | Остановить все контейнеры |
| `./go [--force]` | Обработать файлы из `data/input/` |
| `./web [qwen\|xiaomi]` | Веб-интерфейс на `http://localhost:18067` |
| `./dl qwen` | Скачать модель (нужен интернет) |
| `./status` | Проверить, что vLLM и модель работают |
| `./logs` | Логи воркера |
| `./ps` | Статус контейнеров |

## Требования к железу
- Qwen-3.8 27B: ~24 GB VRAM (GPU'architecture Ampere+, SM 8.0+)
- Xiaomi-OCR-0.8: ~4-6 GB VRAM
