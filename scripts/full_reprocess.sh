#!/usr/bin/env bash
# Финальная постобработка output — одна команда, без присмотра.
#
#   ./scripts/full_reprocess.sh           # в tmux/screen или с nohup
#
# Только оставшиеся операции:
#   1. git pull + сборка образа воркера
#   2. удаление неосновного текста из txts + названия файлов по теме
#   3. worker снова в работе
#
# Переработка input, merge, reflow и старый format здесь больше не запускаются.
# Весь вывод дублируется в logs/reprocess_<время>.log.
# REPROCESS_QUIET=1 — писать только в файл, в консоль тишина.
# Управление: scripts/reprocess_ctl.sh {start|stop|status|logs}.
# При ошибке worker всё равно поднимается обратно.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f infra/compose/local.yml"
mkdir -p logs
PID_FILE="logs/reprocess.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "прогон уже идёт (pid $(cat "$PID_FILE")). Сначала: scripts/reprocess_ctl.sh stop" >&2
    exit 1
fi
echo $$ > "$PID_FILE"
LOG="logs/reprocess_$(date +%Y%m%d_%H%M%S).log"
if [ "${REPROCESS_QUIET:-0}" = "1" ]; then
    exec >> "$LOG" 2>&1
else
    exec > >(tee -a "$LOG") 2>&1
fi

finish() {
    echo "=== $(date '+%F %T') аварийное восстановление: поднимаю worker ==="
    $COMPOSE up -d worker
}
trap finish ERR
trap 'rm -f "$PID_FILE"' EXIT

TOTAL_STEPS=3
STEP_NO=0
STEP_STARTED=$SECONDS
finish_step() {
    if [ "$STEP_NO" -gt 0 ]; then
        echo "=== $(date '+%F %T') шаг $STEP_NO/$TOTAL_STEPS завершён за $((SECONDS - STEP_STARTED)) с ==="
    fi
}
step() {
    finish_step
    STEP_NO=$((STEP_NO + 1))
    STEP_STARTED=$SECONDS
    echo
    local filled=$((STEP_NO > TOTAL_STEPS ? TOTAL_STEPS : STEP_NO))
    local bar=""
    for ((i = 1; i <= TOTAL_STEPS; i++)); do
        if [ "$i" -lt "$filled" ]; then bar="${bar}#";
        elif [ "$i" -eq "$filled" ]; then bar="${bar}>";
        else bar="${bar}."; fi
    done
    echo "=== $(date '+%F %T') [$bar] $STEP_NO/$TOTAL_STEPS $* ==="
}

step "git pull + сборка образа"
git pull
$COMPOSE build worker
$COMPOSE stop worker || true

step "чистка неосновного текста + названия по теме"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py finalize

step "worker снова в строю"
$COMPOSE up -d worker
finish_step

echo
echo "=== $(date '+%F %T') ГОТОВО. Лог: $LOG ==="
trap - ERR
