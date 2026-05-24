"""Per-frame wrist-velocity computation for stroke detection.

Refactor of ml_pipeline/diag/ball_hit_pose.py into a pure-function module.
The probe was the spec; this module is the production implementation.

Algorithm (unchanged from probe):
  1. For each pose row, extract left+right wrist (x, y) if conf ≥ min_conf.
  2. Per player, per side, compute |position(f) - position(prev_f)| / (f - prev_f).
     Only count velocity across pose gaps ≤ max_gap_frames.
  3. Per player per frame: max(left_velocity, right_velocity).
  4. Per frame globally: max across players (robust to player_id swap glitches).
  5. Smooth with rolling mean over smooth_window frames.
  6. Find local maxima above min_velocity with min_gap_frames between peaks.

The detector orchestrator (`detector.py`) applies three post-peak filters
on top of the raw peaks: peak-to-contact offset, deceleration ratio, and
per-player attribution.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple


# COCO keypoint indices (matches ml_pipeline/player_tracker.py)
KP_LEFT_WRIST = 9
KP_RIGHT_WRIST = 10

DEFAULT_MIN_KP_CONF = 0.3
DEFAULT_MAX_GAP_FRAMES = 3
DEFAULT_SMOOTH_WINDOW = 3
DEFAULT_MIN_VELOCITY_PX_PER_FRAME = 30.0
DEFAULT_MIN_GAP_FRAMES = 25      # raised from probe's 15 — see __init__.py
DEFAULT_PEAK_TO_CONTACT_OFFSET = 4  # frames added to predicted_hit_frame
DEFAULT_DECEL_RATIO_MAX = 0.5    # reject peaks where post_v / peak_v > this


def _parse_keypoints(raw) -> Optional[list]:
    """Normalise keypoints to [[x, y, conf], ...] (17 elements). Returns None
    on malformed input. Accepts the DB's JSONB nested form or the flat-51
    form some YOLO outputs emit."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not raw or not isinstance(raw, (list, tuple)):
        return None
    if isinstance(raw[0], (int, float)):
        if len(raw) < 51:
            return None
        return [[float(raw[i * 3]), float(raw[i * 3 + 1]), float(raw[i * 3 + 2])]
                for i in range(17)]
    if len(raw) < 11:
        return None
    return [[float(raw[i][0]), float(raw[i][1]), float(raw[i][2])]
            for i in range(min(17, len(raw)))]


def _wrist_positions(
    keypoints, min_conf: float,
) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    """Return (left_wrist_xy_or_None, right_wrist_xy_or_None)."""
    kp = _parse_keypoints(keypoints)
    if kp is None:
        return None, None
    out: List[Optional[Tuple[float, float]]] = [None, None]
    for slot, idx in [(0, KP_LEFT_WRIST), (1, KP_RIGHT_WRIST)]:
        try:
            x, y, c = kp[idx]
        except (IndexError, TypeError, ValueError):
            continue
        if c is None or float(c) < min_conf:
            continue
        out[slot] = (float(x), float(y))
    return out[0], out[1]


def compute_per_player_velocity(
    poses: Sequence[Tuple[int, int, list]],
    *,
    min_kp_conf: float = DEFAULT_MIN_KP_CONF,
    max_gap_frames: int = DEFAULT_MAX_GAP_FRAMES,
) -> Dict[int, Dict[int, float]]:
    """Return {player_id: {frame: max(left_vel, right_vel)}}.

    `poses` is an iterable of (frame_idx, player_id, keypoints_payload) tuples.
    Velocity is dropped across pose gaps > max_gap_frames — when YOLO loses
    a body for ~10 frames we don't know what the wrist did in between, so
    no velocity sample is emitted at the reappearance frame.
    """
    per_player_rows: Dict[int, List[Tuple[int, list]]] = {}
    for frame, pid, kps in poses:
        per_player_rows.setdefault(pid, []).append((int(frame), kps))

    out: Dict[int, Dict[int, float]] = {}
    for pid, rows in per_player_rows.items():
        rows.sort(key=lambda r: r[0])
        last_left: Optional[Tuple[int, float, float]] = None
        last_right: Optional[Tuple[int, float, float]] = None
        out[pid] = {}
        for frame, kps in rows:
            left, right = _wrist_positions(kps, min_kp_conf)
            v_left = v_right = None
            if left is not None:
                if last_left is not None and frame - last_left[0] <= max_gap_frames:
                    dx = left[0] - last_left[1]
                    dy = left[1] - last_left[2]
                    df = frame - last_left[0]
                    v_left = ((dx * dx + dy * dy) ** 0.5) / max(df, 1)
                last_left = (frame, left[0], left[1])
            if right is not None:
                if last_right is not None and frame - last_right[0] <= max_gap_frames:
                    dx = right[0] - last_right[1]
                    dy = right[1] - last_right[2]
                    df = frame - last_right[0]
                    v_right = ((dx * dx + dy * dy) ** 0.5) / max(df, 1)
                last_right = (frame, right[0], right[1])
            cands = [v for v in (v_left, v_right) if v is not None]
            if cands:
                out[pid][frame] = max(cands)
    return out


