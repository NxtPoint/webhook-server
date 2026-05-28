"""Feature extraction for the bounce CNN.

Builds the (14 channels x 41 frame) windowed feature tensor per the
ADR-01 feature-list table. Inputs come from bronze:
  - ml_analysis.ball_detections (court_x/court_y/x/y/confidence)
  - ml_analysis.player_detections_roi (wrist keypoints in court coords)
  - ml_analysis.serve_events (rally_state per ts)
  - court geometry constants (court_keypoints — projected geometry,
    not a separate table; the constants are baked into this module)

Channel layout (14):
   0: court_x normalised to [0, 1] over COURT_WIDTH_M
   1: court_y normalised to [0, 1] over COURT_LENGTH_M
   2: dx_court (1st diff of court_x, m/frame)
   3: dy_court (1st diff of court_y, m/frame)
   4: ddx_court (2nd diff of court_x)
   5: ddy_court (2nd diff of court_y)
   6: gravity_residual = y_t - parabolic_fit(y_{t-N..t+N} excluding t)
   7: dist_to_baseline (signed, normalised)
   8: dist_to_sideline (signed, normalised)
   9: dist_to_service_line (signed, normalised)
  10: dist_to_net_line (absolute, normalised) — co-located with above_net_flag
  11: min_dist_to_any_wrist (court coords, m)
  12: rally_state_in_rally (0/1)
  13: ball_detection_confidence (raw, [0..1])

This module is the bridge between the raw bronze and the CNN. Each
feature is computed deterministically so v0 plumbing matches the
trained-v1 path exactly — only the CNN weights change.

NOTE: this v0 build does NOT call the feature extractor end-to-end
on prod data — the orchestrator (detector.py) skips it when the model
is in STOPGAP mode, since random-init scores aren't useful. Training
the model in the next session will exercise this code path heavily;
keeping it complete avoids a "build it later" debt that future
sessions would hit when wiring training data.
"""
from __future__ import annotations

import logging
import math
from typing import Optional, Sequence

import numpy as np

from ml_pipeline.bounce_detector.cnn import (
    CENTRE_IDX,
    N_CHANNELS,
    WINDOW_FRAMES,
)

logger = logging.getLogger(__name__)

# Court constants — match pre_gates.py + serve_detector/detector.py.
COURT_LENGTH_M = 23.77
COURT_WIDTH_M = 10.97        # doubles court width
HALF_Y = COURT_LENGTH_M / 2.0
NET_Y = HALF_Y
SERVICE_LINE_FROM_NET_M = 6.40
SINGLES_HALF_WIDTH_M = 4.115
COURT_CENTRE_X = COURT_WIDTH_M / 2.0
SIDELINE_X_LEFT = COURT_CENTRE_X - SINGLES_HALF_WIDTH_M    # 1.370
SIDELINE_X_RIGHT = COURT_CENTRE_X + SINGLES_HALF_WIDTH_M   # 9.600

# Distance normalisers (chosen to keep all channels roughly in [-1, +1]
# during training; the CNN learns the actual per-channel scale via
# BatchNorm but staying close to unit range helps early training stability).
DIST_NORM_M = COURT_LENGTH_M / 2.0   # 11.885

# Gravity-residual fit window — exclude the candidate frame itself so we
# measure how anomalous it is vs the parabolic prior fitted on neighbours.
GRAVITY_FIT_HALFWIDTH = 5            # ±5 frames around candidate, candidate excluded


def _safe_diff(arr: np.ndarray) -> np.ndarray:
    """First difference, padded so output length equals input length."""
    if len(arr) < 2:
        return np.zeros_like(arr)
    d = np.diff(arr, prepend=arr[0])
    return d


