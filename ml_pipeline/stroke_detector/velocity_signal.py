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

# Body-scale velocity normalisation (fixes the near-player attribution bias).
# Wrist velocity is raw pixels, but the far player is ~3x smaller than the near
# player in pixels, so far wrist motion can't cross MIN_VELOCITY and the global-
# max attribution always picked the near player (~208/34 on Match 1 vs SA's
# ~50/50). We scale each player's velocity by (reference_body / player_body),
# with the reference = the LARGEST player so the near player is unchanged
# (factor 1) and the 30px threshold stays valid; only smaller players scale up.
DEFAULT_NORMALIZE_BODY_SCALE = True
DEFAULT_SCALE_MIN_SAMPLES = 10   # need this many valid poses to trust a player's median scale
DEFAULT_SCALE_MAX_FACTOR = 6.0   # cap the boost so a degenerate tiny scale can't blow up velocity

# Swing-path precision gate (cuts near-player false-positive peaks). A real
# groundstroke sweeps the wrist through a large arc; a recovery/split-step/ready
# adjustment has a velocity blip but little total excursion. We measure the
# wrist path length (validated consecutive motion, teleports rejected) over a
# window, normalised to torso-lengths, and reject peaks below the minimum.
# APPLIED ONLY to the reference (largest / near) player, where pose is dense
# enough for path length to be meaningful — the far player's pose is too sparse
# (gaps, dropouts) so its real strokes measure LOW and a global gate would cut
# them. PROVISIONAL: the threshold is calibrated on a single match (Match 1, no
# second video / training corpus yet) — re-validate when more SA truth exists;
# the proper fix is the trained stroke classifier (Q1-D). Set to 0.0 to disable.
DEFAULT_MIN_SWING_PATH_TORSOS = 0.0   # overridden by detector default; 0 = gate off
DEFAULT_SWING_PATH_WINDOW = 10        # +/- frames around the peak
DEFAULT_SWING_STEP_CAP_TORSOS = 0.6   # reject single-step wrist jumps > this (teleport/outlier)


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


def _body_scale(keypoints, min_conf: float) -> Optional[float]:
    """Robust per-pose body size in pixels: shoulder-midpoint → hip-midpoint
    (torso length), falling back to shoulder width. Used to size-normalise
    wrist velocity. Returns None when not enough confident keypoints.

    Torso is preferred over shoulder width alone: it's a longer, more stable
    lever that survives the far player's ~18px shoulder span better.
    """
    kp = _parse_keypoints(keypoints)
    if kp is None:
        return None

    def g(i):
        return kp[i] if i < len(kp) else (0.0, 0.0, 0.0)

    ls, rs, lh, rh = g(5), g(6), g(11), g(12)
    if ls[2] < min_conf or rs[2] < min_conf:
        return None
    sm_x, sm_y = (ls[0] + rs[0]) / 2.0, (ls[1] + rs[1]) / 2.0
    hips = [h for h in (lh, rh) if h[2] >= min_conf]
    if hips:
        hm_x = sum(h[0] for h in hips) / len(hips)
        hm_y = sum(h[1] for h in hips) / len(hips)
        torso = ((sm_x - hm_x) ** 2 + (sm_y - hm_y) ** 2) ** 0.5
        if torso > 2.0:
            return torso
    sw = abs(ls[0] - rs[0])
    return sw if sw > 2.0 else None


