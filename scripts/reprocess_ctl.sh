#!/usr/bin/env bash
# Управление полным прогоном переработки — одна команда вместо танцев с PID.
#
#   scripts/reprocess_ctl.sh start    # запустить прогон в фоне (nohup, тихо)
#   scripts/reprocess_ctl.sh stop     # остановить прогон и все контейнеры
#   scripts/reprocess_ctl.sh status   # что сейчас происходит + последние строки лога
#   scripts/reprocess_ctl.sh logs     # следить за логом (tail -f, Ctrl+C чтобы выйти)
#   scripts/reprocess_ctl.sh reset          # стоп + удалить журналы (txts сохраняются)
#   scripts/reprocess_ctl.sh reset --all    # и txts, и архив — прогон совсем с нуля
#
# Можно рвать SSH-соединение сразу после start — прогон доживёт сам.
set -uo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f infra/compose/local.yml"
PID_FILE="logs/reprocess.pid"

pid() { [ -f "$PID_FILE" ] && cat "$PID_FILE" 2>/dev/null || true; }
running() { local p; p=$(pid); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

case "${1:-}" in
    start)
        if running; then
            echo "прогон уже идёт (pid $(pid))" >&2
            exit 1
        fi
        mkdir -p logs
        REPROCESS_QUIET=1 nohup bash scripts/full_reprocess.sh >/dev/null 2>&1 &
        echo "прогон запущен (pid $!). Лог: logs/reprocess_*.log"
        echo "смотреть: scripts/reprocess_ctl.sh logs | status; стоп: stop"
        ;;
    stop)
        p=$(pid)
        if running; then
            kill "$p" && echo "прогон остановлен (pid $p)"
        else
            echo "прогон не работует"
        fi
        $COMPOSE stop 2>/dev/null || true
        echo "контейнеры остановлены"
        ;;
    status)
        if running; then
            echo "прогон работает (pid $(pid))"
        else
            echo "прогон не работает"
        fi
        $COMPOSE ps 2>/dev/null || true
        latest=$(ls -t logs/reprocess_*.log 2>/dev/null | head -1 || true)
        if [ -n "$latest" ]; then
            echo "--- $latest (последние 30 строк):"
            tail -30 "$latest"
            echo "--- смотреть непрерывно: scripts/reprocess_ctl.sh logs"
        fi
        ;;
    logs)
        latest=$(ls -t logs/reprocess_*.log 2>/dev/null | head -1 || true)
        if [ -n "$latest" ]; then
            tail -f "$latest"
        else
            echo "логов ещё нет — прогон не запускался" >&2
            exit 1
        fi
        ;;
    reset)
        # Полный сброс результатов: стоп + удаление журналов и служебных файлов.
        # txts НЕ трогаем — старый корпус в data/output/txts может не иметь
        # источников в input; стартовая нормализация сама почистит/переименует.
        # Полный wipe включая txt: reset --all (источники только с флешки!).
        p=$(pid)
        if running; then
            kill "$p" && echo "прогон остановлен (pid $p)"
        fi
        $COMPOSE stop 2>/dev/null || true
        rm -f data/output/results.jsonl data/output/results_readable.json \
              data/output/stats.jsonl data/output/verify_state.json \
              data/output/quarantine.txt data/output/txts/_names_map.json
        echo "журналы удалены"
        if [ "${2:-}" = "--all" ]; then
            rm -rf data/output/txts
            rm -f data/output/results.zip
            echo "txts и архив удалены"
        else
            echo "txts сохранены ($(ls data/output/txts/*.txt 2>/dev/null | wc -l) файлов)"
        fi
        echo "теперь: git pull && $0 start"
        ;;
    *)
        echo "использование: $0 {start|stop|status|logs|reset [--all]}" >&2
        exit 1
        ;;
esac