def _gravity_residual(y_window: np.ndarray, centre_idx: int,
                      halfwidth: int = GRAVITY_FIT_HALFWIDTH) -> float:
    """Fit a parabola to y_t at indices [centre-halfwidth, centre+halfwidth]
    excluding the centre, then return the residual at centre.

    Bounces are 2nd-order discontinuities in the ball's y-coordinate — a
    ballistic-trajectory model fitted on the neighbourhood will mis-predict
    the bounce frame heavily. That mis-prediction (residual) is the most
    discriminative single feature per ADR-01.
    """
    lo = max(0, centre_idx - halfwidth)
    hi = min(len(y_window), centre_idx + halfwidth + 1)
    xs, ys = [], []
    for i in range(lo, hi):
        if i == centre_idx:
            continue
        v = y_window[i]
        if np.isnan(v):
            continue
        xs.append(i)
        ys.append(float(v))
    if len(xs) < 3:
        return 0.0
    try:
        coeffs = np.polyfit(xs, ys, deg=2)
        predicted = np.polyval(coeffs, centre_idx)
    except (np.linalg.LinAlgError, ValueError):
        return 0.0
    centre_val = y_window[centre_idx]
    if np.isnan(centre_val):
        return 0.0
    return float(centre_val - predicted)


def _norm_position(value: Optional[float], norm: float) -> float:
    """Normalise a scalar by `norm`, mapping None -> 0.0."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    return float(value) / norm


def _min_dist_to_wrist(
    cx: Optional[float], cy: Optional[float],
    wrists: Sequence[tuple[Optional[float], Optional[float]]],
) -> float:
    """Minimum distance (court metres) to any wrist; returns DIST_NORM_M
    (i.e. "far") when no wrists or no candidate coords."""
    if cx is None or cy is None or not wrists:
        return DIST_NORM_M
    best = math.inf
    for wx, wy in wrists:
        if wx is None or wy is None:
            continue
        d = math.sqrt((cx - wx) ** 2 + (cy - wy) ** 2)
        if d < best:
            best = d
    return float(best) if best != math.inf else DIST_NORM_M


def build_window(
    candidate_frame_idx: int,
    ball_rows_by_frame: dict[int, dict],
    wrist_positions_at_centre: Sequence[tuple[Optional[float], Optional[float]]],
    rally_state_at_centre: Optional[str],
) -> np.ndarray:
    """Build the (N_CHANNELS, WINDOW_FRAMES) feature window for one candidate.

    ball_rows_by_frame: dict mapping frame_idx -> ball_detections row dict
        with keys court_x, court_y, x, y, is_bounce, speed_kmh.
        Frames missing from the dict are treated as detection drops (NaN).
    wrist_positions_at_centre: 4 wrists (both wrists of both players) in
        court coords at the centre frame; channels 11 + the wrist gate.
    rally_state_at_centre: rally state at the centre frame (string).

    The window is centred at WINDOW_FRAMES // 2 (= 20). Frames outside the
    detected range are zero-padded for normalised position channels and
    have first/second diffs == 0 at the boundary.
    """
    half = WINDOW_FRAMES // 2
    lo_frame = candidate_frame_idx - half
    hi_frame = candidate_frame_idx + half + 1   # exclusive

    feats = np.zeros((N_CHANNELS, WINDOW_FRAMES), dtype=np.float32)
    cx_seq = np.full(WINDOW_FRAMES, np.nan, dtype=np.float32)
    cy_seq = np.full(WINDOW_FRAMES, np.nan, dtype=np.float32)
    conf_seq = np.zeros(WINDOW_FRAMES, dtype=np.float32)

    for i, fi in enumerate(range(lo_frame, hi_frame)):
        row = ball_rows_by_frame.get(fi)
        if row is None:
            continue
        cx = row.get("court_x")
        cy = row.get("court_y")
        if cx is not None:
            cx_seq[i] = cx
        if cy is not None:
            cy_seq[i] = cy
        # ball_detections.is_in / detection_confidence is not always present —
        # fall back to a unit confidence when the row exists at all.
        conf = row.get("confidence", row.get("detection_confidence"))
        conf_seq[i] = float(conf) if conf is not None else 1.0

    # NaN -> 0 for channel 0/1 (normalised position) so downstream BatchNorm
    # doesn't choke; the gravity residual computation handles NaN explicitly.
    cx_filled = np.nan_to_num(cx_seq, nan=0.0)
    cy_filled = np.nan_to_num(cy_seq, nan=0.0)

    # 0,1: normalised position
    feats[0] = cx_filled / COURT_WIDTH_M
    feats[1] = cy_filled / COURT_LENGTH_M

    # 2,3: 1st diff
    feats[2] = _safe_diff(cx_filled) / COURT_WIDTH_M
    feats[3] = _safe_diff(cy_filled) / COURT_LENGTH_M

    # 4,5: 2nd diff
    feats[4] = _safe_diff(feats[2])
    feats[5] = _safe_diff(feats[3])

    # 6: gravity residual at every frame (each frame as its own candidate
    # within the window — useful for the CNN's per-frame view; the centre
    # frame is the one downstream NMS / threshold cares about).
    for j in range(WINDOW_FRAMES):
        feats[6, j] = _gravity_residual(cy_seq, j)
    feats[6] = feats[6] / DIST_NORM_M

    # 7,8,9: signed distances (use FAR baseline = court_y == 0 as origin;
    # NEAR baseline = COURT_LENGTH_M). dist_to_baseline is min distance to
    # nearer of the two baselines (still signed by which half).
    # 10: abs distance to net line (planar).
    for j in range(WINDOW_FRAMES):
        cx_j = cx_seq[j]
        cy_j = cy_seq[j]
        if np.isnan(cx_j) or np.isnan(cy_j):
            continue
        # Signed: positive when ball is on near half, negative on far.
        dist_near_base = (cy_j - COURT_LENGTH_M)            # negative when in-court
        dist_far_base = cy_j                                # positive when in-court
        signed_baseline = (
            dist_near_base if abs(dist_near_base) < abs(dist_far_base)
            else dist_far_base
        )
        feats[7, j] = signed_baseline / DIST_NORM_M

        # Sideline: signed by which side of the centre line.
        dist_left = cx_j - SIDELINE_X_LEFT
        dist_right = cx_j - SIDELINE_X_RIGHT
        signed_sideline = dist_left if abs(dist_left) < abs(dist_right) else dist_right
        feats[8, j] = signed_sideline / DIST_NORM_M

        # Service line: signed distance to the nearer service line (each side
        # has one ±SERVICE_LINE_FROM_NET_M from the net).
        sl_near = (NET_Y + SERVICE_LINE_FROM_NET_M)
        sl_far = (NET_Y - SERVICE_LINE_FROM_NET_M)
        d_sl_near = cy_j - sl_near
        d_sl_far = cy_j - sl_far
        signed_sl = d_sl_near if abs(d_sl_near) < abs(d_sl_far) else d_sl_far
        feats[9, j] = signed_sl / DIST_NORM_M

        feats[10, j] = abs(cy_j - NET_Y) / DIST_NORM_M

    # 11: min wrist distance — use centre frame's wrists for ALL frames in
    # the window. Wrist positions change slowly; per-frame wrist tracking
    # would 4x the bronze query cost for marginal CNN benefit in v1. Future
    # version can stream per-frame wrist if signal calls for it.
    cx_centre = cx_seq[CENTRE_IDX]
    cy_centre = cy_seq[CENTRE_IDX]
    centre_min_wrist = _min_dist_to_wrist(
        None if np.isnan(cx_centre) else float(cx_centre),
        None if np.isnan(cy_centre) else float(cy_centre),
        wrist_positions_at_centre,
    )
    feats[11, :] = centre_min_wrist / DIST_NORM_M

    # 12: rally_state in_rally flag (0/1, broadcast)
    rs = (rally_state_at_centre or "").lower()
    in_rally = 1.0 if rs in ("in_rally", "serve_in_flight") else 0.0
    feats[12, :] = in_rally

    # 13: ball-detection confidence
    feats[13] = conf_seq

    return feats
