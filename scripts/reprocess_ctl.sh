#!/usr/bin/env bash
# Управление полным прогоном переработки — одна команда вместо танцев с PID.
#
#   scripts/reprocess_ctl.sh start    # запустить прогон в фоне (nohup, тихо)
#   scripts/reprocess_ctl.sh stop     # остановить прогон и все контейнеры
#   scripts/reprocess_ctl.sh status   # что сейчас происходит + последние строки лога
#   scripts/reprocess_ctl.sh logs     # следить за логом (tail -f, Ctrl+C чтобы выйти)
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
            echo "--- $latest:"
            tail -5 "$latest"
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
    *)
        echo "использование: $0 {start|stop|status|logs}" >&2
        exit 1
        ;;
esac
