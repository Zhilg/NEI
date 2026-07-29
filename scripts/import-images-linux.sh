#!/usr/bin/env bash
# Simplified import: loads images and starts the Compose stack.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
archive_path="${project_root}/transfer/idp-images.tar"
metadata_path="${archive_path}.json"
completion_path="${project_root}/transfer/EXPORT-COMPLETE.txt"

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
output_root="${IDP_OUTPUT_ROOT:-${project_root}/data/output}"
models_root="${IDP_MODELS_ROOT:-${project_root}/transfer/models}"

docker load --input "$archive_path"

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

printf '%s\n' 'Images imported and stack started.'
printf 'Input: %s\n' "$input_root"
printf 'Output: %s\n' "$output_root"
printf 'Models: %s\n' "$models_root"
