#!/usr/bin/env bash
# Полный прогон переработки — одна команда, без присмотра.
#
#   ./scripts/full_reprocess.sh           # в tmux/screen или с nohup
#
# Порядок (как договорились):
#   0. git pull + сборка образа воркера
#   1. PDF с текстовым слоем — записи/txt выкинуты на переработку
#   2. воркер перерабатывает их новым пайплайном (ждём до 24 ч, с прогрессом)
#   3. корректировка ВСЕХ файлов в output (plain text) + схлопывание results.jsonl
#   4. сборка финального архива текстов
#   5. воркер снова в работе
#
# Весь вывод дублируется в reprocess_<время>.log в корне репозитория.
# При ошибке на любом шаге воркер всё равно поднимается обратно.
set -uo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f infra/compose/local.yml"
LOG="reprocess_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

finish() {
    echo "=== $(date '+%F %T') аварийное восстановление: поднимаю worker ==="
    $COMPOSE up -d worker
}
trap finish ERR

step() { echo; echo "=== $(date '+%F %T') $* ==="; }

step "0/5 git pull + сборка образа"
git pull
$COMPOSE build worker
$COMPOSE stop worker || true

step "1/5 pdf-requeue: текстовослойные pdf на переработку"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py pdf-requeue

step "2/5 worker перерабатывает pdf (до 24 ч)"
$COMPOSE up -d worker
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/wait_pdf_reprocess.py

step "3/5 format: plain text для всех txts + схлопывание results.jsonl"
$COMPOSE stop worker
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py format

step "4/5 archive: финальная сборка results.zip"
$COMPOSE run --rm --no-deps worker \
    python /workspace/scripts/fix_results.py archive

step "5/5 worker снова в строю"
$COMPOSE up -d worker

echo
echo "=== $(date '+%F %T') ГОТОВО. Лог: $LOG ==="
trap - ERR
