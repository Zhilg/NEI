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

echo "[4/5] Checking models..."
models_vl="$project_root/transfer/models/vl"
models_llm="$project_root/transfer/models/llm"

if [[ ! -d "$models_vl" ]] || [[ ! -d "$models_llm" ]]; then
  echo "Models not found. Creating directories and downloading..."
  mkdir -p "$models_vl" "$models_llm"
  pip install huggingface-hub -q
  python3 -c "
from huggingface_hub import snapshot_download
import sys
try:
    snapshot_download('Qwen/Qwen2.5-VL-32B-Instruct', local_dir='$models_vl', local_dir_use_symlinks=False)
except Exception as e:
    print(f'Failed to download VL model: {e}', file=sys.stderr)
    sys.exit(1)
try:
    snapshot_download('Qwen/Qwen3-14B-Instruct', local_dir='$models_llm', local_dir_use_symlinks=False)
except Exception as e:
    print(f'Failed to download LLM model: {e}', file=sys.stderr)
    sys.exit(1)
"
fi

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
