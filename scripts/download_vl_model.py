"""Download Qwen2.5-VL-32B-Instruct-AWQ model per infra/compose/local.yml.

Model path inside container: /models/vl
Local host path:   ../../transfer/models/vl  (relative to infra/compose)
HF cache:          ../../transfer/models/vl/hf-cache
"""

import os
import sys
from pathlib import Path

REPO_ID = "Qwen/Qwen2.5-VL-32B-Instruct-AWQ"

SCRIPT_DIR = Path(__file__).resolve().parent
MODELS_ROOT = (SCRIPT_DIR / ".." / ".." / "transfer" / "models").resolve()
MODEL_DIR = MODELS_ROOT / "vl"
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
        local_dir_use_symlinks=False,
        resume_download=True,
    )

    print(f"[dl] Done. Files at {MODEL_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
