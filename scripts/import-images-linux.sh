#!/usr/bin/env bash
# Imports a Docker image archive produced by scripts/export-images-windows.ps1.
set -euo pipefail

archive_path="${1:-./idp-images.tar}"
checksum_path="${archive_path}.sha256"

if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Docker CLI was not found in PATH.' >&2
  exit 1
fi

if [[ ! -f "$archive_path" ]]; then
  printf 'Image archive does not exist: %s\n' "$archive_path" >&2
  exit 1
fi

if [[ -f "$checksum_path" ]]; then
  (cd "$(dirname "$archive_path")" && sha256sum --check "$(basename "$checksum_path")")
else
  printf 'Warning: checksum file not found; importing without SHA-256 check: %s\n' "$archive_path" >&2
fi

docker load --input "$archive_path"
printf 'Imported Docker images from %s\n' "$archive_path"
