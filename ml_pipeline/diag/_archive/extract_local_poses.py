"""Extract pose data for every Nth frame of a match video.

Writes JSONL where each line is one detection. The big full-frame person
bbox per frame is labelled `role='near'`; the small far-baseline bbox
(if any) is `role='far'`. Intended as ground-truth pose data for the
serve-detector module so we can validate it WITHOUT waiting for a
Batch rebuild to fix the upstream pose-coverage bug.

Every Nth frame matches the prod PLAYER_DETECTION_INTERVAL=5 cadence so
the volume matches what the DB would contain if the pipeline weren't
dropping detections.

Usage (repo root, .venv active):
    python -m ml_pipeline.diag.extract_local_poses \\
        ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --output ml_pipeline/diag/local_poses_081e089c.jsonl \\
        --every 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
from ultralytics import YOLO


DEFAULT_WEIGHTS = Path("ml_pipeline/models/yolov8x-pose.pt")


def _extract(video: Path, weights: Path, out_path: Path, every: int,
             max_frames: Optional[int] = None) -> dict:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"Could not open video: {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if max_frames:
        total = min(total, max_frames)

    print(f"Video: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))} "
          f"@ {fps:.1f} fps, {total} frames total. Sampling every {every} frames.")
    print(f"Loading {weights}")
    model = YOLO(str(weights))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    near_count = 0
    far_count = 0
    start = time.time()

    with out_path.open("w", encoding="utf-8") as f:
        fi = 0
        while fi < total:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok:
                fi += every
                continue

            results = model(frame, verbose=False, classes=[0])
            persons: List[dict] = []
            for r in results:
                if r.boxes is None or len(r.boxes) == 0:
                    continue
                boxes = r.boxes.xyxy.cpu().numpy()
                confs = r.boxes.conf.cpu().numpy()
                kps_xy = r.keypoints.xy.cpu().numpy() if r.keypoints is not None else None
                kps_cf = r.keypoints.conf.cpu().numpy() if r.keypoints is not None else None
                for i, box in enumerate(boxes):
                    x1, y1, x2, y2 = [float(v) for v in box]
                    w = x2 - x1
                    h = y2 - y1
                    kp_list = None
                    if kps_xy is not None and kps_cf is not None:
                        kp_list = [
                            [float(kps_xy[i][k][0]), float(kps_xy[i][k][1]),
                             float(kps_cf[i][k])]
                            for k in range(17)
                        ]
                    persons.append({
                        "bbox": [x1, y1, x2, y2],
                        "w": w,
                        "h": h,
                        "cx": (x1 + x2) / 2,
                        "cy": (y1 + y2) / 2,
                        "det_conf": float(confs[i]),
                        "kps": kp_list,
                    })

            # Role assignment by bbox size — the near player is always the
            # BIGGEST person in the frame (100-250 px wide), the far player
            # is the smallest (30-70 px). Anyone in between is likely mid-
            # court. Heuristic: biggest bbox = near; smallest-and-small bbox
            # (w < 80, cy < 400) = far. This is fine for a dev tool; prod
            # pipeline uses full court-projection scoring.
            if persons:
                persons.sort(key=lambda p: p["w"] * p["h"], reverse=True)
                for role, p in [("near", persons[0])]:
                    row = {
                        "frame_idx": fi,
                        "ts": fi / fps,
                        "role": role,
                        **{k: v for k, v in p.items() if k != "bbox"},
                        "bbox": p["bbox"],
                    }
                    f.write(json.dumps(row) + "\n")
                    written += 1
                    near_count += 1
                # Far player candidate — small bbox in upper half of frame
                for p in persons[1:]:
                    if p["w"] < 80 and p["cy"] < 400:
                        row = {
                            "frame_idx": fi,
                            "ts": fi / fps,
                            "role": "far",
                            **{k: v for k, v in p.items() if k != "bbox"},
                            "bbox": p["bbox"],
                        }
                        f.write(json.dumps(row) + "\n")
                        written += 1
                        far_count += 1
                        break

            fi += every
            if fi % (every * 100) == 0:
                elapsed = time.time() - start
                pct = 100 * fi / total
                eta = elapsed / max(fi, 1) * (total - fi)
                print(f"  frame {fi}/{total} ({pct:.1f}%)  elapsed={elapsed:.0f}s  eta={eta:.0f}s  written={written}")

    cap.release()
    elapsed = time.time() - start
    print(f"\nDONE in {elapsed:.1f}s → {out_path}")
    print(f"  near detections: {near_count}")
    print(f"  far detections:  {far_count}")
    return {"near": near_count, "far": far_count, "elapsed": elapsed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--output", required=True)
    ap.add_argument("--every", type=int, default=5)
    ap.add_argument("--max-frames", type=int, default=None)
    args = ap.parse_args(argv)

    _extract(Path(args.video), Path(args.weights), Path(args.output),
             args.every, args.max_frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
