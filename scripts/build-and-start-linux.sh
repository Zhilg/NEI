#!/usr/bin/env bash
# Linux build and start script
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI not found in PATH." >&2
  exit 1
fi

echo "[1/5] Building worker image..."
docker build -t local/idp-app:latest "$project_root"

echo "[2/5] Building vLLM VL image..."
docker build -t local/vllm-vl:latest "$project_root/infra/dockerfiles/vllm-vl"

echo "[3/5] Building vLLM LLM image..."
docker build -t local/vllm-llm:latest "$project_root/infra/dockerfiles/vllm-llm"

echo "[4/5] Ensuring models are in place..."
models_vl="$project_root/transfer/models/vl"
models_llm="$project_root/transfer/models/llm"

mkdir -p "$models_vl" "$models_llm"

ensure_model() {
  local target_dir="$1"
  local repo="$2"
  local token="${HF_TOKEN:-}"

  if [[ -f "$target_dir/config.json" ]]; then
    echo "  Model already present in $target_dir"
    return 0
  fi

  echo "  Downloading $repo -> $target_dir"
  if ! command -v huggingface-cli >/dev/null 2>&1; then
    pip install -q huggingface-hub
  fi

  local args=("download" "$repo" "--local-dir" "$target_dir" "--local-dir-use-symlinks" "False")
  if [[ -n "$token" ]]; then
    args+=("--token" "$token")
  fi

  if ! huggingface-cli "${args[@]}"; then
    echo "ERROR: Failed to download $repo" >&2
    exit 1
  fi
}

ensure_model "$models_vl" "Qwen/Qwen2.5-VL-32B-Instruct-AWQ"
ensure_model "$models_llm" "Qwen/Qwen3-14B-AWQ"

echo "[5/5] Starting stack..."
input_root="${IDP_INPUT_ROOT:-${project_root}/data/input}"
output_root="${IDP_OUTPUT_ROOT:-${project_root}/data/output}"
models_root="${IDP_MODELS_ROOT:-${project_root}/transfer/models}"

mkdir -p "$input_root" "$output_root" "$models_root"

export IDP_SOURCE_ROOT="$project_root"
export IDP_INPUT_ROOT="$input_root"
export IDP_OUTPUT_ROOT="$output_root"
export IDP_MODELS_ROOT="$models_root"
export IDP_APP_IMAGE="local/idp-app:latest"
export IDP_VLLM_VL_IMAGE="local/vllm-vl:latest"
export IDP_VLLM_LLM_IMAGE="local/vllm-llm:latest"

cd "$project_root"
docker compose -f infra/compose/local.yml up -d

echo ""
echo "=== Stack started ==="
echo "Input:  $input_root"
echo "Output: $output_root"
echo "Models: $models_root"
