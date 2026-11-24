#!/usr/bin/env bash
# Полный прогон переработки — одна команда, без присмотра.
#
#   ./scripts/full_reprocess.sh           # в tmux/screen или с nohup
#
# Порядок (как договорились — работаем от output, input открываем только
# для файлов с проблемами):
#   0. git pull + сборка образа воркера
#   1. verify: сверка results.jsonl с txts/ (output-only) — что без txt,
#      что в error; error проверяются в input: живые — на переработку,
#      мёртвые (0 байт/не PDF) — карантин, больше никого не блокируют
#   2. воркер перерабатывает найденное (ждём до 24 ч, с прогрессом)
#   3. merge: книги, расколотые эвристикой на главы, склеиваются обратно
#   4. reflow: 'лесенку' (строки, разорванные колонкой) склеивает VLM
#   5. корректировка ВСЕХ файлов в output (plain text) + схлопывание results.jsonl
#   6. сборка финального архива текстов
#   7. воркер снова в работе
#
# Весь вывод дублируется в logs/reprocess_<время>.log (подпапка репо,
# не корень).  REPROCESS_QUIET=1 — писать только в файл, в консоль тишина.
# Управление: scripts/reprocess_ctl.sh {start|stop|status|logs} — запускает
# этот скрипт в nohup, корректно гасит, показывает прогресс.
# При ошибке на любом шаге воркер всё равно поднимается обратно.
set -uo pipefail

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

TOTAL_STEPS=7
STEP_NO=0
step() {
    STEP_NO=$((STEP_NO + 1))
    echo
    # Полоска общего прогресса по шагам: [##>    ] 3/6 merge: ...
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

step "verify: сверка output (журнал vs txts), карантин мёртвых"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/verify_reprocess.py verify

step "worker перерабатывает найденное (до 24 ч)"
# --force-recreate ОБЯЗАТЕЛЕН: autoheal/руками воркер мог подняться ПОСЛЕ verify,
# но с журналом, прочитанным ДО его перезаписи — тогда up -d не пересоздаёт
# контейнер, воркер сидит с протухшим processed-сетом и ничего не жуёт.
$COMPOSE up -d --force-recreate worker
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/verify_reprocess.py wait

step "merge: склейка книг, разбитых на главы (сборники/газеты не трогаем)"
$COMPOSE stop worker || true
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py merge

step "reflow: 'лесенку' склеивает VLM (только разорванные колонкой файлы)"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/reflow_vlm.py

step "format: plain text для всех txts + схлопывание results.jsonl"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py format

step "worker снова в строю"
$COMPOSE up -d worker

echo
echo "=== $(date '+%F %T') ГОТОВО. Лог: $LOG ==="
trap - ERR
