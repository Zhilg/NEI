#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="infra/compose/local.yml"
ENV_FILE="${IDP_ENV_FILE:-.env}"

usage() {
  cat <<EOF
Использование: ./run <команда> [аргументы]

Команды:
  up                  Запустить стек (vLLM + worker + веб)
  down                Остановить и удалить контейнеры
  restart             Перезапустить стек
  logs                Логи воркера (follow)
  ps                  Статус контейнеров
  process [--force]   Обработать файлы из data/input
  web                 Веб-интерфейс на http://localhost:18067
  web-stub            Веб-интерфейс без GPU (заглушка VLM)
  model qwen|xiaomi   Переключить модель (сохраняет в .env)
  download [hf/repo]  Скачать модель в transfer/models/
  bench               Бенчмарк VLM
  test                Тесты без GPU
  shell               shell в контейнере воркера
  help                Эта справка

Примеры:
  ./run up                          # запуск на Qwen-3.8 (по умолчанию)
  ./run model xiaomi && ./run up    # переключиться на Xiaomi-OCR-0.8 и запустить
  ./run process --force             # переобработать все файлы
  ./run download xiaomi-research/Xiaomi-OCR-0 --model-dir xiaomi-ocr-0
EOF
  exit 1
}

cmd="${1:-}"
shift || true

case "$cmd" in
  up)
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build
    ;;
  down)
    docker compose -f "$COMPOSE_FILE" down
    ;;
  restart)
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build
    ;;
  logs)
    docker compose -f "$COMPOSE_FILE" logs -f worker
    ;;
  ps)
    docker compose -f "$COMPOSE_FILE" ps
    ;;
  process)
    docker compose -f "$COMPOSE_FILE" run --rm worker idp "$@"
    ;;
  web)
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d web
    echo "Веб-интерфейс: http://localhost:18067"
    ;;
  web-stub)
    docker compose -f "$COMPOSE_FILE" --profile stub up -d web-stub
    echo "Веб-интерфейс (stub): http://localhost:18067"
    ;;
  model)
    target="${1:-}"
    case "$target" in
      qwen)
        cp .env.qwen .env
        echo "Модель переключена: Qwen-3.8 (27B AWQ-INT4)"
        ;;
      xiaomi)
        cp .env.xiaomi .env
        echo "Модель переключена: Xiaomi-OCR-0.8"
        ;;
      *)
        echo "Доступные модели: qwen, xiaomi"
        exit 1
        ;;
    esac
    ;;
  download)
    python scripts/download_vl_model.py "$@"
    ;;
  bench)
    python scripts/bench_vlm.py "$@"
    ;;
  test)
    python3 -m pytest tests/ -q --timeout 150 --timeout-method=thread
    ;;
  shell)
    docker compose -f "$COMPOSE_FILE" run --rm --no-deps worker bash
    ;;
  help|--help|-h|"")
    usage
    ;;
  *)
    echo "Неизвестная команда: $cmd"
    usage
    ;;
esac
