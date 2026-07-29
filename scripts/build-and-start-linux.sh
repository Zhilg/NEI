#!/usr/bin/env bash
# Linux build and start script
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Parse arguments
OLD_MODE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --old)
      OLD_MODE=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI not found in PATH." >&2
  exit 1
fi

# Build or pull images depending on mode
if [[ "$OLD_MODE" -eq 1 ]]; then
  echo "[1/5] Old-driver mode: pulling vllm v0.5 GPU image and tagging local images"
  OLD_BASE_IMAGE="${IDP_OLD_VLLM_BASE:-vllm/vllm-openai:v0.5.0}"
  echo "  Pulling $OLD_BASE_IMAGE"
  docker pull "$OLD_BASE_IMAGE"
  docker tag "$OLD_BASE_IMAGE" local/vllm-vl:old
  docker tag "$OLD_BASE_IMAGE" local/vllm-llm:old

  echo "[2/5] Building worker image..."
  docker build -t local/idp-app:latest "$project_root"

  echo "[3/5] Ensuring small models are in place for old mode..."
  models_vl="$project_root/transfer/models/vl"
  models_llm="$project_root/transfer/models/llm"

  mkdir -p "$models_vl" "$models_llm"
else
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
fi

ensure_model() {
  local target_dir="$1"
  local repo="$2"
  local token="${HF_TOKEN:-}"

  if [[ -f "$target_dir/config.json" ]] || [[ -f "$target_dir/pytorch_model.bin" ]] || [[ -f "$target_dir/tokenizer.json" ]]; then
    echo "  Model already present in $target_dir"
    return 0
  fi

  echo "  Downloading $repo -> $target_dir using an isolated container (no host changes)"

  # Use an ephemeral Python container to download the model with huggingface-hub
  docker run --rm \
    -e REPO="$repo" \
    -e HF_TOKEN="$token" \
    -v "$target_dir":/target \
    python:3.12-slim \
    sh -lc 'pip install -q huggingface-hub && python - <<PY
import os
from huggingface_hub import snapshot_download
repo = os.environ["REPO"]
token = os.environ.get("HF_TOKEN") or None
snapshot_download(repo_id=repo, local_dir="/target", local_dir_use_symlinks=False, token=token)
PY'

  if [[ $? -ne 0 ]]; then
    echo "ERROR: Failed to download $repo" >&2
    exit 1
  fi
}

# Choose which models to fetch
if [[ "$OLD_MODE" -eq 1 ]]; then
  # Small models chosen by request to exercise vLLM in --old mode
  ensure_model "$models_vl" "HuggingFaceTB/SmolVLM-256M-Instruct"
  ensure_model "$models_llm" "HuggingFaceTB/SmolLM2-135M-Instruct"
else
  ensure_model "$models_vl" "Qwen/Qwen2.5-VL-32B-Instruct-AWQ"
  ensure_model "$models_llm" "Qwen/Qwen3-14B-AWQ"
fi

echo "[5/5] Starting stack..."
input_root="${IDP_INPUT_ROOT:-${project_root}/data/input}"
output_root="${IDP_OUTPUT_ROOT:-${project_root}/data/output}"
models_root="${IDP_MODELS_ROOT:-${project_root}/transfer/models}"

mkdir -p "$input_root" "$output_root" "$models_root"
# Ensure hf-cache folders exist and are writable for vllm services
mkdir -p "$models_vl/hf-cache" "$models_llm/hf-cache"
chmod 0777 "$models_vl/hf-cache" "$models_llm/hf-cache"

export IDP_SOURCE_ROOT="$project_root"
export IDP_INPUT_ROOT="$input_root"
export IDP_OUTPUT_ROOT="$output_root"
export IDP_MODELS_ROOT="$models_root"
export IDP_APP_IMAGE="local/idp-app:latest"

# If old mode is active, use the pulled/tagged old vllm images
if [[ "$OLD_MODE" -eq 1 ]]; then
  export IDP_VLLM_VL_IMAGE="local/vllm-vl:old"
  export IDP_VLLM_LLM_IMAGE="local/vllm-llm:old"
  # Default small models requested
  export IDP_VL_MODEL="${IDP_VL_MODEL:-HuggingFaceTB/SmolVLM-256M-Instruct}"
  export IDP_LLM_MODEL="${IDP_LLM_MODEL:-HuggingFaceTB/SmolLM2-135M-Instruct}"
else
  export IDP_VLLM_VL_IMAGE="local/vllm-vl:latest"
  export IDP_VLLM_LLM_IMAGE="local/vllm-llm:latest"
fi

cd "$project_root"
if [[ "$OLD_MODE" -eq 1 ]]; then
  echo "[*] Old-driver mode enabled: using compose override infra/compose/local-old.yml"
  docker compose -f infra/compose/local.yml -f infra/compose/local-old.yml up -d
else
  docker compose -f infra/compose/local.yml up -d
fi

echo ""
echo "=== Stack started ==="
echo "Input:  $input_root"
echo "Output: $output_root"
echo "Models: $models_root"
