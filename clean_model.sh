#!/usr/bin/env bash
set -euo pipefail

MODEL_DIR="transfer/models/new_vl"

if [ ! -d "$MODEL_DIR" ]; then
  echo "Модель не найдена: $MODEL_DIR"
  exit 0
fi

echo "Удаляю модель: $MODEL_DIR"

if rm -rf "$MODEL_DIR" 2>/dev/null; then
  echo "Удалено успешно"
else
  echo "Нет прав — пробую через sudo..."
  sudo rm -rf "$MODEL_DIR"
  echo "Удалено через sudo"
fi

echo "Готово. Папка очищена."
