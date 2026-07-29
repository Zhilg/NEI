#!/usr/bin/env bash
# Simplified import: loads images and starts the Compose stack.
set -euo pipefail

<<<<<<< HEAD
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
archive_path="${project_root}/transfer/idp-images.tar"
=======
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
archive_path="${project_root}/idp-images.tar"
checksum_path="${archive_path}.sha256"
>>>>>>> main
metadata_path="${archive_path}.json"
completion_path="${project_root}/EXPORT-COMPLETE.txt"
bundle_checksum_path="${project_root}/SHA256SUMS"

if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Docker CLI was not found in PATH.' >&2
  exit 1
fi

if [[ ! -f "$archive_path" ]]; then
  printf 'Image archive does not exist: %s\n' "$archive_path" >&2
  exit 1
fi

if [[ ! -f "${project_root}/infra/compose/local.yml" ]]; then
  printf 'Compose file does not exist under project root: %s\n' "$project_root" >&2
  exit 1
fi

input_root="${IDP_INPUT_ROOT:-${project_root}/data/input}"
<<<<<<< HEAD
output_root="${IDP_OUTPUT_ROOT:-${project_root}/data/output}"
models_root="${IDP_MODELS_ROOT:-${project_root}/transfer/models}"
=======
models_root="${IDP_MODELS_ROOT:-${project_root}/models}"
tools_root="${IDP_TOOLS_ROOT:-${project_root}/data/tools}"

existing_env_value() {
  local key="$1"
  local line=""
  if [[ -f "${project_root}/.env" ]]; then
    line="$(grep -E "^${key}=" "${project_root}/.env" | tail -n 1 || true)"
  fi
  printf '%s' "${line#*=}"
}

if [[ ! -f "$checksum_path" ]]; then
  printf 'Checksum file does not exist: %s\n' "$checksum_path" >&2
  exit 1
fi
if [[ ! -f "$metadata_path" ]]; then
  printf 'Metadata file does not exist: %s\n' "$metadata_path" >&2
  exit 1
fi
if [[ ! -f "$completion_path" ]]; then
  printf 'Windows export completion marker does not exist: %s\n' "$completion_path" >&2
  exit 1
fi
if [[ ! -f "$bundle_checksum_path" ]]; then
  printf 'Bundle checksum manifest does not exist: %s\n' "$bundle_checksum_path" >&2
  exit 1
fi
printf '%s\n' 'Verifying portable bundle files...'
(cd "$project_root" && sha256sum --check "$(basename "$bundle_checksum_path")")
(cd "$(dirname "$archive_path")" && sha256sum --check "$(basename "$checksum_path")")
>>>>>>> main

docker load --input "$archive_path"

<<<<<<< HEAD
mkdir -p "$input_root" "$output_root" "$models_root"
=======
mkdir -p "$data_root" "$input_root" "$tools_root/mineru"

find "$tools_root/mineru" -type f -name '*.py' -exec chmod +x {} + 2>/dev/null || true
find "$tools_root/mineru" -maxdepth 1 -type f -exec chmod +x {} + 2>/dev/null || true

mineru_checkpoint="$tools_root/mineru/models/Layout/YOLO/doclayout_yolo_docstructbench_imgsz1280_2501.pt"
mineru_config="$tools_root/mineru/magic-pdf.json"
mineru_runner="$tools_root/mineru/run"
for required_file in "$mineru_checkpoint" "$mineru_config" "$mineru_runner"; do
  if [[ ! -s "$required_file" ]]; then
    printf 'Required offline MinerU asset is missing or empty: %s\n' "$required_file" >&2
    exit 1
  fi
done
docker run --rm --network none \
  --env HF_HUB_OFFLINE=1 \
  --env TRANSFORMERS_OFFLINE=1 \
  --env HF_DATASETS_OFFLINE=1 \
  --env MODELSCOPE_OFFLINE=1 \
  --volume "$tools_root/mineru:/tools/mineru:ro" \
  --entrypoint python local/idp-app:latest \
  -c "from pathlib import Path; import magic_pdf; checkpoint=Path('/tools/mineru/models/Layout/YOLO/doclayout_yolo_docstructbench_imgsz1280_2501.pt'); assert checkpoint.is_file() and checkpoint.stat().st_size > 0"

for model in qwen-vl qwen3; do
  if [[ ! -f "$models_root/$model/config.json" ]] \
    || [[ -z "$(find "$models_root/$model" -type f -name '*.safetensors' -print -quit)" ]]; then
    printf 'Mounted model is missing or incomplete: %s\n' "$models_root/$model" >&2
    exit 1
  fi
done
if [[ ! -f "$models_root/SHA256SUMS" ]]; then
  printf 'Model checksum manifest is missing: %s\n' "$models_root/SHA256SUMS" >&2
  exit 1
