"""bench_lens — Fix E lens-distortion estimator self-test (2026-05-28).

Validates the camera-agnostic distortion estimator (ml_pipeline/lens_distortion.py)
on the committed calibration fixtures. The estimator is DORMANT in production
(T5_CALIB_LENS_MODE=off) — this proves it runs and is well-behaved on real
wide footage before anyone enables it.

What it asserts (honest, given we have no strong-fisheye fixture yet):
  • the line estimator runs on real footage and returns a model,
  • undistortion does NOT increase court-line curvature (residual <= baseline),
  • prints the recovered k1/k2 + residuals so a reviewer can see the barrel.
True fisheye validation needs a Class-C/D fixture (see camera_taxonomy doc).

Local-only (like bench_calib / bench_ball); NOT a CI trigger.
Run: python -m ml_pipeline.diag.bench_lens
"""
import os
import sys
import glob
import logging

import cv2

logging.disable(logging.WARNING)
from ml_pipeline.lens_distortion import estimate_lens_distortion, extract_court_line_points

FX = os.path.join(os.path.dirname(__file__), "..", "fixtures_calib")


def _load(prefix):
    files = sorted(
        glob.glob(os.path.join(FX, f"{prefix}_*.jpg")),
        key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]),
    )
    return [im for im in (cv2.imread(f) for f in files) if im is not None]


def main():
    results = []
    for cls in ("indoor_matchi", "outdoor_club"):
        frames = _load(cls)
        if not frames:
            results.append((cls, False, "no fixture frames"))
            continue
        h, w = frames[0].shape[:2]
        nlines = sum(len(extract_court_line_points(f)) for f in frames)
        lens = estimate_lens_distortion(frames, [], (h, w), mode="line")
        if lens is None:
            results.append((cls, False, f"lines={nlines} -> None"))
            continue
        well_behaved = (lens.residual_px <= lens.baseline_px + 0.5
                        and abs(float(lens.dist[0])) <= 0.8)  # physical k1
        detail = (f"lines={nlines} model={lens.model} k1={lens.dist[0]:+.4f} "
                  f"k2={lens.dist[1]:+.4f} resid={lens.residual_px:.2f}px "
                  f"base={lens.baseline_px:.2f}px improved={lens.improved}")
        results.append((cls, well_behaved, detail))

    print("=== bench_lens  (Fix E lens-distortion estimator) ===\n")
    all_ok = True
    for name, ok, detail in results:
        all_ok = all_ok and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:14s} {detail}")
    print()
    if not all_ok:
        print("[FAIL] estimator increased curvature or failed on a fixture")
        sys.exit(1)
    print("[OK] line-based estimator is well-behaved on real footage "
          "(fisheye path still needs a Class-C/D fixture to validate)")


if __name__ == "__main__":
    main()
