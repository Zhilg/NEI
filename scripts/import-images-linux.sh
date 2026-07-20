#!/usr/bin/env bash
# Imports the Windows archive, prepares mounted paths, and starts the full Compose stack.
set -euo pipefail

archive_path="${1:-./idp-images.tar}"
project_root="${2:-$(pwd)}"
checksum_path="${archive_path}.sha256"
metadata_path="${archive_path}.json"

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

project_root="$(cd "$project_root" && pwd)"
data_root="${IDP_DATA_ROOT:-${project_root}/data/runtime}"
input_root="${IDP_INPUT_ROOT:-${project_root}/data/input}"
models_root="${IDP_MODELS_ROOT:-${project_root}/data/models}"
tools_root="${IDP_TOOLS_ROOT:-${project_root}/data/tools}"

if [[ ! -f "$checksum_path" ]]; then
  printf 'Checksum file does not exist: %s\n' "$checksum_path" >&2
  exit 1
fi
if [[ ! -f "$metadata_path" ]]; then
  printf 'Metadata file does not exist: %s\n' "$metadata_path" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  printf '%s\n' 'jq is required to read image metadata.' >&2
  exit 1
fi
(cd "$(dirname "$archive_path")" && sha256sum --check "$(basename "$checksum_path")")

docker load --input "$archive_path"

mkdir -p "$data_root" "$input_root" "$models_root/qwen-vl" "$models_root/qwen3" "$tools_root/mineru" "$tools_root/ocr"

if [[ ! -x "$tools_root/mineru/run" ]]; then
  printf 'Missing executable MinerU wrapper: %s\n' "$tools_root/mineru/run" >&2
  exit 1
fi

for tool in detect route recognize-east-slavic recognize-cyrillic recognize-latin-cjk; do
  if [[ ! -x "$tools_root/ocr/$tool" ]]; then
    printf 'Missing executable OCR wrapper: %s\n' "$tools_root/ocr/$tool" >&2
    exit 1
  fi
done

if [[ -z "$(find "$models_root/qwen-vl" -mindepth 1 -print -quit)" ]]; then
  printf 'Qwen-VL model directory is empty: %s\n' "$models_root/qwen-vl" >&2
  exit 1
fi
if [[ -z "$(find "$models_root/qwen3" -mindepth 1 -print -quit)" ]]; then
  printf 'Qwen3 model directory is empty: %s\n' "$models_root/qwen3" >&2
  exit 1
fi

export IDP_SOURCE_ROOT="$project_root"
export IDP_INPUT_ROOT="$input_root"
export IDP_DATA_ROOT="$data_root"
export IDP_MODELS_ROOT="$models_root"
export IDP_TOOLS_ROOT="$tools_root"
export IDP_APP_IMAGE="$(jq -er '.app_image' "$metadata_path")"
export IDP_POSTGRES_IMAGE="$(jq -er '.postgres_image' "$metadata_path")"
export IDP_MINIO_IMAGE="$(jq -er '.minio_image' "$metadata_path")"
export IDP_MINIO_MC_IMAGE="$(jq -er '.minio_mc_image' "$metadata_path")"
export IDP_QWEN_VL_IMAGE="$(jq -er '.qwen_vl_image' "$metadata_path")"
export IDP_QWEN3_IMAGE="$(jq -er '.qwen3_image' "$metadata_path")"
export IDP_PIPELINE_PROFILE_VERSION="$(jq -er '.pipeline_profile_version' "$metadata_path")"

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
IDP_PIPELINE_PROFILE_VERSION=${IDP_PIPELINE_PROFILE_VERSION}
EOF

cd "$project_root"
docker compose -f infra/compose/local.yml --profile models up -d
docker compose -f infra/compose/local.yml run --rm operator idp healthcheck

printf '%s\n' 'Images imported and full IDP stack started.'
printf 'Copy PDFs to: %s\n' "$input_root"
printf '%s\n' 'Submit them with:'
printf '%s\n' 'docker compose -f infra/compose/local.yml run --rm operator idp batch submit /input --profile default'
