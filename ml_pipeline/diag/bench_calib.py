"""bench_calib — court calibration regression harness (2026-05-28).

Offline, deterministic: feeds committed fixture frames (ml_pipeline/
fixtures_calib/) to the real CourtDetector and asserts the frame-selection
+ degeneracy-gate fix (G + B) behaves correctly per camera class:

  • indoor_matchi  — wide indoor MATCHi court (bench-equivalent) → must LOCK
                     a VALIDATED detection with a lens calibration.
  • outdoor_club   — match-4 outdoor club rally frames → must LOCK VALIDATED
                     (proves the green/red dawn court calibrates).
  • window_trap    — match-4 OPENING frames only (CNN finds ~0 keypoints) →
                     must NOT lock a degenerate Hough fabrication. This is the
                     regression guard for the actual silent-degeneracy bug.
  • self_heal      — window_trap THEN outdoor_club → must LOCK VALIDATED once
                     rally footage arrives (the production self-heal).

Local-only, like bench_ball / bench_silver — NOT a CI trigger.
Run: python -m ml_pipeline.diag.bench_calib
"""
import os
import sys
import glob
import logging

import cv2

logging.disable(logging.WARNING)
from ml_pipeline.court_detector import CourtDetector

FX = os.path.join(os.path.dirname(__file__), "..", "fixtures_calib")
# Step > COURT_CALIBRATION_FRAMES/ (#frames) so the fed sequence crosses the
# 300-frame window and accumulates >= COURT_MIN_CALIB_OBS observations.
IDX_STEP = 50


def _load(prefix):
    files = sorted(
        glob.glob(os.path.join(FX, f"{prefix}_*.jpg")),
        key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]),
    )
    return [cv2.imread(f) for f in files]


def _run(frames):
    det = CourtDetector()
    raised = False
    for i, img in enumerate(frames):
        if img is None:
            continue
        try:
            det.detect(img, frame_idx=i * IDX_STEP)
        except RuntimeError:
            raised = True  # fail-loud fired (acceptable for a truly-bad sequence)
            break
    locked = det._locked_detection is not None
    cal = None if det._calibration is None else det._calibration.mode
    return det, locked, cal, raised


def main():
    results = []

    det, locked, cal, _ = _run(_load("indoor_matchi"))
    results.append(("indoor_matchi", "LOCK + calibration",
                    locked and det._calibration is not None, f"locked={locked} cal={cal}"))

    det, locked, cal, _ = _run(_load("outdoor_club"))
    results.append(("outdoor_club", "LOCK + calibration",
                    locked and det._calibration is not None, f"locked={locked} cal={cal}"))

    # Opening frames only: must refuse to lock a degenerate detection. Either
    # stays unlocked (keeps searching) — never freezes garbage.
    det, locked, cal, raised = _run(_load("window_trap"))
    results.append(("window_trap", "NO degenerate lock", (not locked),
                    f"locked={locked} best_validated={det._best_validated_detection is not None} raised={raised}"))

    det, locked, cal, _ = _run(_load("window_trap") + _load("outdoor_club"))
    results.append(("self_heal", "LOCK after recovery",
                    locked and det._calibration is not None, f"locked={locked} cal={cal}"))

    print("=== bench_calib  (court calibration regression) ===\n")
    all_ok = True
    for name, check, ok, detail in results:
        all_ok = all_ok and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:14s} {check:22s} {detail}")
    print()
    if not all_ok:
        print("[FAIL] calibration regression detected")
        sys.exit(1)
    print("[OK] calibration behaves correctly across all fixture classes")


if __name__ == "__main__":
    main()
