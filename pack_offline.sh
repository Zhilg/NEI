#!/usr/bin/env bash
set -euo pipefail

# pack_offline.sh — готовит архив для переноса на сервер без интернета
# Собирает: код, конфиги, скрипты запуска, докерфайлы, модель Qwen-3.8

OUT="idp_offline_$(date +%Y%m%d_%H%M%S).tar.gz"
PACK_DIR="/tmp/kilo/idp_pack"
rm -rf "$PACK_DIR"
mkdir -p "$PACK_DIR"

echo "==> Копируем исходный код и конфиги..."
cp -a /workspace/app/README.md "$PACK_DIR/"
cp -a /workspace/app/pyproject.toml "$PACK_DIR/"
cp -a /workspace/app/src "$PACK_DIR/"
cp -a /workspace/app/scripts "$PACK_DIR/"
cp -a /workspace/app/infra "$PACK_DIR/"
cp -a /workspace/app/docs "$PACK_DIR/"
cp -a /workspace/app/tests "$PACK_DIR/"
cp -a /workspace/app/.env.example "$PACK_DIR/"
cp -a /workspace/app/.env.qwen "$PACK_DIR/"
cp -a /workspace/app/.env.xiaomi "$PACK_DIR/"

echo "==> Копируем скрипты запуска..."
for f in up down go web dl logs ps status run.sh; do
  cp -a "/workspace/app/$f" "$PACK_DIR/"
done

echo "==> Скачиваем модель Qwen-3.8-AWQ (если ещё нет)..."
MODEL_DIR="transfer/models/new_vl"
mkdir -p "$MODEL_DIR"
if [ -z "$(ls -A "$MODEL_DIR" 2>/dev/null)" ]; then
  python3 /workspace/app/scripts/download_vl_model.py \
    --repo mattbucci/Qwen3.8-27B-AWQ \
    --model-dir new_vl \
    || echo "ВНИМАНИЕ: не удалось скачать модель — качайте вручную на сервере с интернетом и положите в transfer/models/new_vl/"
fi
# Копируем скачанную модель в паковку
cp -a /workspace/app/transfer "$PACK_DIR/" 2>/dev/null || true

echo "==> Создаём архив..."
tar czf "/workspace/app/$OUT" -C "$PACK_DIR" .
echo "Готово: /workspace/app/$OUT"
echo ""
echo "На сервере без интернета:"
echo "  tar xzf $OUT"
echo "  ./dl qwen          # только если модель не скачалась (нужен интернет один раз)"
echo "  ./up qwen          # поднимает стек (нужен Docker + GPU)"
echo "  ./go --force       # обработать файлы"