fi
printf '%s\n' 'Verifying mounted model files...'
(cd "$models_root" && sha256sum --check SHA256SUMS)
>>>>>>> main

export IDP_SOURCE_ROOT="$project_root"
export IDP_INPUT_ROOT="$input_root"
export IDP_OUTPUT_ROOT="$output_root"
export IDP_MODELS_ROOT="$models_root"
export IDP_APP_IMAGE="local/idp-app:latest"
<<<<<<< HEAD
export IDP_VLLM_VL_IMAGE="local/vllm-vl:latest"
export IDP_VLLM_LLM_IMAGE="local/vllm-llm:latest"
=======
export IDP_POSTGRES_IMAGE="postgres:16.9-alpine"
export IDP_MINIO_IMAGE="minio/minio:RELEASE.2025-04-22T22-12-26Z"
export IDP_MINIO_MC_IMAGE="minio/mc:RELEASE.2025-05-21T01-59-54Z"
export IDP_QWEN_VL_IMAGE="local/qwen-vl:latest"
export IDP_QWEN3_IMAGE="local/qwen3:latest"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1
export MODELSCOPE_OFFLINE=1
export MINERU_TOOLS_CONFIG_JSON="$mineru_config"
archive_hash="$(cut -d ' ' -f 1 "$checksum_path")"
models_hash="$(sha256sum "$models_root/SHA256SUMS" | cut -d ' ' -f 1)"
export IDP_PIPELINE_PROFILE_VERSION="$(printf '%s\n%s\n' "$archive_hash" "$models_hash" | sha256sum | cut -c 1-16)"
export IDP_POSTGRES_PASSWORD="${IDP_POSTGRES_PASSWORD:-$(existing_env_value IDP_POSTGRES_PASSWORD)}"
export IDP_MINIO_ACCESS_KEY="${IDP_MINIO_ACCESS_KEY:-$(existing_env_value IDP_MINIO_ACCESS_KEY)}"
export IDP_MINIO_SECRET_KEY="${IDP_MINIO_SECRET_KEY:-$(existing_env_value IDP_MINIO_SECRET_KEY)}"
export IDP_POSTGRES_PASSWORD="${IDP_POSTGRES_PASSWORD:-idp}"
export IDP_MINIO_ACCESS_KEY="${IDP_MINIO_ACCESS_KEY:-minioadmin}"
export IDP_MINIO_SECRET_KEY="${IDP_MINIO_SECRET_KEY:-minioadmin}"

if [[ -f "${project_root}/.env" ]]; then
  cp "${project_root}/.env" "${project_root}/.env.before-idp-import"
fi
cat > "${project_root}/.env" <<EOF
IDP_SOURCE_ROOT=${IDP_SOURCE_ROOT}
IDP_INPUT_ROOT=${IDP_INPUT_ROOT}
IDP_DATA_ROOT=${IDP_DATA_ROOT}
IDP_MODELS_ROOT=${IDP_MODELS_ROOT}
IDP_TOOLS_ROOT=${IDP_TOOLS_ROOT}
IDP_APP_IMAGE=${IDP_APP_IMAGE}
IDP_POSTGRES_IMAGE=${IDP_POSTGRES_IMAGE}
IDP_MINIO_IMAGE=${IDP_MINIO_IMAGE}
IDP_MINIO_MC_IMAGE=${IDP_MINIO_MC_IMAGE}
IDP_QWEN_VL_IMAGE=${IDP_QWEN_VL_IMAGE}
IDP_QWEN3_IMAGE=${IDP_QWEN3_IMAGE}
HF_HUB_OFFLINE=${HF_HUB_OFFLINE}
TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE}
HF_DATASETS_OFFLINE=${HF_DATASETS_OFFLINE}
MODELSCOPE_OFFLINE=${MODELSCOPE_OFFLINE}
MINERU_TOOLS_CONFIG_JSON=${MINERU_TOOLS_CONFIG_JSON}
IDP_PIPELINE_PROFILE_VERSION=${IDP_PIPELINE_PROFILE_VERSION}
IDP_POSTGRES_PASSWORD=${IDP_POSTGRES_PASSWORD}
IDP_MINIO_ACCESS_KEY=${IDP_MINIO_ACCESS_KEY}
IDP_MINIO_SECRET_KEY=${IDP_MINIO_SECRET_KEY}
EOF
>>>>>>> main

cd "$project_root"
docker compose -f infra/compose/local.yml up -d

printf '%s\n' 'Images imported and stack started.'
printf 'Input: %s\n' "$input_root"
printf 'Output: %s\n' "$output_root"
printf 'Models: %s\n' "$models_root"
