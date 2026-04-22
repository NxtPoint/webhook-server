"""Download WASB pretrained tennis weights.

Weights are not checked into git (6.1 MB, but the convention in this repo
is to exclude *.pth.tar). Run this once on any environment that needs
the WASB ball detector — Render, Docker, local dev.

Usage:
    python -m ml_pipeline.download_wasb_weights

The weights land at ml_pipeline/models/wasb_tennis_best.pth.tar.
Source: https://github.com/nttcom/WASB-SBDT (MIT-licensed, BMVC 2023).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_GDRIVE_ID = "14AeyIOCQ2UaQmbZLNQJa1H_eSwxUXk7z"
_MODELS_DIR = Path(__file__).parent / "models"
_WEIGHTS_PATH = _MODELS_DIR / "wasb_tennis_best.pth.tar"
_EXPECTED_SIZE_MB = 5.8  # 6,102,633 bytes


def main():
    if _WEIGHTS_PATH.exists():
        size_mb = os.path.getsize(_WEIGHTS_PATH) / 1e6
        if size_mb >= _EXPECTED_SIZE_MB:
            print(f"[OK] WASB weights already present: {_WEIGHTS_PATH} ({size_mb:.1f} MB)")
            return 0
        print(f"[WARN] Existing weights look truncated ({size_mb:.1f} MB); re-downloading")

    _MODELS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        import gdown
    except ImportError:
        print("[INFO] installing gdown (one-off)")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "gdown"])
        import gdown

    print(f"[INFO] downloading to {_WEIGHTS_PATH}")
    gdown.download(id=_GDRIVE_ID, output=str(_WEIGHTS_PATH), quiet=False)

    if not _WEIGHTS_PATH.exists():
        print("[ERR] download failed"); return 1
    size_mb = os.path.getsize(_WEIGHTS_PATH) / 1e6
    print(f"[OK] {_WEIGHTS_PATH}: {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
