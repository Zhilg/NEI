#!/usr/bin/env bash
# Полный прогон переработки — одна команда, без присмотра.
#
#   ./scripts/full_reprocess.sh           # в tmux/screen или с nohup
#
# Порядок (как договорились):
#   0. git pull + сборка образа воркера
#   1. PDF с текстовым слоем — записи/txt выкинуты на переработку
#   2. воркер перерабатывает их новым пайплайном (ждём до 24 ч, с прогрессом)
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

step() { echo; echo "=== $(date '+%F %T') $* ==="; }

step "0/7 git pull + сборка образа"
git pull
$COMPOSE build worker
$COMPOSE stop worker || true

step "1/7 pdf-requeue: текстовослойные pdf на переработку"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py pdf-requeue

step "2/7 worker перерабатывает pdf (до 24 ч)"
$COMPOSE up -d worker
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/wait_pdf_reprocess.py

step "3/7 merge: склейка книг, разбитых на главы (сборники/газеты не трогаем)"
$COMPOSE stop worker || true
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py merge

step "4/7 reflow: 'лесенку' склеивает VLM (только разорванные колонкой файлы)"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/reflow_vlm.py

step "5/7 format: plain text для всех txts + схлопывание results.jsonl"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py format

step "6/7 archive: финальная сборка results.zip"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py archive

step "7/7 worker снова в строю"
$COMPOSE up -d worker

echo
echo "=== $(date '+%F %T') ГОТОВО. Лог: $LOG ==="
trap - ERR
