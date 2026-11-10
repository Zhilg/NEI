#!/usr/bin/env bash
set -euo pipefail

docker compose -f infra/compose/local.yml run --rm worker idp "$@"
