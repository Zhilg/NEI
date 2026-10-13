"""Download the VL model used by the vllm-vl services.

Default: the Qwen3.8-27B AWQ-INT4 build (``transfer/models/new_vl``).

To try a smaller/faster model (e.g. a 7B build) without touching the default:

    python3 scripts/download_vl_model.py \\
        --repo cyankiwi/Qwen3.8-7B-AWQ-INT4 --model-dir new_vl_7b

then start the stack with the matching overrides:

    IDP_VL_MODEL_DIR=new_vl_7b IDP_VL_MODEL=Qwen3.8-7B \\
        docker compose -f infra/compose/local.yml up

Model path inside container: /models/<model-dir>
Local project path:        transfer/models/<model-dir>/
HF cache:                  transfer/models/<model-dir>/hf-cache/
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "true")

DEFAULT_REPO_ID = "cyankiwi/Qwen3.8-27B-AWQ-INT4"

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
MODELS_ROOT = PROJECT_ROOT / "transfer" / "models"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO_ID, help=f"HuggingFace repo (default: {DEFAULT_REPO_ID})")
    parser.add_argument("--model-dir", default="new_vl", help="subfolder of transfer/models (default: new_vl)")
    args = parser.parse_args()

    model_dir = MODELS_ROOT / args.model_dir
    hf_cache = model_dir / "hf-cache"
    model_dir.mkdir(parents=True, exist_ok=True)
    hf_cache.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[pip] Installing huggingface_hub...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub"])
        from huggingface_hub import snapshot_download

    print(f"[dl] Repo:   {args.repo}")
    print(f"[dl] Target: {model_dir}")
    print(f"[dl] Cache:  {hf_cache}")

    snapshot_download(
        repo_id=args.repo,
        cache_dir=str(hf_cache),
        local_dir=str(model_dir),
    )

    print(f"[dl] Done. Files at {model_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
