"""Replay the ball tracker against a fixture manifest — A/B BallTracker vs WASB.

Loads a `<task>.json` manifest (produced by snapshot_task_ball OR hand-written)
and runs ONE tracker over the configured frame windows. Returns a metrics dict
with detection_rate, sa_bounce_recall, runtime, and (for TrackNetV2) the per-
tier diagnostic counters.

Use this for fast iteration: edit `ball_tracker.py` or `wasb_ball_tracker.py` →

    python -m ml_pipeline.diag.replay_ball ml_pipeline/fixtures_ball/<task>.json
    python -m ml_pipeline.diag.replay_ball ... --tracker wasb

A single tracker run over ~1000 frames takes ~20-30s on GPU, ~5-10x longer on
CPU. The bench (`bench_ball.py`) wraps this for multi-fixture × multi-tracker
regression checks vs `bench_ball_baseline.json`.

The manifest schema (see ml_pipeline/fixtures_ball/README or `snapshot_task_ball.py`):

    {
      "task_id": "a798eff0-...",
      "video_local_path": "ml_pipeline/test_videos/a798eff0_sa_video.mp4",
      "fps": 25.0,
      "windows": [{"name": "warmup", "start_frame": 0, "n_frames": 300}, ...],
      "sa_bounce_frames": [1234, 1267, ...]   # frame indices of SA-confirmed bounces
    }
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

import numpy as np

from ml_pipeline.video_preprocessor import VideoPreprocessor


logger = logging.getLogger(__name__)


# Default ±frames window for matching a tracker detection to an SA bounce frame.
# 3 frames = 120ms at 25fps — tight enough that "ball detected in window" really
# does mean the tracker saw the ball at the bounce moment, loose enough to
# survive single-frame jitter in either source.
SA_BOUNCE_TOLERANCE_FRAMES = 3


@dataclass
class BenchDetection:
    """Normalised detection across BallTracker / WASBBallTracker."""
    frame_idx: int
    x: float
    y: float
    score: Optional[float] = None  # WASB only


def _load_fixture(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"fixture not found: {path}")
    with open(p) as f:
        fix = json.load(f)
    for k in ("task_id", "video_local_path", "fps", "windows"):
        if k not in fix:
            raise SystemExit(f"fixture missing required key: {k}")
    fix.setdefault("sa_bounce_frames", [])
    return fix


def _decode_windows(
    video_path: str, windows: list[dict], target_fps: int,
) -> Iterator[tuple[str, int, np.ndarray]]:
    """Yield (window_name, sampled_frame_idx, frame) for frames inside any window.

    Frames are subsampled to ``target_fps`` to match production's frame_idx
    space (the indices stored in ``ml_analysis.ball_detections``). H.264
    keyframe seeking is unreliable across decoders, so this iterates from
    frame 0 — bounded by the last window so we exit early instead of decoding
    the whole match.
    """
    if not Path(video_path).exists():
        raise SystemExit(f"video not found: {video_path}")

    sorted_windows = sorted(windows, key=lambda w: w["start_frame"])
    if not sorted_windows:
        return

    last_frame_needed = max(w["start_frame"] + w["n_frames"] - 1 for w in sorted_windows)

    preproc = VideoPreprocessor(video_path, target_fps=target_fps)

    for fi, frame in enumerate(preproc.frames()):
        if fi > last_frame_needed:
            break
        for w in sorted_windows:
            start = w["start_frame"]
            end = start + w["n_frames"]
            if start <= fi < end:
                yield w["name"], fi, frame
                break


def _make_tracker(tracker_name: str):
    """Instantiate the requested tracker. Lazy-imports torch only when needed."""
    if tracker_name in ("tracknet", "tracknet_v2", "v2"):
        from ml_pipeline.ball_tracker import BallTracker
        return BallTracker(), "tracknet_v2"
    if tracker_name in ("wasb", "wasb_sbdt"):
        from ml_pipeline.wasb_ball_tracker import WASBBallTracker
        return WASBBallTracker(), "wasb"
    raise SystemExit(f"unknown tracker: {tracker_name}")


def _normalise_detection(det, frame_idx: int) -> Optional[BenchDetection]:
    """Convert tracker output → BenchDetection. None pass-through."""
    if det is None:
        return None
    # BallTracker returns a BallDetection dataclass; WASBBallTracker returns a dict
    if hasattr(det, "frame_idx"):
        return BenchDetection(frame_idx=det.frame_idx, x=det.x, y=det.y)
    if isinstance(det, dict):
        return BenchDetection(
            frame_idx=det["frame_idx"], x=det["x"], y=det["y"],
            score=det.get("score"),
        )
    return None


def _compute_metrics(
    detections: List[BenchDetection],
    sa_bounce_frames: list[int],
    total_frames_processed: int,
    runtime_sec: float,
    diag: Optional[dict] = None,
    tolerance: int = SA_BOUNCE_TOLERANCE_FRAMES,
) -> dict:
    """Reduce a tracker run to the comparison numbers the bench cares about."""
    detected_frames = {d.frame_idx for d in detections}
    n_det = len(detected_frames)
    rate = n_det / total_frames_processed if total_frames_processed else 0.0

    sa_hits = 0
    sa_misses: list[int] = []
    for bf in sa_bounce_frames:
        window_hit = any(
            abs(df - bf) <= tolerance for df in detected_frames
        )
        if window_hit:
            sa_hits += 1
        else:
            sa_misses.append(bf)
    sa_recall = sa_hits / len(sa_bounce_frames) if sa_bounce_frames else None

    metrics: dict = {
        "frames_processed": total_frames_processed,
        "detections": n_det,
        "detection_rate": round(rate, 4),
        "sa_bounce_total": len(sa_bounce_frames),
        "sa_bounce_hits": sa_hits,
        "sa_bounce_recall": (round(sa_recall, 4) if sa_recall is not None else None),
        "sa_bounce_misses": sa_misses,
        "runtime_sec": round(runtime_sec, 2),
    }

    # TrackNetV2 exposes per-tier diagnostics; surface them for diagnosis but
    # don't use them in the verdict (the verdict is detection_rate + recall).
    if diag is not None:
        keep = {
            "frames_inferred",
            "heatmap_empty",
            "tier1_hough",
            "tier2_cc",
            "tier2_cc_rejected_size",
            "tier3_argmax",
            "none_returned",
            "delta_fallback_hits",
        }
        metrics["tier_dist"] = {k: int(diag.get(k, 0)) for k in keep}

    return metrics


def replay(fixture: dict, tracker_name: str = "tracknet_v2") -> dict:
    """Run one tracker over one fixture's windows. Returns a metrics dict."""
    tracker, normalised_name = _make_tracker(tracker_name)

    detections: List[BenchDetection] = []
    frames_processed = 0

    target_fps = int(round(float(fixture.get("fps", 25.0))))

    t0 = time.time()
    for window_name, frame_idx, frame in _decode_windows(
        fixture["video_local_path"], fixture["windows"], target_fps=target_fps,
    ):
        frames_processed += 1
        det = tracker.detect_frame(frame, frame_idx)
        norm = _normalise_detection(det, frame_idx)
        if norm is not None:
            detections.append(norm)
    runtime = time.time() - t0

    diag = getattr(tracker, "_diag", None)

    metrics = _compute_metrics(
        detections=detections,
        sa_bounce_frames=fixture.get("sa_bounce_frames", []),
        total_frames_processed=frames_processed,
        runtime_sec=runtime,
        diag=diag,
    )
    metrics["tracker"] = normalised_name
    metrics["task_id"] = fixture["task_id"]
    return metrics


