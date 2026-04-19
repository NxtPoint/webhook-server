"""prod_pose_audit.py — reproduce Batch's frame iteration locally and compare
YOLOv8x-pose output against ml_analysis.player_detections to discriminate
the three density-gap hypotheses from 2026-04-19.

Hypotheses (from handover_t5.md P0 section):
  H1. Pipeline preprocessing differs from isolated _run_yolo calls.
  H2. _choose_two_players / upstream filter drops pose-carrying bboxes.
  H3. cv2 seek vs sequential-read landed on different actual video frames.

This script reads the video SEQUENTIALLY using the same fps-downsampling
logic as VideoPreprocessor.frames() (source_fps / target_fps stride), so
yielded_idx N here corresponds to the same yielded_idx N that Batch wrote
into ml_analysis.player_detections. For each sampled frame, we also seek
to the same yielded_idx via cap.set(CAP_PROP_POS_FRAMES, N) and compare —
any pixel delta between the two is evidence for H3.

We run YOLOv8x-pose on each frame (same weights + imgsz Batch uses) and
check whether a pose-carrying near-half bbox exists. If it does locally
but ml_analysis has no pid=0 row for that frame_idx, the gap is in the
prod pipeline (H1 or H2), not the model's fundamental capability.

Output: per-frame table + aggregate stats + hypothesis verdict.

Usage (repo root, .venv active, DATABASE_URL set):
    python -m ml_pipeline.diag.prod_pose_audit \\
        --task f181aaf7-6862-4364-bd03-7e92ff5346e9 \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --start-frame 4500 --end-frame 6000 --every 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, text as sql_text

from ml_pipeline.config import (
    FRAME_SAMPLE_FPS,
    PLAYER_DETECTION_INTERVAL,
    YOLO_CONFIDENCE,
    YOLO_IMGSZ,
)

# cv2, numpy, ultralytics are heavy deps not installed on Render webhook-server.
# Import lazily inside the YOLO-comparison path so --fetch-db-only works without them.


DEFAULT_VIDEO = Path("ml_pipeline/test_videos/match_90ad59a8.mp4.mp4")
DEFAULT_WEIGHTS = Path("ml_pipeline/models/yolov8x-pose.pt")
DEFAULT_TASK = "f181aaf7-6862-4364-bd03-7e92ff5346e9"


def _lazy_cv2():
    import cv2
    return cv2


def _lazy_numpy():
    import numpy as np
    return np


def _iter_sequential_frames(video_path: Path, target_fps: int,
                            yielded_indices_wanted: set):
    """Iterate video sequentially with VideoPreprocessor's exact downsampling.

    Yields (yielded_idx, source_idx, frame) only for the indices wanted,
    but MUST walk the capture sequentially from frame 0 so the source/target
    frame alignment matches Batch.
    """
    cv2 = _lazy_cv2()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise SystemExit(f"Could not open video: {video_path}")
    source_fps = cap.get(cv2.CAP_PROP_FPS) or float(target_fps)
    if target_fps < source_fps:
        frame_interval = source_fps / target_fps
    else:
        frame_interval = 1.0

    yielded_idx = 0
    source_frame_idx = 0
    next_sample_at = 0.0
    max_wanted = max(yielded_indices_wanted) if yielded_indices_wanted else -1

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if source_frame_idx >= next_sample_at:
                if yielded_idx in yielded_indices_wanted:
                    yield yielded_idx, source_frame_idx, frame
                yielded_idx += 1
                next_sample_at += frame_interval
                if yielded_idx > max_wanted:
                    break
            source_frame_idx += 1
    finally:
        cap.release()


def _seek_read(video_path: Path, yielded_idx: int, target_fps: int):
    """Read via cap.set(POS_FRAMES, source_idx) — matches the old probe path.

    yielded_idx → source_idx via the same ratio VideoPreprocessor uses. This
    is the frame we'd hit if we used the keyframe-seek shortcut instead of
    iterating sequentially. If the content differs from the sequential read,
    H3 is real.
    """
    cv2 = _lazy_cv2()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    try:
        source_fps = cap.get(cv2.CAP_PROP_FPS) or float(target_fps)
        if target_fps < source_fps:
            frame_interval = source_fps / target_fps
        else:
            frame_interval = 1.0
        # Replicate VideoPreprocessor's cumulative-threshold math: the k-th
        # yielded frame is read at the smallest source_idx where
        # source_idx >= k * frame_interval (approximately; the exact rule is
        # while-loop accumulation). For fractional intervals we want floor.
        source_idx = int(yielded_idx * frame_interval)
        cap.set(cv2.CAP_PROP_POS_FRAMES, source_idx)
        ret, frame = cap.read()
        return frame if ret else None
    finally:
        cap.release()


def _run_yolo(model, frame):
    """Run YOLOv8x-pose at the same imgsz + confidence Batch uses."""
    results = model.predict(frame, conf=YOLO_CONFIDENCE, imgsz=YOLO_IMGSZ, verbose=False)
    if not results:
        return []
    r = results[0]
    if r.boxes is None or len(r.boxes) == 0:
        return []
    boxes = r.boxes.xyxy.cpu().numpy()
    kps_xy = r.keypoints.xy.cpu().numpy() if r.keypoints is not None else None
    kps_cf = r.keypoints.conf.cpu().numpy() if r.keypoints is not None else None
    out = []
    for i, box in enumerate(boxes):
        x1, y1, x2, y2 = [float(v) for v in box]
        has_pose = False
        pose_score = 0.0
        if kps_xy is not None and kps_cf is not None and i < len(kps_cf):
            # YOLOv8x-pose returns keypoints for every detection — what
            # distinguishes "pose carrying" from "pose suppressed" is
            # whether any keypoints have meaningful confidence. Use the
            # same >0 check Ultralytics uses internally.
            conf_arr = kps_cf[i]
            has_pose = bool((conf_arr > 0).any())
            pose_score = float(conf_arr.mean()) if has_pose else 0.0
        out.append({
            "bbox": (x1, y1, x2, y2),
            "cx": (x1 + x2) / 2,
            "cy": (y1 + y2) / 2,
            "w": x2 - x1,
            "h": y2 - y1,
            "has_pose": has_pose,
            "pose_score": pose_score,
        })
    return out


def _near_half_pose_bboxes(dets, frame_h):
    """Return pose-carrying detections whose bbox center is in the near half."""
    midline = frame_h / 2
    return [d for d in dets if d["has_pose"] and d["cy"] > midline]


def _fetch_db_detections(engine, task_id: str, frame_min: int, frame_max: int):
    """Pull Batch-stored player_detections for a frame window."""
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT frame_idx, player_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                   center_x, center_y, court_x, court_y,
                   (keypoints IS NOT NULL) AS has_keypoints
            FROM ml_analysis.player_detections
            WHERE job_id = :tid
              AND frame_idx BETWEEN :lo AND :hi
            ORDER BY frame_idx, player_id
        """), {"tid": task_id, "lo": frame_min, "hi": frame_max}).fetchall()
    # Group by frame_idx
    by_frame = {}
    for r in rows:
        by_frame.setdefault(int(r.frame_idx), []).append({
            "player_id": int(r.player_id),
            "bbox": (float(r.bbox_x1), float(r.bbox_y1),
                     float(r.bbox_x2), float(r.bbox_y2)),
            "center": (float(r.center_x), float(r.center_y)),
            "court_y": float(r.court_y) if r.court_y is not None else None,
            "has_keypoints": bool(r.has_keypoints),
        })
    return by_frame


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=DEFAULT_TASK,
                    help=f"Task UUID to compare against (default: {DEFAULT_TASK})")
    ap.add_argument("--video", default=str(DEFAULT_VIDEO))
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--start-frame", type=int, default=4500,
                    help="First yielded_idx to audit (default 4500 = minute 3)")
    ap.add_argument("--end-frame", type=int, default=6000,
                    help="Last yielded_idx (exclusive) to audit (default 6000 = minute 4)")
    ap.add_argument("--every", type=int, default=PLAYER_DETECTION_INTERVAL,
                    help="Stride between sampled frames (default = PLAYER_DETECTION_INTERVAL=5)")
    ap.add_argument("--no-seek-compare", action="store_true",
                    help="Skip the seek-read H3 comparison (faster)")
    ap.add_argument("--verbose", action="store_true",
                    help="Print every sampled frame, not just the gap rows")
    ap.add_argument("--fetch-db-only", action="store_true",
                    help="Skip all YOLO work — just query the DB and print "
                         "density stats + dump rows to JSON. Runs cleanly on "
                         "Render webhook-server (no cv2 / ultralytics / weights).")
    ap.add_argument("--db-json", default=None,
                    help="Path to a JSON file produced by --fetch-db-only. "
                         "When set, DATABASE_URL is NOT required — rows are "
                         "loaded from the file. Use this to run the YOLO side "
                         "locally when your laptop can't reach the prod DB.")
    args = ap.parse_args(argv)

    db_url = None
    if not args.db_json:
        db_url = (
            os.environ.get("DATABASE_URL")
            or os.environ.get("POSTGRES_URL")
            or os.environ.get("DB_URL")
        )
        if not db_url:
            print("DATABASE_URL env var required (or pass --db-json)", file=sys.stderr)
            return 2
        # Normalize scheme + force psycopg v3 driver (matches db_init.py). The
        # Render webhook-server installs psycopg (v3) only, not psycopg2, so a
        # bare postgresql:// URL makes SQLAlchemy try to import psycopg2 and fail.
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        if db_url.startswith("postgresql://") and "+psycopg" not in db_url:
            db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    if not args.fetch_db_only:
        video = Path(args.video)
        if not video.exists():
            print(f"Missing video: {video}", file=sys.stderr)
            return 2

        weights = Path(args.weights)
        if not weights.exists():
            print(f"Missing YOLO weights: {weights}", file=sys.stderr)
            return 2
    else:
        video = None
        weights = None

    target_indices = set(range(args.start_frame, args.end_frame, args.every))
    print(f"=== prod_pose_audit ===")
    print(f"  task          {args.task}")
    print(f"  mode          {'FETCH-DB-ONLY (no YOLO)' if args.fetch_db_only else 'full YOLO vs DB'}")
    print(f"  video         {video if video else '-'}")
    print(f"  frame window  [{args.start_frame}, {args.end_frame})  every {args.every}")
    print(f"  samples       {len(target_indices)}")
    if not args.fetch_db_only:
        print(f"  YOLO          imgsz={YOLO_IMGSZ} conf={YOLO_CONFIDENCE} weights={weights.name}")

        cv2 = _lazy_cv2()
        cap = cv2.VideoCapture(str(video))
        source_fps = cap.get(cv2.CAP_PROP_FPS)
        total_src = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        cap.release()
        print(f"  video meta    {frame_w}x{frame_h} @ {source_fps:.2f} fps (source), "
              f"target={FRAME_SAMPLE_FPS} fps, total_src={total_src} frames")
    print()

    if args.db_json:
        dbj = json.loads(Path(args.db_json).read_text())
        db_by_frame = {}
        for yi_str, rows in dbj.get("by_frame", {}).items():
            db_by_frame[int(yi_str)] = [
                {"player_id": r["player_id"],
                 "bbox": tuple(r["bbox"]),
                 "center": tuple(r["center"]),
                 "court_y": r.get("court_y"),
                 "has_keypoints": bool(r["has_keypoints"])}
                for r in rows
            ]
        print(f"Loaded DB rows from {args.db_json}")
    else:
        engine = create_engine(db_url)
        db_by_frame = _fetch_db_detections(
            engine, args.task, args.start_frame, args.end_frame,
        )
    total_db_rows = sum(len(v) for v in db_by_frame.values())
    print(f"Have {total_db_rows} player_detections rows across "
          f"{len(db_by_frame)} distinct frames.")
    print()

    # ── DB-only density report ─────────────────────────────────────────────
    # Runs regardless of mode. In --fetch-db-only we stop here; otherwise
    # we continue to the YOLO vs DB comparison below.
    print("=== DB DENSITY BY SAMPLED FRAME ===")
    target_sorted = sorted(target_indices)
    n_pid0 = 0
    n_pid0_pose = 0
    n_pid1 = 0
    n_pid1_pose = 0
    for yi in target_sorted:
        rows = db_by_frame.get(yi, [])
        if any(r["player_id"] == 0 for r in rows):
            n_pid0 += 1
        if any(r["player_id"] == 0 and r["has_keypoints"] for r in rows):
            n_pid0_pose += 1
        if any(r["player_id"] == 1 for r in rows):
            n_pid1 += 1
        if any(r["player_id"] == 1 and r["has_keypoints"] for r in rows):
            n_pid1_pose += 1

    nsamp = max(1, len(target_sorted))
    print(f"  pid=0 (near) present in DB:       {n_pid0}/{nsamp}  "
          f"({100*n_pid0/nsamp:.1f}%)")
    print(f"    of those, with keypoints:       {n_pid0_pose}/{nsamp}  "
          f"({100*n_pid0_pose/nsamp:.1f}%)")
    print(f"  pid=1 (far) present in DB:        {n_pid1}/{nsamp}  "
          f"({100*n_pid1/nsamp:.1f}%)")
    print(f"    of those, with keypoints:       {n_pid1_pose}/{nsamp}  "
          f"({100*n_pid1_pose/nsamp:.1f}%)")
    print()

    # Also break down by minute for comparison against the Apr 19 memo table.
    # At 25fps, minute N = frames [N*1500, (N+1)*1500).
    print("  Per-minute pid=0 density in window:")
    minute_buckets = {}
    for yi in target_sorted:
        minute = yi // 1500
        bkt = minute_buckets.setdefault(minute, {"samples": 0, "pid0": 0, "pose": 0})
        bkt["samples"] += 1
        rows = db_by_frame.get(yi, [])
        if any(r["player_id"] == 0 for r in rows):
            bkt["pid0"] += 1
        if any(r["player_id"] == 0 and r["has_keypoints"] for r in rows):
            bkt["pose"] += 1
    for m in sorted(minute_buckets):
        b = minute_buckets[m]
        pct = 100 * b["pid0"] / max(1, b["samples"])
        pose_pct = 100 * b["pose"] / max(1, b["samples"])
        print(f"    minute {m}:  pid0={b['pid0']}/{b['samples']} ({pct:.1f}%)  "
              f"pose={b['pose']}/{b['samples']} ({pose_pct:.1f}%)")
    print()

    if args.fetch_db_only:
        # Dump the DB rows so the YOLO-side run (done locally) can load them.
        out_path = Path("ml_pipeline/diag") / f"prod_pose_audit_dbrows_{args.task[:8]}.json"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            serializable = {
                str(yi): [
                    {"player_id": r["player_id"],
                     "bbox": list(r["bbox"]),
                     "center": list(r["center"]),
                     "court_y": r["court_y"],
                     "has_keypoints": r["has_keypoints"]}
                    for r in rows
                ]
                for yi, rows in db_by_frame.items()
            }
            out_path.write_text(json.dumps({
                "task": args.task,
                "window": [args.start_frame, args.end_frame],
                "every": args.every,
                "target_indices": target_sorted,
                "by_frame": serializable,
            }, indent=2))
            print(f"DB rows dumped to {out_path}")
            print(f"  Copy this file to your local box to run the YOLO-vs-DB")
            print(f"  comparison without DATABASE_URL:")
            print(f"    python -m ml_pipeline.diag.prod_pose_audit \\")
            print(f"        --db-json {out_path.name} --video <path>")
        except Exception as e:
            print(f"Failed to write DB rows JSON: {e}", file=sys.stderr)
        return 0

    np = _lazy_numpy()
    from ultralytics import YOLO
    print(f"Loading YOLO weights...")
    model = YOLO(str(weights))

    # Counters
    n_samples = 0
    n_local_near_pose = 0      # local YOLO has pose-carrying bbox in near half
    n_db_has_pid0 = 0          # DB has any pid=0 row for this frame
    n_db_pid0_pose = 0         # DB's pid=0 row has keypoints
    n_local_yes_db_no = 0      # local finds near-pose; DB has no pid=0
    n_local_yes_db_no_pose = 0 # local finds near-pose; DB has pid=0 WITHOUT pose
    n_both_present = 0
    n_neither = 0
    n_seek_pixel_diff = 0      # H3: sequential vs seek frame content differs
    n_seek_yolo_diff = 0       # H3: sequential vs seek YOLO pose outcome differs

    print(f"{'yidx':>6}  {'seq_near_pose':>13}  {'seek_near_pose':>14}  "
          f"{'db_pid0':>8}  {'db_pose':>8}  {'verdict'}")
    print("-" * 90)

    detail_rows = []
    t0 = time.time()

    for yielded_idx, source_idx, seq_frame in _iter_sequential_frames(
        video, FRAME_SAMPLE_FPS, target_indices,
    ):
        n_samples += 1

        # Sequential-read YOLO
        seq_dets = _run_yolo(model, seq_frame)
        seq_near_pose = _near_half_pose_bboxes(seq_dets, frame_h)
        local_has_near_pose = len(seq_near_pose) > 0
        if local_has_near_pose:
            n_local_near_pose += 1

        # Seek-read YOLO (H3 test): read same yielded_idx via POS_FRAMES seek
        seek_has_near_pose = None
        seek_frame_matches = None
        if not args.no_seek_compare:
            seek_frame = _seek_read(video, yielded_idx, FRAME_SAMPLE_FPS)
            if seek_frame is not None and seek_frame.shape == seq_frame.shape:
                seek_frame_matches = bool(np.array_equal(seq_frame, seek_frame))
                if not seek_frame_matches:
                    n_seek_pixel_diff += 1
                seek_dets = _run_yolo(model, seek_frame)
                seek_has_near_pose = len(_near_half_pose_bboxes(seek_dets, frame_h)) > 0
                if seek_has_near_pose != local_has_near_pose:
                    n_seek_yolo_diff += 1

        # DB state
        db_rows = db_by_frame.get(yielded_idx, [])
        pid0 = next((r for r in db_rows if r["player_id"] == 0), None)
        db_has_pid0 = pid0 is not None
        db_pid0_pose = pid0["has_keypoints"] if pid0 else False
        if db_has_pid0:
            n_db_has_pid0 += 1
        if db_pid0_pose:
            n_db_pid0_pose += 1

        # Gap classification
        if local_has_near_pose and not db_has_pid0:
            n_local_yes_db_no += 1
            verdict = "LOCAL>DB: pid=0 missing"
        elif local_has_near_pose and db_has_pid0 and not db_pid0_pose:
            n_local_yes_db_no_pose += 1
            verdict = "LOCAL>DB: pid=0 has no keypoints"
        elif local_has_near_pose and db_has_pid0 and db_pid0_pose:
            n_both_present += 1
            verdict = "MATCH"
        elif not local_has_near_pose and not db_has_pid0:
            n_neither += 1
            verdict = "both empty"
        else:
            verdict = "DB>LOCAL (unexpected)"

        # Extra detail
        seq_flag = ("YES (%d, cy=%.0f)" % (len(seq_near_pose), seq_near_pose[0]["cy"])
                    if seq_near_pose else "no")
        if args.no_seek_compare:
            seek_flag = "-"
        elif seek_has_near_pose is None:
            seek_flag = "err"
        else:
            marker = "" if seek_frame_matches else " (pix-diff)"
            seek_flag = ("YES" if seek_has_near_pose else "no") + marker

        # Print either all rows (--verbose) or just the "interesting" ones
        interesting = verdict not in ("MATCH", "both empty") or (
            seek_frame_matches is False or seek_has_near_pose != local_has_near_pose
        )
        if args.verbose or interesting:
            print(f"{yielded_idx:>6}  {seq_flag:>13}  {str(seek_flag):>14}  "
                  f"{'yes' if db_has_pid0 else 'no':>8}  "
                  f"{'yes' if db_pid0_pose else 'no':>8}  {verdict}")

        detail_rows.append({
            "yielded_idx": yielded_idx,
            "source_idx": source_idx,
            "local_near_pose": local_has_near_pose,
            "local_near_bbox_count": len(seq_near_pose),
            "seek_near_pose": seek_has_near_pose,
            "seek_pixel_match": seek_frame_matches,
            "db_has_pid0": db_has_pid0,
            "db_pid0_pose": db_pid0_pose,
            "verdict": verdict,
        })

    elapsed = time.time() - t0
    print()
    print("=" * 90)
    print(f"SUMMARY  — {n_samples} samples, {elapsed:.0f}s elapsed "
          f"({elapsed/max(n_samples,1):.2f}s/frame)")
    print("=" * 90)
    print(f"  LOCAL YOLO has pose-carrying near-half bbox:   "
          f"{n_local_near_pose}/{n_samples}  ({100*n_local_near_pose/max(n_samples,1):.1f}%)")
    print(f"  DB has pid=0 row:                              "
          f"{n_db_has_pid0}/{n_samples}  ({100*n_db_has_pid0/max(n_samples,1):.1f}%)")
    print(f"  DB pid=0 row has keypoints:                    "
          f"{n_db_pid0_pose}/{n_samples}  ({100*n_db_pid0_pose/max(n_samples,1):.1f}%)")
    print()
    print(f"  LOCAL found near-pose, DB missing pid=0:       {n_local_yes_db_no}")
    print(f"  LOCAL found near-pose, DB pid=0 w/o keypoints: {n_local_yes_db_no_pose}")
    print(f"  Both in agreement (present):                   {n_both_present}")
    print(f"  Both in agreement (empty):                     {n_neither}")
    if not args.no_seek_compare:
        print()
        print(f"  SEEK vs SEQUENTIAL pixel content differs:      "
              f"{n_seek_pixel_diff}/{n_samples}  ({100*n_seek_pixel_diff/max(n_samples,1):.1f}%)")
        print(f"  SEEK vs SEQUENTIAL YOLO pose outcome differs:  "
              f"{n_seek_yolo_diff}/{n_samples}  ({100*n_seek_yolo_diff/max(n_samples,1):.1f}%)")
    print()

    # Hypothesis verdict
    print("HYPOTHESIS VERDICT")
    print("-" * 90)
    if not args.no_seek_compare and n_seek_pixel_diff > n_samples * 0.1:
        print(f"  H3 LIKELY: seek vs sequential reads produce different actual")
        print(f"  frames in {n_seek_pixel_diff} of {n_samples} samples. The old")
        print(f"  repro_pose_gap.py / extract_local_poses.py scripts were testing")
        print(f"  DIFFERENT frame content than Batch saw. Offline validation numbers")
        print(f"  using cap.set(POS_FRAMES, N) are unreliable for comparing against")
        print(f"  Batch-stored ml_analysis rows.")
        print()
    if n_local_yes_db_no > n_samples * 0.3:
        print(f"  H1 or H2 LIKELY: local YOLO finds a near-half pose bbox in")
        print(f"  {n_local_yes_db_no} frames where ml_analysis has no pid=0 row at all.")
        print(f"  The raw YOLO model CAN see the near player on these frames, but")
        print(f"  something in the Batch pipeline dropped the detection before")
        print(f"  db_writer committed the row.")
        print(f"  Next diag: replay detect_frame() on these specific frames with")
        print(f"  the same court_corners/to_court_coords Batch used (pull from")
        print(f"  ml_analysis.court_detections) — if detect_frame locally ALSO")
        print(f"  returns empty, H2 (scoring drops it). If it returns the bbox,")
        print(f"  H1 (Batch-container-only behavior, e.g. GPU nondeterminism).")
        print()
    if n_local_near_pose < n_samples * 0.1 and n_db_has_pid0 < n_samples * 0.1:
        print(f"  NEITHER HYPOTHESIS — local YOLO ALSO fails to find the near")
        print(f"  player in most frames ({n_local_near_pose}/{n_samples}). The")
        print(f"  density gap is a genuine model-capability / video-content issue,")
        print(f"  not a pipeline bug. Investigate: motion blur? occlusion?")
        print(f"  camera pan? Consider re-running at higher imgsz or a different")
        print(f"  pose weight.")
        print()
    if n_both_present > n_samples * 0.8:
        print(f"  NO GAP — local YOLO and DB agree on {n_both_present}/{n_samples}")
        print(f"  frames. The density numbers in the Apr 19 memo may have been")
        print(f"  computed on a different frame range.")
        print()

    # Dump detail JSON for follow-up analysis
    out_path = Path("ml_pipeline/diag") / f"prod_pose_audit_{args.task[:8]}.json"
    out_path.write_text(json.dumps({
        "task": args.task,
        "video": str(video),
        "window": [args.start_frame, args.end_frame],
        "every": args.every,
        "samples": n_samples,
        "local_near_pose": n_local_near_pose,
        "db_has_pid0": n_db_has_pid0,
        "db_pid0_pose": n_db_pid0_pose,
        "local_yes_db_no": n_local_yes_db_no,
        "seek_pixel_diff": n_seek_pixel_diff,
        "seek_yolo_diff": n_seek_yolo_diff,
        "rows": detail_rows,
    }, indent=2, default=str))
    print(f"Detail dumped to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
