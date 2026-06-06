"""Per-anchor window features for the serve model.

Pure functions over pre-sorted per-task arrays. Everything is derivable
from ml_analysis tables — NO video access (the corpus source videos are
deleted post-trim, so v1 is feature-based by constraint as well as design).

Feature intuition (what separates a far SERVE from phantom anchors):
- serves start from idle: long bounce-quiet gap BEFORE, activity AFTER
- the far player is present and active at the baseline (pose density)
- the ball appears high in the frame (small image-y) rising then falling
- the resulting bounce lands forward of the near baseline, usually (but
  not always — faults!) near the service box
- the near player (receiver) is comparatively still
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import List, Sequence

import numpy as np

from ml_pipeline.serve_model.candidates import Anchor, HALF_Y

FEATURE_NAMES = [
    # anchor identity
    "is_bounce_anchor", "is_pose_anchor", "merged_both",
    # bounce geometry (0/NaN-filled when no bounce)
    "bounce_court_x", "bounce_court_y", "bounce_in_service_box",
    "bounce_court_y_minus_net", "bounce_coords_known",
    # rally / idle context
    "idle_before_s", "next_bounce_gap_s", "bounces_prev_5s", "bounces_next_5s",
    # far player presence
    "far_pose_rows_pm2s", "roi_rows_pm2s", "roi_burst_rows",
    "far_pose_rows_prev2s", "far_pose_rows_next2s",
    # ball trajectory (image space — calibration-independent)
    "ball_rows_pm2s", "ball_y_min_pm1s", "ball_y_slope_prev1s", "ball_y_slope_next1s",
    "ball_high_frames_pm1s",
    # near player (receiver) context
    "near_pose_rows_pm2s",
    # far-player serve signature from ROI ViTPose keypoints (COCO: 5/6
    # shoulders, 9/10 wrists; image-y is DOWN so raised wrist = smaller y)
    "far_max_arm_raise", "far_wrist_up_frames", "far_trophy_frames",
]

SERVICE_BOX = dict(y_min=HALF_Y - 1.5, y_max=HALF_Y + 6.4 + 1.5,
                   x_min=-0.13, x_max=11.10)  # same slack as serve_detector


def _count(sorted_ts: Sequence[float], lo: float, hi: float) -> int:
    return bisect_right(sorted_ts, hi) - bisect_left(sorted_ts, lo)


def _gap_before(sorted_ts: Sequence[float], t: float, cap: float = 30.0) -> float:
    i = bisect_left(sorted_ts, t - 1e-6)
    return min(cap, t - sorted_ts[i - 1]) if i > 0 else cap


def _gap_after(sorted_ts: Sequence[float], t: float, cap: float = 30.0) -> float:
    i = bisect_right(sorted_ts, t + 1e-6)
    return min(cap, sorted_ts[i] - t) if i < len(sorted_ts) else cap


def _arm_raise_stats(roi_rows: Sequence[dict], t: float,
                     lo: float = -2.0, hi: float = 2.0):
    """Serve-signature stats from far ROI keypoints in [t+lo, t+hi].

    Returns (max_arm_raise, wrist_up_frames, trophy_frames):
    - max_arm_raise: highest (shoulder_y - wrist_y)/bbox_h across the window
      (positive = wrist above shoulder; the serve toss/trophy signature)
    - wrist_up_frames: rows where EITHER wrist is above its shoulder line
    - trophy_frames: rows where BOTH wrists are above the shoulder line
    """
    max_raise, up, trophy = 0.0, 0, 0
    for r in roi_rows:
        ts = r["ts"]
        if ts < t + lo:
            continue
        if ts > t + hi:
            break
        kp = r.get("kp")
        bh = r.get("bbox_h") or 0.0
        if not kp or len(kp) < 11 or bh <= 0:
            continue
        sh_y = min(kp[5][1], kp[6][1])
        w1, w2 = kp[9][1], kp[10][1]
        if min(kp[5][2], kp[6][2], kp[9][2], kp[10][2]) < 0.3:
            continue  # low-confidence keypoints — skip the row
        max_raise = max(max_raise, (sh_y - min(w1, w2)) / bh)
        if min(w1, w2) < sh_y:
            up += 1
        if max(w1, w2) < sh_y:
            trophy += 1
    return max_raise, up, trophy


def featurize(anchor: Anchor,
              bounce_ts: Sequence[float],
              roi_ts: Sequence[float],
              far_pose_ts: Sequence[float],
              near_pose_ts: Sequence[float],
              ball_t: np.ndarray,
              ball_y: np.ndarray,
              roi_rows: Sequence[dict] = (),
              frame_h: float = 1080.0) -> np.ndarray:
    """Build the feature vector for one anchor.

    ball_t / ball_y: parallel arrays of ball detection times + image-y,
    sorted by time. Image-y is used (not court) so the features survive
    calibration failures like ca475740.
    roi_rows: ts-sorted dicts {ts, kp (17x[x,y,conf]), bbox_h} for the far
    player's ROI ViTPose rows — feeds the serve-signature features.
    """
    t = anchor.ts
    merged = anchor.extras.get("merged_sources", set())

    cx, cy = anchor.bounce_court_x, anchor.bounce_court_y
    coords_known = cx is not None and cy is not None
    in_box = (coords_known
              and SERVICE_BOX["y_min"] <= cy <= SERVICE_BOX["y_max"]
              and SERVICE_BOX["x_min"] <= cx <= SERVICE_BOX["x_max"])

    lo, hi = bisect_left(ball_t, t - 2.0), bisect_right(ball_t, t + 2.0)
    n_ball = hi - lo

    def y_slice(a, b):
        i, j = bisect_left(ball_t, a), bisect_right(ball_t, b)
        return ball_y[i:j], ball_t[i:j]

    y_pm1, t_pm1 = y_slice(t - 1.0, t + 1.0)
    y_prev, t_prev = y_slice(t - 1.0, t)
    y_next, t_next = y_slice(t, t + 1.0)

    def slope(ys, ts_):
        if len(ys) < 3:
            return 0.0
        return float(np.polyfit(ts_, ys, 1)[0]) / frame_h  # normalised px/s

    return np.array([
        1.0 if anchor.source == "bounce" else 0.0,
        1.0 if anchor.source == "pose" else 0.0,
        1.0 if len(merged) > 1 else 0.0,

        (cx or 0.0) / 11.0,
        (cy or 0.0) / 23.77,
        1.0 if in_box else 0.0,
        ((cy - HALF_Y) / 11.885) if cy is not None else 0.0,
        1.0 if coords_known else 0.0,

        _gap_before(bounce_ts, t) / 30.0,
        _gap_after(bounce_ts, t) / 30.0,
        _count(bounce_ts, t - 5.0, t - 1e-6) / 10.0,
        _count(bounce_ts, t + 1e-6, t + 5.0) / 10.0,

        _count(far_pose_ts, t - 2.0, t + 2.0) / 100.0,
        _count(roi_ts, t - 2.0, t + 2.0) / 50.0,
        anchor.extras.get("burst_rows", 0) / 50.0,
        _count(far_pose_ts, t - 2.0, t) / 50.0,
        _count(far_pose_ts, t, t + 2.0) / 50.0,

        n_ball / 100.0,
        (float(np.min(y_pm1)) / frame_h) if len(y_pm1) else 1.0,
        slope(y_prev, t_prev),
        slope(y_next, t_next),
        (float(np.sum(y_pm1 < 0.25 * frame_h)) / 25.0) if len(y_pm1) else 0.0,

        _count(near_pose_ts, t - 2.0, t + 2.0) / 100.0,

        *(lambda mr, up, tr: (min(mr, 2.0) / 2.0, up / 50.0, tr / 50.0))(
            *_arm_raise_stats(roi_rows, t)),
    ], dtype=np.float32)


N_FEATURES = len(FEATURE_NAMES)  # 23 — model input width; keep in sync with featurize()
