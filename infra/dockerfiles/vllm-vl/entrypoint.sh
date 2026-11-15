#!/usr/bin/env bash
set -euo pipefail

# vLLM does not accept --quantization none. Xiaomi-OCR-0 (0.8B) ships unquantized,
# so when IDP_VLLM_QUANTIZATION is none/empty we strip --quantization <value>
# from the forwarded args entirely. Otherwise vllm serve receives the CMD as-is
# (it already contains --quantization <valid value>).
if [[ -z "${IDP_VLLM_QUANTIZATION:-}" || "${IDP_VLLM_QUANTIZATION}" == "none" ]]; then
  ARGS=()
  skip_next=0
  for arg in "$@"; do
    if [[ $skip_next -eq 1 ]]; then
      skip_next=0
      continue
    fi
    if [[ "$arg" == "--quantization" ]]; then
      skip_next=1
      continue
    fi
    ARGS+=("$arg")
  done
  exec vllm serve "${ARGS[@]}"
else
  exec vllm serve "$@"
fi
