"""Per-candidate features for the hit model.

Pure functions over pre-sorted per-task arrays — no DB, no video (corpus
source videos are deleted post-trim). IMAGE-SPACE FIRST: the train tasks
are warp-era, the gate eval is clean-coordinate; image features transfer,
court features carry the warp (the serve model proved this split works).

What separates a HIT from the other discontinuity classes:
  - vs BOUNCE: a hit reverses the ball's net-crossing direction (vy sign
    flip) and happens at racquet height (image y well above the ground
    contact line for that court depth); a bounce preserves horizontal
    direction and sits AT the bounce parabola minimum. The CNN bounce
    model already names bounces — proximity to a ball_bounces event is
    the single strongest negative signal.
  - vs NOISE (TrackNet jitter): real hits have coherent speed in AND out;
    jitter has tiny speeds or isolated single-frame spikes.
  - context: hits happen mid-rally near a player; noise doesn't care.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Sequence

import numpy as np

from ml_pipeline.hit_model.candidates import HitCandidate

FEATURE_NAMES = [
    # discontinuity geometry
    "angle_deg", "speed_in", "speed_out", "speed_ratio",
    "vy_flip", "vy_in_sign", "abs_vy_in", "abs_vy_out",
    # perspective-normalised speed: far ball moves few px/frame (compressed
    # at the top of the image), so a global speed gate under-scores real far
    # hits and the model fires on the stronger near-side bounce instead
    # (far emission 22/51, attribution 8/22 — both stem from ranking the
    # wrong discontinuity). Scaling speed by the local px-per-metre proxy
    # (image-y) makes a sharp far reversal look as fast as a near one.
    "speed_in_persp", "speed_out_persp",
    # ball position (image, normalised)
    "img_x", "img_y",
    # bounce disambiguation
    "cnn_bounce_gap_s", "near_cnn_bounce", "legacy_bounce_gap_s",
    # player proximity (image space, both halves)
    "near_player_gap_px", "far_player_gap_px", "nearest_player_gap_px",
    # rally / temporal context
    "cands_prev_2s", "cands_next_2s", "gap_prev_cand_s", "gap_next_cand_s",
    "ball_rows_pm1s",
    # court (NULL-tolerant — warped on train tasks, honest on eval)
    "court_y_norm", "court_known",
]
N_FEATURES = len(FEATURE_NAMES)


def _count(sorted_ts, lo, hi):
    return bisect_right(sorted_ts, hi) - bisect_left(sorted_ts, lo)


def _nearest_gap(sorted_ts, t, cap=10.0):
    if not sorted_ts:
        return cap
    i = bisect_left(sorted_ts, t)
    best = cap
    for j in (i - 1, i):
        if 0 <= j < len(sorted_ts):
            best = min(best, abs(sorted_ts[j] - t))
    return best


def featurize(c: HitCandidate,
              cand_ts: Sequence[float],
              ball_ts: Sequence[float],
              cnn_bounce_ts: Sequence[float],
              legacy_bounce_ts: Sequence[float],
              near_player_xy_by_ts: dict,
              far_player_xy_by_ts: dict,
              frame_w: float = 1920.0,
              frame_h: float = 1080.0) -> np.ndarray:
    """Build one candidate's feature vector.

    near/far_player_xy_by_ts: {rounded_ts: (cx, cy)} image-centre lookups
    built by dataset.load_task_arrays at 0.2s resolution.
    """
    t = c.ts
    cnn_gap = _nearest_gap(cnn_bounce_ts, t)
    legacy_gap = _nearest_gap(legacy_bounce_ts, t)

    # px-per-metre grows toward the bottom of the image (near court). Use
    # image-y as a linear perspective proxy: dividing speed by (y/frame_h)
    # boosts far (small-y) speeds ~4-5x so they rank with near hits. Floor
    # the divisor so a ball near the top can't explode the value.
    persp = max(c.y, 0.12 * frame_h) / frame_h
    speed_in_persp = min(c.speed_in / persp, 200.0) / 200.0
    speed_out_persp = min(c.speed_out / persp, 200.0) / 200.0

    def player_gap(lookup):
        best = 2000.0
        for dt in (-0.2, 0.0, 0.2):
            xy = lookup.get(round((t + dt) * 5) / 5)
            if xy is not None:
                best = min(best, float(np.hypot(xy[0] - c.x, xy[1] - c.y)))
        return best

    ng = player_gap(near_player_xy_by_ts)
    fg = player_gap(far_player_xy_by_ts)

    i = bisect_left(cand_ts, t)
    gap_prev = (t - cand_ts[i - 1]) if i > 0 else 10.0
    gap_next = (cand_ts[i + 1] - t) if i + 1 < len(cand_ts) else 10.0

    return np.array([
        c.angle_deg / 180.0,
        min(c.speed_in, 100.0) / 100.0,
        min(c.speed_out, 100.0) / 100.0,
        min(c.speed_out / max(c.speed_in, 0.1), 10.0) / 10.0,
        1.0 if (c.vy_in > 0) != (c.vy_out > 0) else 0.0,
        1.0 if c.vy_in > 0 else 0.0,
        min(abs(c.vy_in), 50.0) / 50.0,
        min(abs(c.vy_out), 50.0) / 50.0,
        speed_in_persp,
        speed_out_persp,

        c.x / frame_w,
        c.y / frame_h,

        min(cnn_gap, 5.0) / 5.0,
        1.0 if cnn_gap < 0.25 else 0.0,
        min(legacy_gap, 5.0) / 5.0,

        min(ng, 2000.0) / 2000.0,
        min(fg, 2000.0) / 2000.0,
        min(ng, fg, 2000.0) / 2000.0,

        _count(cand_ts, t - 2.0, t - 1e-6) / 20.0,
        _count(cand_ts, t + 1e-6, t + 2.0) / 20.0,
        min(gap_prev, 10.0) / 10.0,
        min(gap_next, 10.0) / 10.0,
        _count(ball_ts, t - 1.0, t + 1.0) / 50.0,

        (c.court_y / 23.77) if c.court_y is not None else 0.5,
        1.0 if c.court_y is not None else 0.0,
    ], dtype=np.float32)
