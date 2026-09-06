"""Download Qwen3.8-27B-FP8 model.

Model path inside container: /models/new_vl
Local project path:        transfer/models/new_vl/
HF cache:                  transfer/models/new_vl/hf-cache/
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "true")

REPO_ID = "Qwen/Qwen3.8-27B-FP8"

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
MODELS_ROOT = PROJECT_ROOT / "transfer" / "models"
MODEL_DIR = MODELS_ROOT / "new_vl"
HF_CACHE = MODEL_DIR / "hf-cache"


def main() -> int:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    HF_CACHE.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[pip] Installing huggingface_hub...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub"])
        from huggingface_hub import snapshot_download

    print(f"[dl] Repo:   {REPO_ID}")
    print(f"[dl] Target: {MODEL_DIR}")
    print(f"[dl] Cache:  {HF_CACHE}")

    snapshot_download(
        repo_id=REPO_ID,
        cache_dir=str(HF_CACHE),
        local_dir=str(MODEL_DIR),
    )

    print(f"[dl] Done. Files at {MODEL_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