def _print_report(metrics: dict) -> None:
    print(f"=== replay_ball task={metrics['task_id'][:8]} "
          f"tracker={metrics['tracker']} ===")
    print(f"  frames_processed:   {metrics['frames_processed']}")
    print(f"  detections:         {metrics['detections']}")
    print(f"  detection_rate:     {metrics['detection_rate']:.2%}")
    if metrics["sa_bounce_total"]:
        recall = metrics["sa_bounce_recall"]
        recall_s = f"{recall:.2%}" if recall is not None else "n/a"
        print(f"  sa_bounce_recall:   {recall_s}  "
              f"({metrics['sa_bounce_hits']}/{metrics['sa_bounce_total']})")
        if metrics["sa_bounce_misses"]:
            shown = metrics["sa_bounce_misses"][:5]
            extra = "" if len(metrics["sa_bounce_misses"]) <= 5 else \
                f" ...+{len(metrics['sa_bounce_misses'])-5} more"
            print(f"  sa_bounce_misses:   {shown}{extra}")
    print(f"  runtime_sec:        {metrics['runtime_sec']:.2f}s")
    if "tier_dist" in metrics:
        td = metrics["tier_dist"]
        print(f"  tier_dist:          "
              f"hough={td['tier1_hough']} cc={td['tier2_cc']} "
              f"argmax={td['tier3_argmax']} delta={td['delta_fallback_hits']} "
              f"empty={td['heatmap_empty']} none={td['none_returned']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", help="Path to ml_pipeline/fixtures_ball/<task>.json")
    ap.add_argument("--tracker", default="tracknet_v2",
                    choices=["tracknet_v2", "tracknet", "v2", "wasb", "wasb_sbdt"],
                    help="Which tracker to replay (default tracknet_v2)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    fixture = _load_fixture(args.fixture)
    metrics = replay(fixture, tracker_name=args.tracker)
    _print_report(metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