def compute_global_max_velocity(
    per_player_vel: Dict[int, Dict[int, float]],
) -> Tuple[Dict[int, float], Dict[int, int]]:
    """Merge across players: returns (vel_by_frame, attribution_by_frame).

    attribution_by_frame[f] = the player_id whose wrist hit max velocity at f
    (used for stroke-event player_id assignment). Robust to player_id swap
    glitches: if tracking briefly labels NEAR as player 1 instead of 0, the
    real swing wrist's velocity still dominates the merged signal.
    """
    vel_out: Dict[int, float] = {}
    attr_out: Dict[int, int] = {}
    for pid, fv in per_player_vel.items():
        for frame, v in fv.items():
            if frame not in vel_out or v > vel_out[frame]:
                vel_out[frame] = v
                attr_out[frame] = pid
    return vel_out, attr_out


def smooth_velocity(
    velocity_by_frame: Dict[int, float], window: int,
) -> List[Tuple[int, float]]:
    """Apply rolling-mean smoothing over the ordered frame sequence.

    Returns [(frame, smoothed_velocity), ...] sorted by frame. Frames with
    no velocity entry are skipped (no interpolation).
    """
    if not velocity_by_frame:
        return []
    frames = sorted(velocity_by_frame.keys())
    smoothed: List[Tuple[int, float]] = []
    for i, f in enumerate(frames):
        lo = max(0, i - window + 1)
        window_vals = [velocity_by_frame[frames[j]] for j in range(lo, i + 1)]
        smoothed.append((f, sum(window_vals) / len(window_vals)))
    return smoothed


def detect_velocity_peaks(
    smoothed: List[Tuple[int, float]],
    *,
    min_velocity: float = DEFAULT_MIN_VELOCITY_PX_PER_FRAME,
    min_gap_frames: int = DEFAULT_MIN_GAP_FRAMES,
) -> List[int]:
    """Find local maxima above min_velocity with min_gap_frames between peaks.

    A frame F is a peak if v(F) > v(F-1) AND v(F) >= v(F+1) — handles flat
    tops by taking the earliest frame of a plateau. Greedy nearest-first:
    we accept peaks in chronological order, suppressing any within
    min_gap_frames of the previously accepted one (the probe over-fired on
    backswing+forward+follow-through within 15 frames; 25-frame gap is
    typical between-stroke time at 25fps).
    """
    if len(smoothed) < 3:
        return []
    peaks: List[int] = []
    last_accepted = -10 ** 9
    for i in range(1, len(smoothed) - 1):
        f, v = smoothed[i]
        if v < min_velocity:
            continue
        _, v_prev = smoothed[i - 1]
        _, v_next = smoothed[i + 1]
        if v > v_prev and v >= v_next:
            if f - last_accepted >= min_gap_frames:
                peaks.append(f)
                last_accepted = f
    return peaks


def velocity_at(smoothed: List[Tuple[int, float]], frame: int) -> Optional[float]:
    """Return smoothed velocity at the exact frame, or None if absent."""
    for f, v in smoothed:
        if f == frame:
            return v
        if f > frame:
            return None
    return None


def post_peak_mean_velocity(
    smoothed: List[Tuple[int, float]], peak_frame: int, lookahead: int = 3,
) -> Optional[float]:
    """Mean smoothed velocity over the `lookahead` frames AFTER peak_frame.

    Returns None when fewer than 1 sample falls in (peak_frame, peak_frame+lookahead].
    Tolerant of pose gaps — uses the smoothed series's actual frames rather
    than assuming every frame is present.
    """
    samples = [v for f, v in smoothed if peak_frame < f <= peak_frame + lookahead]
    if not samples:
        return None
    return sum(samples) / len(samples)


def pre_peak_mean_velocity(
    smoothed: List[Tuple[int, float]], peak_frame: int, lookback: int = 3,
) -> Optional[float]:
    """Mean smoothed velocity over the `lookback` frames BEFORE peak_frame."""
    samples = [v for f, v in smoothed if peak_frame - lookback <= f < peak_frame]
    if not samples:
        return None
    return sum(samples) / len(samples)