def median_body_scales(
    poses: Sequence[Tuple[int, int, list]],
    *,
    min_kp_conf: float = DEFAULT_MIN_KP_CONF,
    min_samples: int = DEFAULT_SCALE_MIN_SAMPLES,
) -> Dict[int, float]:
    """Return {player_id: median body scale px} (torso length). Used both to
    derive velocity scale factors and to normalise swing-path to torso-lengths."""
    scales: Dict[int, List[float]] = {}
    for _frame, pid, kps in poses:
        s = _body_scale(kps, min_kp_conf)
        if s is not None:
            scales.setdefault(pid, []).append(s)
    out: Dict[int, float] = {}
    for pid, vals in scales.items():
        if len(vals) >= min_samples:
            vals.sort()
            out[pid] = vals[len(vals) // 2]
    return out


def swing_path_torsos(
    player_rows: List[Tuple[int, list]],
    frames: List[int],
    center_frame: int,
    body_scale: float,
    *,
    window: int = DEFAULT_SWING_PATH_WINDOW,
    max_gap_frames: int = DEFAULT_MAX_GAP_FRAMES,
    step_cap_torsos: float = DEFAULT_SWING_STEP_CAP_TORSOS,
    min_kp_conf: float = 0.4,
) -> Optional[float]:
    """Wrist path length (in torso-lengths) over +/-window frames around a peak.

    Sums validated consecutive wrist displacement (gap <= max_gap_frames) for
    each wrist, rejecting single steps > step_cap_torsos (teleport / outlier
    keypoints), and returns the larger of the two wrists / body_scale. This is a
    robust "how big was the swing" measure — real strokes sweep a large arc,
    fidgets barely move. Returns None if body_scale is missing.
    """
    if not frames or not body_scale or body_scale <= 0:
        return None
    import bisect as _bisect
    lo = _bisect.bisect_left(frames, center_frame - window)
    hi = _bisect.bisect_right(frames, center_frame + window)
    cap = step_cap_torsos * body_scale
    best = 0.0
    for slot in (0, 1):  # left wrist, right wrist
        last: Optional[Tuple[int, float, float]] = None
        path = 0.0
        for i in range(lo, hi):
            frame, kps = player_rows[i]
            left, right = _wrist_positions(kps, min_kp_conf)
            w = (left, right)[slot]
            if w is None:
                continue
            if last is not None and frame - last[0] <= max_gap_frames:
                d = ((w[0] - last[1]) ** 2 + (w[1] - last[2]) ** 2) ** 0.5
                if d <= cap:
                    path += d
            last = (frame, w[0], w[1])
        best = max(best, path)
    return best / body_scale


def compute_player_scale_factors(
    poses: Sequence[Tuple[int, int, list]],
    *,
    min_kp_conf: float = DEFAULT_MIN_KP_CONF,
    min_samples: int = DEFAULT_SCALE_MIN_SAMPLES,
    max_factor: float = DEFAULT_SCALE_MAX_FACTOR,
) -> Dict[int, float]:
    """Return {player_id: velocity scale factor}.

    factor = reference_body / player_body, where reference = the LARGEST
    player's median body size. The largest player (the near player) gets
    factor 1.0 (unchanged); smaller (far) players get a >1 boost so their
    small-pixel wrist motion becomes comparable. Players with fewer than
    `min_samples` valid scale poses get factor 1.0 (don't trust a tiny sample),
    and the boost is capped at `max_factor` so a degenerate scale can't blow up.
    """
    medians = median_body_scales(poses, min_kp_conf=min_kp_conf, min_samples=min_samples)
    if not medians:
        return {}
    ref = max(medians.values())
    factors: Dict[int, float] = {}
    for pid, m in medians.items():
        factors[pid] = min(max_factor, ref / m) if m > 0 else 1.0
    return factors


def compute_per_player_velocity(
    poses: Sequence[Tuple[int, int, list]],
    *,
    min_kp_conf: float = DEFAULT_MIN_KP_CONF,
    max_gap_frames: int = DEFAULT_MAX_GAP_FRAMES,
    scale_factors: Optional[Dict[int, float]] = None,
) -> Dict[int, Dict[int, float]]:
    """Return {player_id: {frame: max(left_vel, right_vel)}}.

    `poses` is an iterable of (frame_idx, player_id, keypoints_payload) tuples.
    Velocity is dropped across pose gaps > max_gap_frames — when YOLO loses
    a body for ~10 frames we don't know what the wrist did in between, so
    no velocity sample is emitted at the reappearance frame.

    When `scale_factors` is given, each player's velocity is multiplied by its
    factor (see compute_player_scale_factors) so far-player motion is comparable
    to near-player motion. Missing pids default to factor 1.0 (no change).
    """
    per_player_rows: Dict[int, List[Tuple[int, list]]] = {}
    for frame, pid, kps in poses:
        per_player_rows.setdefault(pid, []).append((int(frame), kps))

    out: Dict[int, Dict[int, float]] = {}
    for pid, rows in per_player_rows.items():
        factor = (scale_factors or {}).get(pid, 1.0)
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
                    v_left = ((dx * dx + dy * dy) ** 0.5) / max(df, 1) * factor
                last_left = (frame, left[0], left[1])
            if right is not None:
                if last_right is not None and frame - last_right[0] <= max_gap_frames:
                    dx = right[0] - last_right[1]
                    dy = right[1] - last_right[2]
                    df = frame - last_right[0]
                    v_right = ((dx * dx + dy * dy) ** 0.5) / max(df, 1) * factor
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


def post_peak_velocity_at(
    smoothed: List[Tuple[int, float]], peak_frame: int, offset: int = 3,
) -> Optional[float]:
    """Smoothed velocity at the frame nearest to `peak_frame + offset`.

    Per the pickup-spec deceleration check (`v[i+offset] > peak * threshold`),
    we want a single-frame sample, not a mean. A mean over (peak, peak+offset]
    runs much higher than v[i+offset] alone because frames immediately after
    the peak are still close to peak velocity. Using the mean made the decel
    filter zap 100% of peaks on real video; the spec-correct single-frame
    sample admits realistic swings.

    Tolerant of pose gaps: if the exact peak+offset frame is missing from
    the smoothed series we take the nearest frame within ±1 of the target.
    Returns None if no sample is found in (peak_frame, peak_frame+offset+1].
    """
    target = peak_frame + offset
    best_f: Optional[int] = None
    best_v: Optional[float] = None
    for f, v in smoothed:
        if f <= peak_frame:
            continue
        if f > peak_frame + offset + 1:
            break
        if best_f is None or abs(f - target) < abs(best_f - target):
            best_f = f
            best_v = v
    return best_v


def pre_peak_velocity_at(
    smoothed: List[Tuple[int, float]], peak_frame: int, offset: int = 3,
) -> Optional[float]:
    """Smoothed velocity at the frame nearest to `peak_frame - offset`."""
    target = peak_frame - offset
    best_f: Optional[int] = None
    best_v: Optional[float] = None
    for f, v in smoothed:
        if f >= peak_frame:
            break
        if f < peak_frame - offset - 1:
            continue
        if best_f is None or abs(f - target) < abs(best_f - target):
            best_f = f
            best_v = v
    return best_v
