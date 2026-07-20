#!/usr/bin/env bash
# Imports the Windows archive, prepares mounted paths, and starts the full Compose stack.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
archive_path="${project_root}/transfer/idp-images.tar"
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

data_root="${IDP_DATA_ROOT:-${project_root}/data/runtime}"
input_root="${IDP_INPUT_ROOT:-${project_root}/data/input}"
models_root="${IDP_MODELS_ROOT:-${project_root}/data/models}"
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
IDP_PIPELINE_PROFILE_VERSION=${IDP_PIPELINE_PROFILE_VERSION}
IDP_POSTGRES_PASSWORD=${IDP_POSTGRES_PASSWORD}
IDP_MINIO_ACCESS_KEY=${IDP_MINIO_ACCESS_KEY}
IDP_MINIO_SECRET_KEY=${IDP_MINIO_SECRET_KEY}
EOF

cd "$project_root"
docker compose -f infra/compose/local.yml --profile models up -d
docker compose -f infra/compose/local.yml run --rm operator idp healthcheck

controller_id="$(docker compose -f infra/compose/local.yml ps -q controller)"
network_id="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}' "$controller_id")"
if [[ "$(docker network inspect --format '{{.Internal}}' "$network_id")" != "true" ]]; then
  printf '%s\n' 'Compose runtime network is not internal.' >&2
  exit 1
fi
if docker compose -f infra/compose/local.yml run --rm --no-deps operator \
  python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)" \
  >/dev/null 2>&1; then
  printf '%s\n' 'External internet access unexpectedly succeeded.' >&2
  exit 1
fi
printf '%s\n' 'Offline check passed: runtime network is internal and external HTTPS is blocked.'

if find "$input_root" -type f -iname '*.pdf' -print -quit | grep -q .; then
  printf '%s\n' 'Submitting PDFs from data/input...'
  docker compose -f infra/compose/local.yml run --rm operator \
    idp batch submit /input --profile default
else
  printf 'No PDF files found in %s; stack is running without a batch.\n' "$input_root"
fi

printf '%s\n' 'Images imported and full IDP stack started.'
printf 'Copy PDFs to: %s\n' "$input_root"
printf '%s\n' 'Results: MinIO Console http://localhost:9001, bucket idp-artifacts.'
printf 'MinIO login: %s / %s\n' "$IDP_MINIO_ACCESS_KEY" "$IDP_MINIO_SECRET_KEY"
printf '%s\n' 'Result prefix: results/<item_id>/<source_sha256>/.'
printf '%s\n' 'To submit PDFs added later:'
printf '%s\n' 'docker compose -f infra/compose/local.yml run --rm operator idp batch submit /input --profile default'
