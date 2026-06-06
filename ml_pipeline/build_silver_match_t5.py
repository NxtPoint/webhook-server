"""
ml_pipeline/build_silver_match_t5.py — Silver builder for T5 singles match data.

Transforms T5 ML pipeline bronze (ml_analysis.ball_detections + player_detections)
into silver.point_detail — the SAME table used by SportAI's build_silver_v2.py.

Architecture:
  - T5 Pass 1: Extract bounces, infer the 18 base fields (player_id, serve,
    swing_type, volley, etc.) from ML detections
  - Passes 3-5: Reuse the SHARED derivation logic from build_silver_v2.py
    (point structure, zones, analytics)

The 'model' column distinguishes T5 rows ('t5') from SportAI rows ('sportai').

Usage:
    from ml_pipeline.build_silver_match_t5 import build_silver_match_t5
    result = build_silver_match_t5(task_id="...", replace=True)
"""

import json
import logging
import math
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


def _kps_to_array(raw) -> Optional["np.ndarray"]:
    """Compact JSON/list keypoints to numpy float32 (17, 3) or None.

    Same helper used by serve_detector / stroke_detector — collapses each
    keypoints row from ~2KB Python list to ~204 bytes numpy array. Applied at
    load time in _build_player_buckets, this drops silver-build peak heap on
    a ~44-min match from ~269MB to ~110MB (the dominant allocator was the
    72k-row player_dets list with nested-list keypoints). _parse_keypoints
    already accepts numpy arrays (.tolist() branch) so downstream is
    untouched."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not raw:
        return None
    if isinstance(raw[0], (int, float)):
        if len(raw) < 51:
            return None
        return np.asarray(raw[:51], dtype=np.float32).reshape(17, 3)
    try:
        arr = np.asarray(raw, dtype=np.float32)
    except (ValueError, TypeError):
        return None
    if arr.ndim != 2 or arr.shape[1] != 3 or arr.shape[0] < 11:
        return None
    if arr.shape[0] > 17:
        return arr[:17]
    if arr.shape[0] < 17:
        pad = np.zeros((17 - arr.shape[0], 3), dtype=np.float32)
        return np.vstack([arr, pad])
    return arr

# ---------------------------------------------------------------------------
# Court geometry (ITF standard — same constants as build_silver_v2.SPORT_CONFIG)
# ---------------------------------------------------------------------------
COURT_LENGTH_M = 23.77
COURT_WIDTH_SINGLES_M = 8.23
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = COURT_LENGTH_M / 2                                    # 11.885m — net
SERVICE_LINE_M = 6.40                                           # net → service line
FAR_SERVICE_LINE_M = COURT_LENGTH_M - SERVICE_LINE_M            # 17.37m
SINGLES_LEFT_X = (COURT_WIDTH_DOUBLES_M - COURT_WIDTH_SINGLES_M) / 2  # 1.37m
SINGLES_RIGHT_X = SINGLES_LEFT_X + COURT_WIDTH_SINGLES_M              # 9.60m
CENTRE_X = COURT_WIDTH_DOUBLES_M / 2                            # 5.485m
# A5 widen — was 0.30m but T5's calibration extrapolation places
# real-baseline hitters at 0.38-1.01m INSIDE court on MATCHI wide-
# angle footage, so the tight gate rejected 8 of 11 missing serves
# in the a015bf3a reconcile (Apr 16). 1.5m matches Pass 1's
# HITTER_NEAR_MAX tolerance in _serve_geometric_check below. Zero
# SportAI impact (their hy sits at ~24.47 or ~0.0, nowhere near the
# 0.3-1.5m band the widening opens). This constant is the one the
# T5 builder actually uses — the matching value in build_silver_v2.py
# is overridden by this via SPORT_CONFIG_SINGLES below.
EPS_BASELINE_M = 1.5

# Thresholds for match analysis
SERVE_GAP_S = 3.0       # seconds gap before a bounce to consider it a serve (was 5.0 — too strict for sparse bounce data)
VOLLEY_NET_DISTANCE_M = 2.0  # hitter within 2m of net = volley. Was 4.0 (mid-court),
# which over-counted volleys 13 vs SA's 6 on Match 1. A volley is physically struck
# close to the net, so 2.0m is the motivated value (not a single-match fit to 6).
# Used by both the is_volley flag and the volley-pose branch in swing inference.
SERVE_BOX_TOLERANCE_M = 1.5  # extra tolerance for service box check (real wide serves bounce in doubles alley)
# Bounce-precision guard (Stage 1, 2026-05-25): a bounce within this court
# distance (m) of a player is treated as a racquet contact / near-player
# detection noise, not a floor landing, and dropped from the bounce-driven
# row set. Stage-1 reconciliation vs SA on Match 1: lifts floor precision
# 33%→40% at held recall. See docs/_investigation/bounce_accuracy.md §6-7.
BOUNCE_PLAYER_PROXIMITY_M = 1.5

# Serve-from-events overlay (RULE #1 — silver INHERITS bronze, 2026-05-27).
# T5 has a real serve model: serve_detector -> ml_analysis.serve_events (the
# pose-first 20/24+23/24 detector). When T5_SERVE_FROM_EVENTS is on, T5 silver
# inherits those serves VERBATIM instead of re-deriving them from the geometric
# gate in build_silver_v2 (the "custom serve_d label" — necessary for SportAI,
# whose own bronze serve flag is unreliable, but a stand-in T5 no longer needs).
# The overlay (a) suppresses Pass-1's geometric serve firing, (b) maps each
# serve_event >= min-conf onto a silver serve row carrying the model's hitter
# position, and (c) demotes any leftover overhead-at-baseline row so the SHARED
# pass-3 can't re-flag a stray serve_d — so T5 serve rows == bronze serve_events,
# no more, no less. SportAI is untouched (it has no serve_events). Gated
# default-OFF, env-flip rollback (no Docker rebuild) — same pattern as
# T5_STROKE_DRIVEN_SILVER. NOTE: T5 silver will faithfully reflect bronze's
# current over-fire (more serves / over-segmented points than SportAI) — the
# honest bronze state, whose lever is TRAINING, not silver.
SERVE_EVENTS_MIN_CONF_DEFAULT = 0.70  # count-aligns to SA on Match 1 (26≈26); env-tunable
SERVE_EVENT_MATCH_TOL_S = 1.5         # a serve_event reuses a bounce row within ±this
_OVERHEAD_SWINGS = ("fh_overhead", "bh_overhead", "overhead", "smash")  # pass-3 serve-gate keys

# build_silver_v2 sport config (used when calling shared passes)
SPORT_CONFIG_SINGLES = {
    "court_length_m": COURT_LENGTH_M,
    "doubles_width_m": COURT_WIDTH_DOUBLES_M,
    "singles_left_x": SINGLES_LEFT_X,
    "singles_right_x": SINGLES_RIGHT_X,
    "singles_width": COURT_WIDTH_SINGLES_M,
    "half_y": HALF_Y,
    "service_line_m": SERVICE_LINE_M,
    "far_service_line_m": FAR_SERVICE_LINE_M,
    "eps_baseline_m": EPS_BASELINE_M,
}

SILVER_SCHEMA = "silver"
TABLE = "point_detail"


# ============================================================
# HELPERS
# ============================================================

def _is_in_service_box(court_x: float, court_y: float) -> bool:
    """Check if a bounce position is within either service box.

    Uses SERVE_BOX_TOLERANCE_M extra slack on each side because:
    - Real wide serves bounce just outside the singles line (in the doubles alley)
    - Court coordinate accuracy isn't perfect (~1m error common)
    """
    if court_x is None or court_y is None:
        return False
    tol = SERVE_BOX_TOLERANCE_M
    in_x = (SINGLES_LEFT_X - tol) <= court_x <= (SINGLES_RIGHT_X + tol)
    # Near service box: between net and near service line (with tolerance)
    near_box = (HALF_Y - tol) < court_y <= (FAR_SERVICE_LINE_M + tol)
    # Far service box: between far service line and net (with tolerance)
    far_box = (SERVICE_LINE_M - tol) <= court_y < (HALF_Y + tol)
    return in_x and (near_box or far_box)


def _serve_geometric_check(
    hitter_court_y: Optional[float],
    bounce_court_x: Optional[float],
    bounce_court_y: Optional[float],
    ball_player_distance: Optional[float] = None,
) -> Tuple[bool, str]:
    """Detect a serve by geometry — returns (is_serve, reason).

    Empirical observations from sportai_4a194ff3_serves.csv:
      - Far baseline server: hit_y in [24.42, 24.47]
      - Near baseline server: hit_y in [-2.58, -1.61]
      - Ball-player distance ALWAYS < 1.10m (avg 0.41m) — strongest universal signal
      - Bounce on the opposite side of the net from the hitter

    NOTE: We do NOT require bounce in the strict service box — T5's court_y
    has a ~5m systematic offset vs SportAI.
    """
    if hitter_court_y is None or bounce_court_x is None or bounce_court_y is None:
        return False, "null_coords"

    # NOTE: We do NOT use ball_player_distance for serve detection.
    # SportAI's ground-truth distance is measured at the HIT moment (~0.4m).
    # Our T5 distance is measured at the BOUNCE moment (~12m across the net),
    # so the SportAI threshold doesn't apply. Param kept for API compatibility.
    _ = ball_player_distance

    HITTER_FAR_MIN = 22.0
    HITTER_FAR_MAX = COURT_LENGTH_M + 6.0   # 29.77
    HITTER_NEAR_MIN = -6.0
    HITTER_NEAR_MAX = 1.5

    hitter_at_far = HITTER_FAR_MIN <= hitter_court_y <= HITTER_FAR_MAX
    hitter_at_near = HITTER_NEAR_MIN <= hitter_court_y <= HITTER_NEAR_MAX

    if not (hitter_at_far or hitter_at_near):
        return False, "fail_hitter_y"

    if not (-1.0 <= bounce_court_x <= COURT_WIDTH_DOUBLES_M + 1.0):
        return False, "fail_bounce_x"

    if hitter_at_far:
        if bounce_court_y < HALF_Y:
            return True, "pass"
        return False, "fail_wrong_side"
    if hitter_at_near:
        if bounce_court_y > HALF_Y:
            return True, "pass"
        return False, "fail_wrong_side"
    return False, "fail_hitter_y"


def _is_serve_geometric(
    hitter_court_y: Optional[float],
    bounce_court_x: Optional[float],
    bounce_court_y: Optional[float],
    ball_player_distance: Optional[float] = None,
) -> bool:
    """Boolean wrapper around _serve_geometric_check (kept for compatibility)."""
    ok, _ = _serve_geometric_check(
        hitter_court_y, bounce_court_x, bounce_court_y, ball_player_distance
    )
    return ok


def _is_overhead_pose(keypoints, is_left_handed: bool) -> bool:
    """Check if pose keypoints show an overhead motion (serve).

    A real overhead requires:
      - Dominant wrist ABOVE both shoulders (smaller pixel y)
      - Dominant wrist ABOVE the nose (above face/head)
      - Sufficient confidence on the relevant keypoints

    COCO keypoint order:
      0=nose, 5=left_shoulder, 6=right_shoulder,
      9=left_wrist, 10=right_wrist
    Each keypoint is (x, y, conf).
    """
    if keypoints is None:
        return False

    # Parse if string (sometimes JSONB comes through as str)
    if isinstance(keypoints, str):
        try:
            keypoints = json.loads(keypoints)
        except (json.JSONDecodeError, TypeError):
            return False

    # Need at least 17 keypoints
    if len(keypoints) < 11:
        return False

    try:
        nose = keypoints[0]
        l_shoulder = keypoints[5]
        r_shoulder = keypoints[6]
        l_wrist = keypoints[9]
        r_wrist = keypoints[10]
    except (IndexError, KeyError, TypeError):
        return False

    MIN_CONF = 0.3
    dominant_wrist = l_wrist if is_left_handed else r_wrist

    # All keypoints we use must have decent confidence
    if dominant_wrist[2] < MIN_CONF or nose[2] < MIN_CONF:
        return False

    # At least one shoulder must be confident
    use_l_shoulder = l_shoulder[2] >= MIN_CONF
    use_r_shoulder = r_shoulder[2] >= MIN_CONF
    if not (use_l_shoulder or use_r_shoulder):
        return False

    if use_l_shoulder and use_r_shoulder:
        avg_shoulder_y = (l_shoulder[1] + r_shoulder[1]) / 2
    elif use_l_shoulder:
        avg_shoulder_y = l_shoulder[1]
    else:
        avg_shoulder_y = r_shoulder[1]

    # Lower pixel y = higher in image (image origin is top-left)
    # Wrist must be above shoulders AND above nose
    wrist_y = dominant_wrist[1]
    return wrist_y < avg_shoulder_y and wrist_y < nose[1]


def _parse_keypoints(keypoints) -> Optional[list]:
    """Normalise keypoints to a list of [x, y, conf] triplets, or return None.

    The DB stores keypoints as a JSONB flat array [x1,y1,c1,x2,y2,c2,...] (51
    floats) OR as a nested list [[x,y,c], ...] (17 entries). Also accepts a
    numpy array (the compact in-memory form the streaming loaders store to fit
    Render's 512MB main API). Missing/malformed input returns None.
    """
    if keypoints is None:
        return None
    # Numpy compact form -> nested list; the existing branches then handle it.
    if hasattr(keypoints, "tolist") and not isinstance(keypoints, (list, tuple, str, bytes)):
        try:
            keypoints = keypoints.tolist()
        except Exception:
            return None
    if isinstance(keypoints, str):
        try:
            keypoints = json.loads(keypoints)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(keypoints, (list, tuple)) or len(keypoints) == 0:
        return None
    # Flat list of 51 floats → reshape to 17 × 3
    if isinstance(keypoints[0], (int, float)):
        if len(keypoints) < 51:
            return None
        return [[keypoints[i * 3], keypoints[i * 3 + 1], keypoints[i * 3 + 2]]
                for i in range(17)]
    # Already nested
    if len(keypoints) < 11:
        return None
    return keypoints


def _infer_swing_type_from_keypoints(
    keypoints: Optional[list],
    center_x: Optional[float],
    is_serve: bool,
    is_left_handed: bool,
    court_y: Optional[float] = None,
) -> str:
    """Infer swing type from COCO pose keypoints.

    Returns one of: 'overhead', 'volley', 'fh', 'bh', 'other'

    Decision hierarchy (each level falls through to next if confidence too low):

    1. Serve (is_serve=True)  → 'overhead'  (serve detection is geometric, trusted)
    2. Overhead/smash pose    → 'overhead'  (arm raised, player near net / mid-court)
    3. Volley pose            → 'volley'    (compact arm + player within VOLLEY_NET_DISTANCE_M)
    4. Forehand/Backhand      → 'fh' / 'bh' (dominant wrist vs opposite-side shoulder)
    5. Fallback               → 'other'

    COCO keypoint indices used:
      0=nose, 5=left_shoulder, 6=right_shoulder,
      7=left_elbow, 8=right_elbow,
      9=left_wrist, 10=right_wrist
    All coordinates are in pixel space (y increases downward).
    """
    MIN_CONF = 0.3

    # --- 1. Serve is already known from geometry ---
    if is_serve:
        return "overhead"

    # --- Parse keypoints ---
    kps = _parse_keypoints(keypoints)
    if kps is None:
        return "other"

    def kp(idx):
        """Return (x, y, conf) for keypoint idx, or (None, None, 0) if missing."""
        if idx >= len(kps):
            return None, None, 0.0
        row = kps[idx]
        if len(row) < 3:
            return None, None, 0.0
        return float(row[0]), float(row[1]), float(row[2])

    l_shoulder_x, l_shoulder_y, l_shoulder_c = kp(5)
    r_shoulder_x, r_shoulder_y, r_shoulder_c = kp(6)
    dom_wrist_x, dom_wrist_y, dom_wrist_c = kp(9 if is_left_handed else 10)
    off_wrist_x, off_wrist_y, off_wrist_c = kp(10 if is_left_handed else 9)
    dom_elbow_x, dom_elbow_y, dom_elbow_c = kp(7 if is_left_handed else 8)

    # Shoulder anchor: prefer the same-side shoulder, fall back to the other
    dom_shoulder_x  = (l_shoulder_x  if is_left_handed else r_shoulder_x)
    dom_shoulder_y  = (l_shoulder_y  if is_left_handed else r_shoulder_y)
    dom_shoulder_c  = (l_shoulder_c  if is_left_handed else r_shoulder_c)
    off_shoulder_x  = (r_shoulder_x  if is_left_handed else l_shoulder_x)
    off_shoulder_y  = (r_shoulder_y  if is_left_handed else l_shoulder_y)
    off_shoulder_c  = (r_shoulder_c  if is_left_handed else l_shoulder_c)

    have_dom_wrist   = dom_wrist_c   >= MIN_CONF and dom_wrist_x   is not None
    have_dom_shoulder = dom_shoulder_c >= MIN_CONF and dom_shoulder_x is not None
    have_off_shoulder = off_shoulder_c >= MIN_CONF and off_shoulder_x is not None
    have_dom_elbow   = dom_elbow_c   >= MIN_CONF and dom_elbow_x   is not None

    # --- 2. Overhead / smash detection ---
    # Arm fully raised: dominant wrist above both shoulders in pixel coords
    # (lower pixel y = higher in frame). Player must NOT be at the baseline
    # (otherwise it would be a serve, already handled above).
    if have_dom_wrist and (have_dom_shoulder or have_off_shoulder):
        # Use the average shoulder y when both are visible
        shoulder_ys = []
        if have_dom_shoulder:
            shoulder_ys.append(dom_shoulder_y)
        if have_off_shoulder:
            shoulder_ys.append(off_shoulder_y)
        avg_shoulder_y = sum(shoulder_ys) / len(shoulder_ys)
        # Wrist must be well above the shoulder line (at least 20% of inter-
        # shoulder width as a tolerance — avoids firing on neutral stance)
        shoulder_width_px = abs((dom_shoulder_x or 0) - (off_shoulder_x or dom_shoulder_x or 1))
        overhead_margin = max(10.0, shoulder_width_px * 0.2)
        if dom_wrist_y < avg_shoulder_y - overhead_margin:
            # Distinguish overhead from serve: serve hitter is past baseline
            # (handled by is_serve=True above). Any overhead here is mid-court.
            return "overhead"

    # --- 3. Volley detection ---
    # Player is close to the net AND arm is compact (wrist near the shoulder,
    # not extended for a full groundstroke swing).
    near_net = (
        court_y is not None
        and abs(court_y - HALF_Y) < VOLLEY_NET_DISTANCE_M
    )
    if near_net and have_dom_wrist and have_dom_shoulder:
        # Compact arm: wrist is close to shoulder height (within 30% of
        # shoulder-width tolerance). A real volley punch has the wrist roughly
        # in front of the shoulder, not dropped or raised like a groundstroke.
        wrist_height_diff = abs(dom_wrist_y - dom_shoulder_y)
        shoulder_width_px = max(
            20.0,
            abs((dom_shoulder_x or 0) - (off_shoulder_x or dom_shoulder_x or 1)),
        )
        if wrist_height_diff < shoulder_width_px * 0.8:
            return "volley"

    # --- 4. Forehand / Backhand ---
    # Forehand = dominant wrist extended to the player's dominant side;
    # backhand = the wrist crosses to the off side. Whether the dominant side
    # maps to IMAGE-left or IMAGE-right depends on which way the player faces:
    #   - NEAR player (court_y > HALF_Y) faces AWAY from the camera → their
    #     right side is image-right.
    #   - FAR player (court_y < HALF_Y) faces TOWARD the camera → mirrored, so
    #     their right side is image-LEFT.
    # So the dominant hand sits on image-right iff (right-handed) XOR (far).
    # Without this far-mirror the far player's forehands were misread as
    # backhands (Match 1 far fh 9 / bh 13 vs SA 18 / 6, 2026-05-25). The
    # position fallback `_infer_swing_type_from_position` already mirrors the
    # same way. court_y is None → assume near (preserves prior behaviour, so
    # near-player classification is byte-identical to before this change).
    if have_dom_wrist:
        is_far = court_y is not None and court_y < HALF_Y
        dom_on_right = (not is_left_handed) != is_far  # XOR of handedness & facing

        # Strong signal: dominant wrist vs the OFF-side shoulder.
        if have_dom_shoulder and have_off_shoulder:
            # Backhand = dominant wrist has crossed PAST the off-side shoulder.
            crossed_body = (dom_wrist_x < off_shoulder_x) if dom_on_right \
                else (dom_wrist_x > off_shoulder_x)
            return "bh" if crossed_body else "fh"

        # Medium signal: dominant wrist vs the dominant shoulder.
        if have_dom_shoulder:
            on_dom_side = (dom_wrist_x > dom_shoulder_x) if dom_on_right \
                else (dom_wrist_x < dom_shoulder_x)
            return "fh" if on_dom_side else "bh"

        # Weak signal: dominant wrist vs body centre_x.
        if center_x is not None:
            on_dom_side = (dom_wrist_x > center_x) if dom_on_right \
                else (dom_wrist_x < center_x)
            return "fh" if on_dom_side else "bh"

    return "other"


def _infer_swing_type_from_position(
    ball_x: Optional[float],
    player_x: Optional[float],
    player_y: Optional[float],
    is_serve: bool,
    is_left_handed: bool,
) -> str:
    """Fallback swing type inference from ball/player positions."""
    if is_serve:
        return "overhead"
    if ball_x is None or player_x is None:
        return "other"

    # Player on near half (y > HALF_Y): ball to their right = forehand for right-hander
    # Player on far half (y < HALF_Y): ball to their left = forehand for right-hander
    near_half = player_y is not None and player_y > HALF_Y

    if is_left_handed:
        if near_half:
            return "fh" if ball_x < player_x else "bh"
        else:
            return "fh" if ball_x > player_x else "bh"
    else:
        if near_half:
            return "fh" if ball_x > player_x else "bh"
        else:
            return "fh" if ball_x < player_x else "bh"


# ============================================================
# T5 PASS 1: shared player-detection index
# ============================================================

def _build_player_buckets(conn: Connection, job_id: str) -> dict:
    """Fetch player detections and build the side/keypoint indices used by
    both Pass-1 row-generation strategies (bounce-driven and stroke-driven).

    Returns a dict with binary-search frame lists + parallel detection lists:
      near_frames/near_dets   — players with court_y > HALF_Y (valid coords)
      far_frames/far_dets     — players with court_y < HALF_Y (valid coords)
      any_frames/any_dets     — ALL players with valid coords (mirror fallback)
      near_kp_frames/..._dets — near players that also carry pose keypoints
      far_kp_frames/..._dets  — far players that also carry pose keypoints
      dets_by_pid             — mapped_pid -> (frames, dets) for that track
      pid_map, top_pids       — ghost-id → top-2 mapping (guarantees 2 players)
    """
    # Stream with a server-side cursor on a SEPARATE connection + compact
    # keypoints to numpy float32 (17,3) at load time. Without this, a 44-min
    # match's 72k player_detections rows allocate ~180MB just for nested-list
    # keypoints — the dominant driver of the 512MB-cap OOMs that stalled
    # corpus #3 (9378f2dd, 2026-05-28). Streaming on a separate connection
    # (not the caller's transaction) because `stream_results=True` flips the
    # DBAPI to a named cursor, which can't host the downstream INSERT
    # executemany on the same conn. Bronze isn't mutating during silver
    # build, so a fresh-snapshot read is safe.
    with conn.engine.connect() as _sc:
        _sc = _sc.execution_options(stream_results=True, yield_per=5000)
        player_dets = [
            (r[0], r[1], r[2], r[3], r[4], r[5], _kps_to_array(r[6]), r[7])
            for r in _sc.execute(sql_text("""
                SELECT frame_idx, player_id, court_x, court_y, center_x, center_y, keypoints, stroke_class
                FROM ml_analysis.player_detections
                WHERE job_id = :jid
                ORDER BY frame_idx
            """), {"jid": job_id})
        ]

    # ---- Merge far ViTPose pose from ml_analysis.player_detections_roi ----
    # extract_far_pose writes high-quality far-player keypoints (source=
    # 'far_vitpose', player_id=1) that the full-frame YOLO in the main table
    # misses on 30-40px far bodies. Same merge serve_detector already does:
    # ROI wins wholesale for the far player (pid=1) where both exist; ROI-only
    # frames are added. This lifts far keypoint coverage so far fh/bh swing
    # inference can actually run instead of falling to the position fallback.
    # Coordinate space note: the keypoints are full-frame-relative, and swing
    # inference is intra-frame (wrist vs shoulder), so scale doesn't matter
    # here. Existence is checked via information_schema BEFORE selecting so a
    # missing table can't poison the txn (memory feedback_postgres_missing_table).
    # No-op on SportAI tasks (they have no ROI rows) and on tasks predating the
    # ROI extractor.
    roi_present = conn.execute(sql_text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'ml_analysis' AND table_name = 'player_detections_roi'
        LIMIT 1
    """)).scalar()
    if roi_present:
        # Same streaming + numpy-compaction pattern (separate conn — see above).
        with conn.engine.connect() as _sc:
            _sc = _sc.execution_options(stream_results=True, yield_per=5000)
            roi_rows = [
                (r[0], r[1], r[2], r[3], r[4], r[5], _kps_to_array(r[6]), None)
                for r in _sc.execute(sql_text("""
                    SELECT frame_idx, player_id, court_x, court_y, center_x, center_y, keypoints
                    FROM ml_analysis.player_detections_roi
                    WHERE job_id = :jid AND keypoints IS NOT NULL
                    ORDER BY frame_idx
                """), {"jid": job_id})
            ]
        if roi_rows:
            merged = {(r[1], r[0]): r for r in player_dets}  # (player_id, frame_idx) -> row
            roi_won = roi_added = 0
            for r in roi_rows:
                key = (r[1], r[0])
                if r[1] == 1 or key not in merged:   # ROI wins wholesale for far (pid=1)
                    roi_won += (key in merged)
                    roi_added += (key not in merged)
                    merged[key] = r
            player_dets = sorted(merged.values(), key=lambda r: r[0])
            logger.info(
                "T5 Pass 1: merged %d far ViTPose ROI rows (override=%d add=%d) "
                "from player_detections_roi", len(roi_rows), roi_won, roi_added,
            )

    # Identify the two players — group by player_id, take top 2 by det count.
    from collections import Counter
    pid_counts = Counter(p[1] for p in player_dets)
    top_pids = [pid for pid, _ in pid_counts.most_common(2)]
    if len(top_pids) < 2:
        logger.warning("T5 Pass 1: only %d player(s) detected — assigning alternating IDs", len(top_pids))
        if not top_pids:
            top_pids = [0, 1]
        elif len(top_pids) == 1:
            top_pids.append(top_pids[0] + 1)

    pid_map = {}
    for pid in pid_counts:
        if pid == top_pids[0]:
            pid_map[pid] = str(top_pids[0])
        else:
            pid_map[pid] = str(top_pids[1])

    near_dets, far_dets, any_with_coords = [], [], []
    # Keypoint-only indices (A4): detections that actually carry pose. The
    # primary hitter lookup uses a tight frame window to keep coords fresh; a
    # secondary lookup on these lists uses a wider window so swing_type
    # inference has pose data even when the nearest detection in the tight
    # window is pose-less (SAHI bbox without pose, or PLAYER_DETECTION_INTERVAL
    # straddling the hit frame).
    near_kp_dets, far_kp_dets = [], []
    # Swing-classifier (bronze stroke_class) carriers, by side. Mirrors the
    # keypoint lists: lets the hitter block adopt a nearby model-classified
    # swing_type when the exact resolved-hitter detection lacks one (the
    # inference frame and silver's hitter frame can differ by a few frames at
    # PLAYER_DETECTION_INTERVAL granularity / fps rounding).
    near_sc_dets, far_sc_dets = [], []
    # Per-track index — the stroke-driven path resolves the attributed player's
    # own position when no bounce is available to imply the hitter's side.
    raw_by_pid: dict = {}
    for pd in player_dets:
        frame_idx, pid, cx, cy, centerx, centery, kps, stroke_cls = pd
        mapped_pid = pid_map.get(pid, str(pid))
        entry = {
            "frame_idx": frame_idx,
            "player_id": mapped_pid,
            "court_x": cx, "court_y": cy,
            "center_x": centerx, "center_y": centery,
            "keypoints": kps,
            "stroke_class": stroke_cls,
        }
        if cy is not None and cx is not None:
            any_with_coords.append(entry)
            raw_by_pid.setdefault(mapped_pid, []).append(entry)
            if cy > HALF_Y:
                near_dets.append(entry)
                if kps is not None:
                    near_kp_dets.append(entry)
                if stroke_cls is not None:
                    near_sc_dets.append(entry)
            else:
                far_dets.append(entry)
                if kps is not None:
                    far_kp_dets.append(entry)
                if stroke_cls is not None:
                    far_sc_dets.append(entry)
        # Entries with NULL court coords aren't useful for hit_x/y, so they're
        # excluded from every list; the mirror fallback uses any_with_coords.

    near_frames, near_dets = _build_detection_index(near_dets)
    far_frames, far_dets = _build_detection_index(far_dets)
    any_frames, any_dets = _build_detection_index(any_with_coords)
    near_kp_frames, near_kp_dets = _build_detection_index(near_kp_dets)
    far_kp_frames, far_kp_dets = _build_detection_index(far_kp_dets)
    near_sc_frames, near_sc_dets = _build_detection_index(near_sc_dets)
    far_sc_frames, far_sc_dets = _build_detection_index(far_sc_dets)
    dets_by_pid = {pid: _build_detection_index(dets) for pid, dets in raw_by_pid.items()}

    logger.info(
        "T5 Pass 1: player buckets — near=%d far=%d any_with_coords=%d "
        "near_with_kp=%d far_with_kp=%d (of %d total)",
        len(near_dets), len(far_dets), len(any_dets),
        len(near_kp_dets), len(far_kp_dets), len(player_dets),
    )

    return {
        "near_frames": near_frames, "near_dets": near_dets,
        "far_frames": far_frames, "far_dets": far_dets,
        "any_frames": any_frames, "any_dets": any_dets,
        "near_kp_frames": near_kp_frames, "near_kp_dets": near_kp_dets,
        "far_kp_frames": far_kp_frames, "far_kp_dets": far_kp_dets,
        "near_sc_frames": near_sc_frames, "near_sc_dets": near_sc_dets,
        "far_sc_frames": far_sc_frames, "far_sc_dets": far_sc_dets,
        "dets_by_pid": dets_by_pid,
        "pid_map": pid_map, "top_pids": top_pids,
        "n_player_dets": len(player_dets),
    }


def _lookup_dominant_hand(conn: Connection, task_id: str) -> bool:
    """Return True if the primary member is left-handed (defaults right)."""
    hand_row = conn.execute(sql_text("""
        SELECT COALESCE(m.dominant_hand, 'right') AS hand
        FROM bronze.submission_context sc
        LEFT JOIN billing.member m
            ON lower(m.email) = lower(sc.email) AND m.is_primary = true
        WHERE sc.task_id = :tid
        LIMIT 1
    """), {"tid": task_id}).fetchone()
    return (hand_row[0] if hand_row else "right") == "left"


# ============================================================
# T5 PASS 1 (bounce-driven): one bounce → one silver row
# ============================================================

def _t5_pass1_load_bounce_driven(conn: Connection, task_id: str, job_id: str, fps: float) -> int:
    """
    Transform T5 ml_analysis.* bounce data into silver.point_detail base fields.

    For each bounce detection:
      1. Determine which player hit the ball (ball direction: hitter is on opposite
         side of net from where the ball bounced)
      2. Find the nearest player detection on the hitting side
      3. Infer serve, swing_type, volley from context
      4. INSERT the 18 base fields + model='t5'

    This is the original (pre-Phase-6) strategy, retained as the fallback for
    tasks with no rows in ml_analysis.stroke_events (older T5 ingests, or runs
    where stroke detection failed). The dispatcher _t5_pass1_load prefers the
    stroke-driven strategy when stroke events exist. See
    _t5_pass1_load_stroke_driven.
    """
    # ---- Step 1: Fetch all bounces ordered by time ----
    bounces = conn.execute(sql_text("""
        SELECT frame_idx, x, y, court_x, court_y, speed_kmh, is_in
        FROM ml_analysis.ball_detections
        WHERE job_id = :jid AND is_bounce = TRUE
          AND court_x IS NOT NULL AND court_y IS NOT NULL
        ORDER BY frame_idx
    """), {"jid": job_id}).fetchall()

    if not bounces:
        # Fallback: try without court_x/court_y filter
        bounces = conn.execute(sql_text("""
            SELECT frame_idx, x, y, court_x, court_y, speed_kmh, is_in
            FROM ml_analysis.ball_detections
            WHERE job_id = :jid AND is_bounce = TRUE
            ORDER BY frame_idx
        """), {"jid": job_id}).fetchall()
        if not bounces:
            logger.warning("T5 Pass 1: no bounces found for job_id=%s", job_id)
            return 0

    logger.info("T5 Pass 1: %d bounces for job_id=%s at %.1f fps", len(bounces), job_id, fps)

    # ---- Step 2-3: Shared player-detection buckets + two-player mapping ----
    buckets = _build_player_buckets(conn, job_id)
    near_frames, near_dets = buckets["near_frames"], buckets["near_dets"]
    far_frames, far_dets = buckets["far_frames"], buckets["far_dets"]
    any_frames, any_dets = buckets["any_frames"], buckets["any_dets"]
    near_kp_frames, near_kp_dets = buckets["near_kp_frames"], buckets["near_kp_dets"]
    far_kp_frames, far_kp_dets = buckets["far_kp_frames"], buckets["far_kp_dets"]
    near_sc_frames, near_sc_dets = buckets["near_sc_frames"], buckets["near_sc_dets"]
    far_sc_frames, far_sc_dets = buckets["far_sc_frames"], buckets["far_sc_dets"]
    pid_map, top_pids = buckets["pid_map"], buckets["top_pids"]

    # ---- Step 4: Look up dominant hand ----
    is_left_handed = _lookup_dominant_hand(conn, task_id)

    # When inheriting serves from serve_events, suppress the geometric serve
    # firing here so serves come SOLELY from the detector's bronze output
    # (the overlay runs after the loop, before insert).
    suppress_geometric_serves = _serve_from_events_enabled()

    # ---- Step 4b: Bounce-precision proximity guard ----
    # Drop bounces co-located with a player — those are racquet contacts /
    # near-player detection noise, not floor landings. Keeps bounces with no
    # nearby player detection (can't gate without evidence) and bounces with
    # NULL court coords (handled/skipped downstream). Stage-1 reconciliation
    # vs SA on Match 1: ~25 of 71 SA-unmatched bounces dropped, only ~4 of 41
    # real floor bounces lost (recall held). docs/_investigation/bounce_accuracy.md.
    prox_win = max(1, int(round(fps * 0.16)))  # ±~4 frames @25fps (matches the Stage-1 prototype)
    _n_before = len(bounces)
    _filtered = []
    for _b in bounces:
        _bf, _, _, _bcx, _bcy = _b[0], _b[1], _b[2], _b[3], _b[4]
        if _bcx is not None and _bcy is not None:
            _d = _min_player_distance_m(any_frames, any_dets, _bf, _bcx, _bcy, prox_win)
            if _d is not None and _d < BOUNCE_PLAYER_PROXIMITY_M:
                continue  # within proximity threshold of a player → drop
        _filtered.append(_b)
    if _n_before:
        logger.info(
            "T5 Pass 1 bounce-proximity guard: kept %d/%d bounces "
            "(dropped %d within %.1fm of a player)",
            len(_filtered), _n_before, _n_before - len(_filtered),
            BOUNCE_PLAYER_PROXIMITY_M,
        )
    bounces = _filtered

    # ---- Step 5: Process each bounce → build row ----
    rows_to_insert = []
    prev_ts = -999.0
    last_serve_ts = -999.0  # for serve cooldown
    MIN_SERVE_INTERVAL_S = 8.0  # minimum seconds between consecutive serves

    # Ball flight from hit → bounce on the opposite side is ~0.3s for a
    # typical rally shot (and as low as ~0.15s for a hard serve). We
    # snap the hitter-lookup target backward from the bounce frame by
    # this offset so the "nearest detection" points at where the hitter
    # actually WAS at the moment they struck the ball, not where they
    # ended up after follow-through. The ±window gates out stale
    # detections (e.g. far-side player seen 30 frames ago) — without
    # it, sparse far-side coverage would share one stale position
    # across many bounces, so serve_side_d never alternated.
    HIT_BEFORE_BOUNCE_FRAMES = max(1, int(round(fps * 0.32)))
    HIT_WINDOW_FRAMES = max(1, int(round(fps * 0.20)))
    # A5+ addendum — the first candidate serve in a match must be at
    # least this many seconds into the video. Observed on a015bf3a /
    # 081e089c: two false-positive serves at ts=0.3 and ts=8.5 during
    # warmup. Stationarity gate couldn't catch ts=0.3 because the
    # video has no prior frames to sample. SportAI's FIRST real serve
    # on the reference match is at ts=54.48. 15s is a conservative
    # floor that kills warmup while not clipping any legitimate early
    # serve — coach-camera MATCHI footage always has at least 15s of
    # setup/warmup before the first point.
    FIRST_SERVE_MIN_TS_S = 15.0
    # Soft-fallback window for hitter resolution (#2 post-validation). The
    # tight HIT_WINDOW_FRAMES was returning None on 38% of bounces in the
    # a015bf3a reconcile — those rows lost hit_x/y → null serve_side_d →
    # excluded from point numbering, so points only reached 3 instead of
    # 15+. We now first try the tight window (precision for serves), and
    # on miss retry with a wider window that tags the hitter as "stale"
    # so downstream callers can weight it differently if needed. The
    # wider window still bounds staleness — we never silently reuse a
    # detection from >1.2s before the hit.
    HIT_SOFT_WINDOW_FRAMES = max(1, int(round(fps * 1.20)))
    # Separate, wider window for swing_type pose lookup (A4). Player detection
    # runs every PLAYER_DETECTION_INTERVAL=5 frames and SAHI bboxes come back
    # without pose, so hit-frame pose coverage is sparse. A stroke doesn't
    # change within a second, so a pose from ±1.2s of the hit still describes
    # the same stroke type — just not necessarily the exact contact instant.
    # Coords still come from the tight HIT_WINDOW (A1); only keypoints widen.
    KP_WINDOW_FRAMES = max(1, int(round(fps * 1.20)))

    # OBSERVABILITY (2b, 2026-06-04): how the hitter was resolved, split by side.
    # Quantifies the far ball_hit_location gap — far hits frequently fall to a
    # STALE (±1.2s) or MIRRORED position because the far court_y is NULL ~50% on
    # wide-camera matches (map_to_court ±5m bound rejects the far-baseline
    # overshoot). This is measurement only — NO behaviour change. The accuracy
    # fix is the documented far-court calibration task, NOT a silver tweak (silver
    # has no accurate far court_y to read). See next_session_pickup 2b + north_star.
    hit_resolve_diag = {
        "near_fresh": 0, "near_stale": 0, "near_mirror": 0, "near_none": 0,
        "far_fresh": 0,  "far_stale": 0,  "far_mirror": 0,  "far_none": 0,
    }

    # Diagnostic counters — why serves are accepted/rejected
    serve_diag = {
        "total_bounces": 0,
        "no_hitter": 0,
        "no_hitter_stale_only": 0,  # detections exist but none within tight window
        "hitter_soft_fallback": 0,  # tight window missed, soft fallback resolved it
        "no_hitter_even_soft": 0,   # soft fallback also missed — genuinely no data
        "geometric_pass": 0,
        "geometric_fail_no_dist": 0,   # ball_player_distance > 1.5
        "geometric_fail_hitter_y": 0,  # hitter not past baseline
        "geometric_fail_bounce_x": 0,  # bounce_x out of range
        "geometric_fail_wrong_side": 0, # bounce on same side as hitter
        "pose_pass": 0,
        "pose_no_keypoints": 0,
        "kp_patched_widened": 0,  # A4: hitter had coord but no pose; wider window supplied pose
        "kp_still_missing": 0,    # A4: even widened window had no pose on this side
        "stationary_ok": 0,       # A5+: hitter stationary 1-2s pre-hit
        "stationary_fail": 0,     # A5+: hitter moved > 0.5m in 1-2s before hit (warmup/ball-roll)
        "stationary_nodata": 0,   # A5+: no prior detections; benefit of doubt granted
        "stationarity_block": 0,  # A5+: geom+cooldown passed but stationarity rejected
        "first_serve_too_early": 0,  # A5+: first candidate serve < FIRST_SERVE_MIN_TS_S
        "cooldown_block": 0,
        "fired_primary": 0,
        "fired_secondary": 0,
    }

    for i, b in enumerate(bounces):
        frame_idx, px, py, cx, cy, speed_kmh, is_in = b

        ts = frame_idx / fps
        gap_s = ts - prev_ts

        if cx is None or cy is None:
            # No court coords — skip (we can't do spatial analysis)
            prev_ts = ts
            continue

        # Determine hitter: ball bounced on one side → hitter was on the OTHER side
        # of the net (in tennis the ball crosses the net after being hit).
        # near_dets contains players with court_y > HALF_Y
        # far_dets  contains players with court_y < HALF_Y
        bounce_on_top = cy < HALF_Y  # cy=0 is one baseline, cy=23.77 is the other
        # When bounce is on top half, hitter was on bottom (near_dets)
        h_frames = near_frames if bounce_on_top else far_frames
        h_dets = near_dets if bounce_on_top else far_dets

        # Search target = estimated hit frame (bounce minus flight time).
        # The tight ±window rejects stale detections; sparse-side coverage
        # yields hitter=None so we don't silently reuse an old position.
        hit_frame_est = max(0, frame_idx - HIT_BEFORE_BOUNCE_FRAMES)
        hitter = _find_nearest_detection(
            h_frames, h_dets, hit_frame_est,
            max_distance_frames=HIT_WINDOW_FRAMES,
        )

        # Soft fallback (#2): if the tight window missed but detections
        # exist on this side, widen to HIT_SOFT_WINDOW_FRAMES and tag the
        # hitter so downstream can tell precision from approximation. The
        # tight window is what protects serve detection (serves need exact
        # coords for serve_side_d alternation). For rally shots, an older
        # position is still usable to keep point structure intact. Without
        # this, reconcile showed 38% of rows with null hit_x/y → null
        # point_number → artificially low points count.
        if hitter is None and h_dets:
            serve_diag["no_hitter_stale_only"] += 1
            hitter = _find_nearest_detection(
                h_frames, h_dets, hit_frame_est,
                max_distance_frames=HIT_SOFT_WINDOW_FRAMES,
            )
            if hitter is not None:
                hitter = dict(hitter)  # copy so we don't mutate the index
                hitter["_hitter_stale"] = True
                serve_diag["hitter_soft_fallback"] += 1
            else:
                serve_diag["no_hitter_even_soft"] += 1

        # Fallback: if no player on the hitter's side, use ANY player with
        # valid coords and mirror them to the hitter's side. This handles the
        # case where ML only tracks one player (always on one side).
        if hitter is None and any_dets:
            other = _find_nearest_detection(
                any_frames, any_dets, hit_frame_est,
                max_distance_frames=HIT_WINDOW_FRAMES,
            )
            if other is not None:
                # Determine which side the "other" player is on
                other_on_top = (other["court_y"] is not None and other["court_y"] < HALF_Y)
                # If they're on the wrong side relative to where the hitter
                # should be, mirror to the correct side
                hitter_should_be_on_top = not bounce_on_top
                if other_on_top != hitter_should_be_on_top:
                    mirror_y = COURT_LENGTH_M - other["court_y"]
                else:
                    mirror_y = other["court_y"]
                # Clamp to court bounds [0, COURT_LENGTH_M]. If a player is
                # past their baseline (e.g. y=-3.94), mirroring gives 27.71
                # which is past the FAR baseline. Clamping to 23.77 puts the
                # synthetic hitter AT the baseline, where serves originate.
                if mirror_y is not None:
                    mirror_y = max(0.0, min(COURT_LENGTH_M, mirror_y))
                hitter = {
                    "frame_idx": other["frame_idx"],
                    "player_id": other["player_id"],
                    "court_x": other["court_x"],
                    "court_y": mirror_y,
                    "center_x": other.get("center_x"),
                    "center_y": other.get("center_y"),
                    "keypoints": other.get("keypoints"),
                    "_synthesized": True,
                }

        # A4 — dual-window keypoint patch. If we have a hitter with fresh
        # coords but no pose (common because SAHI returns bboxes without
        # keypoints and PLAYER_DETECTION_INTERVAL=5 means most hit frames
        # aren't pose-run frames), look in a WIDER window for the nearest
        # same-side detection that DOES have keypoints and adopt those. A
        # stroke's classification doesn't flip within ±1.2s so a slightly-
        # older pose still describes the same stroke type.
        #
        # Skip when the hitter was synthesized via the mirror fallback —
        # those coords came from the OPPOSITE half's player, so any pose
        # found on the hitter's "expected" side belongs to a different
        # player entirely.
        if (hitter is not None
                and hitter.get("keypoints") is None
                and not hitter.get("_synthesized")):
            kp_frames = near_kp_frames if bounce_on_top else far_kp_frames
            kp_dets = near_kp_dets if bounce_on_top else far_kp_dets
            kp_match = _find_nearest_detection(
                kp_frames, kp_dets, hit_frame_est,
                max_distance_frames=KP_WINDOW_FRAMES,
            )
            if kp_match is not None:
                hitter["keypoints"] = kp_match.get("keypoints")
                hitter["_kp_source_frame"] = kp_match["frame_idx"]
                serve_diag["kp_patched_widened"] += 1
            else:
                serve_diag["kp_still_missing"] += 1

        # stroke_class windowed patch — the swing classifier (bronze) labels the
        # detection nearest the contact frame; silver's resolved hitter may be a
        # neighbouring detection (sparse pose / fps rounding), so adopt the
        # nearest same-side model classification within KP_WINDOW. Same guard as
        # the keypoint patch: skip synthesised hitters (their coords came from
        # the opposite half, so the expected-side classification isn't theirs).
        if (hitter is not None
                and hitter.get("stroke_class") is None
                and not hitter.get("_synthesized")):
            sc_frames = near_sc_frames if bounce_on_top else far_sc_frames
            sc_dets = near_sc_dets if bounce_on_top else far_sc_dets
            sc_match = _find_nearest_detection(
                sc_frames, sc_dets, hit_frame_est,
                max_distance_frames=KP_WINDOW_FRAMES,
            )
            if sc_match is not None:
                hitter = dict(hitter)  # copy — don't mutate the shared index entry
                hitter["stroke_class"] = sc_match.get("stroke_class")

        # OBSERVABILITY (2b) — record how this hit's location was resolved, by
        # side. hitter side = opposite of the bounce side. No behaviour change.
        _side = "near" if bounce_on_top else "far"
        if hitter is None:
            hit_resolve_diag[f"{_side}_none"] += 1
        elif hitter.get("_synthesized"):
            hit_resolve_diag[f"{_side}_mirror"] += 1
        elif hitter.get("_hitter_stale"):
            hit_resolve_diag[f"{_side}_stale"] += 1
        else:
            hit_resolve_diag[f"{_side}_fresh"] += 1

        # Compute ball-player distance FIRST so we can use it for serve detection
        # (it's the strongest single signal — all SportAI ground truth serves
        # have ball_player_distance < 1.10m, avg 0.41m)
        ball_player_dist = None
        if hitter and hitter.get("court_x") is not None and hitter.get("court_y") is not None:
            ball_player_dist = math.hypot(
                cx - hitter["court_x"],
                cy - hitter["court_y"],
            )

        # Serve detection: combines GEOMETRY + POSE + DISTANCE for high precision.
        # Stale (soft-fallback) hitters are treated as "no hitter" here —
        # serves need exact coords to get serve_side_d right, and a
        # soft-resolved position from ±1.2s back could flip the deuce/ad
        # attribution. Rally-shot rows still USE the stale hitter for
        # hit_x/y (set further below) so point structure stays intact.
        serve_diag["total_bounces"] += 1
        is_serve = False

        hitter_is_stale = bool(hitter and hitter.get("_hitter_stale"))
        if hitter is None or hitter_is_stale:
            serve_diag["no_hitter"] += 1
            geom_ok, geom_reason = False, "no_hitter"
            is_overhead = False
        else:
            geom_ok, geom_reason = _serve_geometric_check(
                hitter.get("court_y"), cx, cy, ball_player_dist,
            )
            if geom_ok:
                serve_diag["geometric_pass"] += 1
            else:
                key = f"geometric_{geom_reason}"
                if key in serve_diag:
                    serve_diag[key] += 1
            is_overhead = _is_overhead_pose(hitter.get("keypoints"), is_left_handed)
            if is_overhead:
                serve_diag["pose_pass"] += 1
            elif hitter.get("keypoints") is None:
                serve_diag["pose_no_keypoints"] += 1

        ts_since_last_serve = ts - last_serve_ts
        cooldown_ok = ts_since_last_serve >= MIN_SERVE_INTERVAL_S

        # A5+ pre-hit stationarity — real servers stand still ~1-2s before
        # contact. Warmup ball-bouncing and between-point rolls involve
        # players wandering on court, which is the single cleanest signal
        # that separates them from a real serve. Kills the two false
        # positives observed on a015bf3a (ts=27, ts=35 warmup bounces
        # before the SportAI-confirmed first serve at ts=54.48).
        stationary = None
        if geom_ok:
            stationary = _check_hitter_stationary_pre_hit(
                h_frames, h_dets, hit_frame_est, hitter, fps,
            )
            if stationary is True:
                serve_diag["stationary_ok"] += 1
            elif stationary is False:
                serve_diag["stationary_fail"] += 1
            else:
                serve_diag["stationary_nodata"] += 1
        # None = no prior samples. Benefit of doubt (don't reject real
        # far-side serves where tracking is sparse and we have no prior
        # pose data to confirm stationarity).
        stationarity_ok = stationary is not False

        # Per-candidate trace: log every bounce that passes the geometric gate
        # so we can see exactly where serves are being accepted/rejected.
        if geom_ok and logger.isEnabledFor(logging.INFO):
            hy = hitter.get("court_y") if hitter else None
            logger.info(
                "T5 serve cand frame=%d ts=%.2f hy=%.2f bx=%.2f by=%.2f dist=%s overhead=%s cooldown_ok=%s stationary=%s (since_last=%.1fs)",
                frame_idx, ts,
                hy if hy is not None else float("nan"),
                cx, cy,
                f"{ball_player_dist:.2f}" if ball_player_dist is not None else "None",
                is_overhead, cooldown_ok, stationary, ts_since_last_serve,
            )

        # Primary trigger: geometric + cooldown + stationary pre-hit.
        #
        # Pose is intentionally NOT required: the actual serve motion happens
        # ~0.5-1.0s BEFORE the bounce frame, so by the time we look at the
        # hitter's pose at the bounce, they're already in follow-through and
        # the wrist-above-shoulders test always fails. We tracked this with
        # diagnostics on task 911f0dce: 21 geometric_pass, 0 fired_primary.
        #
        # The geometric gate (hitter past a baseline + bounce on opposite half
        # of net + bounce_x in singles+alley) is precise enough on its own —
        # rally shots are almost never struck from past the baseline.
        if geom_ok and cooldown_ok and stationarity_ok and not suppress_geometric_serves:
            # First-serve-min-ts gate — no match starts with a serve in
            # the first 15s. Kills warmup false positives that slipped
            # past stationarity (e.g. near player momentarily still at
            # ts=0.3s because video JUST started and no prior samples
            # exist to refute stationarity).
            if last_serve_ts < 0 and ts < FIRST_SERVE_MIN_TS_S:
                serve_diag["first_serve_too_early"] += 1
            else:
                is_serve = True
                serve_diag["fired_primary"] += 1
                if is_overhead:
                    # Bonus signal — track for diagnostics, doesn't change outcome
                    serve_diag["fired_secondary"] += 1
        elif geom_ok and not cooldown_ok:
            serve_diag["cooldown_block"] += 1
        elif geom_ok and not stationarity_ok:
            serve_diag["stationarity_block"] += 1

        if is_serve:
            last_serve_ts = ts

        # Swing type — RULE #1/#2: the swing classifier is the bronze MODEL that
        # OWNS this fact. Prefer its answer (projected verbatim from stroke_class)
        # over silver's pose/position heuristics, which are STOPGAP-until the
        # model covers every hit. The classifier has no serve class, so serves
        # keep their own label (serve is owned by geometry/serve_events); the
        # classifier only applies to non-serves.
        swing_type = "other"
        if hitter:
            flow_class = hitter.get("stroke_class")
            if not is_serve and flow_class in ("fh", "bh", "overhead"):
                swing_type = flow_class  # bronze model wins
            else:
                # STOPGAP fallback — model produced no answer for this hit (or
                # it's a serve). Pose keypoints, then position.
                swing_type = _infer_swing_type_from_keypoints(
                    hitter.get("keypoints"), hitter.get("center_x"),
                    is_serve, is_left_handed,
                    court_y=hitter.get("court_y"),
                )
                if swing_type == "other" and not is_serve:
                    swing_type = _infer_swing_type_from_position(
                        cx, hitter.get("court_x"), hitter.get("court_y"),
                        is_serve, is_left_handed,
                    )

        # Volley detection: hitter within VOLLEY_NET_DISTANCE_M of net
        is_volley = False
        if hitter and hitter.get("court_y") is not None:
            dist_to_net = abs(hitter["court_y"] - HALF_Y)
            is_volley = dist_to_net < VOLLEY_NET_DISTANCE_M

        # Player position as ball_hit_location (where the hitter was)
        hit_x = hitter.get("court_x") if hitter else None
        hit_y = hitter.get("court_y") if hitter else None
        # Assign player_id based on court side — guarantees 2 distinct players
        # even when ML tracker only detected 1 player_id
        # If bounce on top half (cy < HALF_Y), hitter was on bottom (player[0])
        hitter_pid = str(top_pids[0]) if bounce_on_top else str(top_pids[1])

        rows_to_insert.append({
            "id": i + 1,
            "task_id": task_id,
            "player_id": hitter_pid,
            "valid": True,
            "serve": is_serve,
            "swing_type": swing_type,
            "volley": is_volley,
            "is_in_rally": True,
            "ball_player_distance": ball_player_dist,
            # silver.ball_speed is stored in km/h to match SportAI's semantic.
            # Was previously converted to m/s here; SportAI silver stores km/h
            # as-is from the bronze JSON, so the conversion caused a 3.6×
            # unit gap that the reconcile tool hid by multiplying both sides
            # by 3.6 (producing SportAI's "359 km/h avg" which is physically
            # impossible). Store km/h directly so both sides match.
            "ball_speed": speed_kmh,
            "ball_impact_type": None,
            "ball_hit_s": ts,
            "ball_hit_location_x": hit_x,
            "ball_hit_location_y": hit_y,
            "type": "floor",
            "timestamp": ts,
            "court_x": cx,
            "court_y": cy,
            "model": "t5",
        })

        prev_ts = ts

    # Log serve detection diagnostics — shows where the filter is rejecting
    logger.info("T5 serve diagnostics: %s", serve_diag)
    # Hit-location resolution by side (2b observability) — high far_stale/far_mirror
    # = far ball_hit_location is approximate; the fix is far-court calibration.
    _hr = hit_resolve_diag
    _far_tot = _hr["far_fresh"] + _hr["far_stale"] + _hr["far_mirror"] + _hr["far_none"]
    _far_approx = _hr["far_stale"] + _hr["far_mirror"] + _hr["far_none"]
    logger.info(
        "T5 hit-resolve by side: %s (far approx/total = %d/%d = %.0f%%)",
        _hr, _far_approx, _far_tot, (100.0 * _far_approx / _far_tot) if _far_tot else 0.0,
    )

    if not rows_to_insert:
        logger.warning("T5 Pass 1: no valid rows to insert")
        return 0

    # ---- Step 5b: Serve-from-events overlay (RULE #1) ----
    # Geometric serve firing was suppressed above; inherit serves from the
    # serve_detector's bronze output now. No-op unless T5_SERVE_FROM_EVENTS.
    if suppress_geometric_serves:
        _apply_serve_events_overlay(conn, task_id, rows_to_insert, top_pids)

    # ---- Step 6: Bulk INSERT ----
    return _insert_pass1_rows(conn, rows_to_insert)


def _insert_pass1_rows(conn: Connection, rows_to_insert: List[dict]) -> int:
    """Bulk-INSERT Pass-1 base-field rows. Shared by both row-generation
    strategies so the column list / conflict clause stay in one place."""
    conn.execute(sql_text(f"""
        INSERT INTO {SILVER_SCHEMA}.{TABLE} (
            id, task_id, player_id, valid, serve, swing_type, volley, is_in_rally,
            ball_player_distance, ball_speed, ball_impact_type,
            ball_hit_s, ball_hit_location_x, ball_hit_location_y,
            type, timestamp, court_x, court_y, model
        ) VALUES (
            :id, :task_id, :player_id, :valid, :serve, :swing_type, :volley, :is_in_rally,
            :ball_player_distance, :ball_speed, :ball_impact_type,
            :ball_hit_s, :ball_hit_location_x, :ball_hit_location_y,
            :type, :timestamp, :court_x, :court_y, :model
        )
        ON CONFLICT (task_id, id, model) DO NOTHING
    """), rows_to_insert)
    logger.info("T5 Pass 1: inserted %d rows into silver.point_detail", len(rows_to_insert))
    return len(rows_to_insert)


# ============================================================
# Serve-from-events overlay (RULE #1 — T5 silver inherits serve_events)
# ============================================================

def _serve_from_events_enabled() -> bool:
    """Read T5_SERVE_FROM_EVENTS at call time (env-flip rollback, no rebuild —
    same pattern as _stroke_driven_enabled).

    DEFAULT ON since 2026-06-06: the overlay shipped 2026-05-27 (fc9bc6b)
    defaulting OFF pending a Render env flip that never landed — the flag
    was in neither render.yaml nor docs/env_vars.md, so prod silver kept
    running the legacy geometric serve path the whole time (verified: the
    Jun-4 'count-aligned 24v26' pair traced only 1/24 silver serves to a
    bronze serve_event — count coincidence, not inheritance). RULE #1
    requires bronze->silver verbatim; the code default is now the truthful
    one and the env var is the rollback, not the enabler."""
    return os.getenv("T5_SERVE_FROM_EVENTS", "1").strip().lower() in ("1", "true", "yes", "on")


def _serve_events_min_conf() -> float:
    """Min serve_detector confidence to inherit a serve (env-tunable)."""
    try:
        return float(os.getenv("T5_SERVE_EVENTS_MIN_CONF", str(SERVE_EVENTS_MIN_CONF_DEFAULT)))
    except (TypeError, ValueError):
        return SERVE_EVENTS_MIN_CONF_DEFAULT


def _apply_serve_events_overlay(
    conn: Connection, task_id: str, rows_to_insert: List[dict], top_pids: list,
) -> dict:
    """Overlay serves from ml_analysis.serve_events onto the Pass-1 rows.

    Precondition: the caller has SUPPRESSED Pass-1 geometric serve firing (no
    row is a serve yet). For each serve_event >= min-conf, in ts order:
      - reuse the nearest UNCLAIMED bounce row within ±SERVE_EVENT_MATCH_TOL_S
        (no row inflation — the serve's own bounce row becomes the serve), OR
      - if none, append a fresh serve row.
    The row carries the EVENT's hitter position (x = hitter_court_x for pass-3's
    serve_side_d; y SNAPPED to the server's baseline so pass-3's baseline gate
    inherits it as serve_d). court_x/y keep the bounce (serve location 1-8).
    player_id from court SIDE (top_pids[0]=near, [1]=far).

    Finally DEMOTE any non-serve overhead-at-baseline row to swing_type='other'
    so the shared pass-3 geometric gate fires serve_d ONLY on these overlaid
    serves — making T5 silver serves == the bronze serve_events exactly.

    Mutates rows_to_insert in place. Returns a diagnostic dict.
    """
    min_conf = _serve_events_min_conf()
    diag = {"events": 0, "converted": 0, "inserted": 0, "demoted": 0, "min_conf": min_conf}

    present = conn.execute(sql_text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'ml_analysis' AND table_name = 'serve_events' LIMIT 1
    """)).scalar()
    if not present:
        logger.warning("T5 serve overlay: ml_analysis.serve_events absent — leaving rows serve-less")
        return diag

    # No hitter-coord NOT NULL filter (removed 2026-06-06): it silently
    # dropped every event whose far-player feet couldn't be projected —
    # half the conf-passing events on warp-era tasks, and ALL model_far
    # events (the Batch serve-model candidates carry bounce coords +
    # player_id, not hitter coords). Near/far comes from the event's OWN
    # player_id — the detector's verdict (trust-the-rule) — with the old
    # hitter_court_y geometry as fallback for legacy rows. NULL hitter_x
    # just means serve_side_d stays NULL downstream; a serve with an
    # unknown side beats a dropped serve.
    events = conn.execute(sql_text("""
        SELECT ts, player_id, hitter_court_x, hitter_court_y,
               bounce_court_x, bounce_court_y
        FROM ml_analysis.serve_events
        WHERE task_id::text = :tid AND confidence >= :thr
        ORDER BY ts
    """), {"tid": task_id, "thr": min_conf}).fetchall()
    diag["events"] = len(events)

    claimed: set = set()
    next_id = max((r["id"] for r in rows_to_insert), default=0) + 1
    eps = EPS_BASELINE_M

    for ts, ev_pid, hx, hy, b_cx, b_cy in events:
        ts = float(ts)
        hx = float(hx) if hx is not None else None
        hy = float(hy) if hy is not None else None
        if ev_pid is not None:
            near = int(ev_pid) == 0
        elif hy is not None:
            near = hy > HALF_Y
        else:
            continue  # no side evidence at all — cannot place the serve
        pid = str(top_pids[0]) if near else str(top_pids[1])
        snap_y = COURT_LENGTH_M if near else 0.0  # snap to baseline → pass-3 serve_d gate passes

        best_i, best_d = None, None
        for i, r in enumerate(rows_to_insert):
            if i in claimed:
                continue
            rt = r.get("ball_hit_s")
            if rt is None:
                continue
            d = abs(rt - ts)
            if best_d is None or d < best_d:
                best_d, best_i = d, i

        if best_i is not None and best_d <= SERVE_EVENT_MATCH_TOL_S:
            r = rows_to_insert[best_i]
            r.update(serve=True, swing_type="overhead", volley=False, player_id=pid,
                     ball_hit_location_x=hx, ball_hit_location_y=snap_y,
                     ball_hit_s=ts, timestamp=ts)
            claimed.add(best_i)
            diag["converted"] += 1
        else:
            rows_to_insert.append({
                "id": next_id, "task_id": task_id, "player_id": pid, "valid": True,
                "serve": True, "swing_type": "overhead", "volley": False, "is_in_rally": True,
                "ball_player_distance": None, "ball_speed": None, "ball_impact_type": None,
                "ball_hit_s": ts, "ball_hit_location_x": hx, "ball_hit_location_y": snap_y,
                "type": "floor", "timestamp": ts,
                "court_x": float(b_cx) if b_cx is not None else None,
                "court_y": float(b_cy) if b_cy is not None else None,
                "model": "t5",
            })
            next_id += 1
            diag["inserted"] += 1

    # Demote stray overhead-at-baseline non-serves so pass-3 doesn't re-flag them.
    for i, r in enumerate(rows_to_insert):
        if i in claimed or r.get("serve"):
            continue
        y = r.get("ball_hit_location_y")
        if (y is not None and (y < eps or y > COURT_LENGTH_M - eps)
                and str(r.get("swing_type", "")).lower() in _OVERHEAD_SWINGS):
            r["swing_type"] = "other"
            diag["demoted"] += 1

    logger.info("T5 serve overlay (T5_SERVE_FROM_EVENTS): %s", diag)
    return diag


# ============================================================
# T5 PASS 1 (stroke-driven): one stroke contact → one silver row
# ============================================================

def _t5_pass1_load_stroke_driven(conn: Connection, task_id: str, job_id: str, fps: float) -> int:
    """Phase 6: generate one silver row per detected stroke contact.

    Iterates ml_analysis.stroke_events (pose wrist-velocity peaks) instead of
    ball bounces. Each stroke becomes a silver row whose ball_hit_location is
    the hitter's court position at predicted_hit_frame; bounce coords
    (court_x/court_y/ball_speed) are joined when a ball bounce falls within ~1s
    after the hit. This recovers groundstrokes whose bounce TrackNet missed —
    the Forehand undercount the bounce-driven path exposed (active fh 17 vs
    SA 38 on Match 1).

    Hitter SIDE is resolved from the reliable bounce-opposite-side signal when
    a bounce is matched, otherwise from the attributed player's own track.
    The stroke detector's player_id attribution is perspective-biased toward
    the near player (it picks whichever wrist has the highest *pixel* velocity),
    so it is NOT used to assign the silver player_id — side is taken from court
    position, preserving the bounce-driven invariant that the two silver players
    map to the two court ends (needed by pass3 serve/point numbering).
    """
    import bisect

    strokes = conn.execute(sql_text("""
        SELECT predicted_hit_frame, player_id, confidence
        FROM ml_analysis.stroke_events
        WHERE task_id::text = :tid
        ORDER BY predicted_hit_frame
    """), {"tid": task_id}).fetchall()
    if not strokes:
        logger.info("T5 Pass 1 (stroke-driven): no stroke events for task=%s", task_id)
        return 0

    # Shared player-detection buckets + dominant hand.
    buckets = _build_player_buckets(conn, job_id)
    near_frames, near_dets = buckets["near_frames"], buckets["near_dets"]
    far_frames, far_dets = buckets["far_frames"], buckets["far_dets"]
    any_frames, any_dets = buckets["any_frames"], buckets["any_dets"]
    near_kp_frames, near_kp_dets = buckets["near_kp_frames"], buckets["near_kp_dets"]
    far_kp_frames, far_kp_dets = buckets["far_kp_frames"], buckets["far_kp_dets"]
    dets_by_pid = buckets["dets_by_pid"]
    pid_map, top_pids = buckets["pid_map"], buckets["top_pids"]
    is_left_handed = _lookup_dominant_hand(conn, task_id)

    # Bounce index (court coords only) for the stroke→bounce join.
    bounce_rows = conn.execute(sql_text("""
        SELECT frame_idx, court_x, court_y, speed_kmh, is_in
        FROM ml_analysis.ball_detections
        WHERE job_id = :jid AND is_bounce = TRUE
          AND court_x IS NOT NULL AND court_y IS NOT NULL
        ORDER BY frame_idx
    """), {"jid": job_id}).fetchall()
    bounce_frames = [b[0] for b in bounce_rows]

    # Windows / thresholds mirror the bounce-driven path.
    HIT_WINDOW_FRAMES = max(1, int(round(fps * 0.20)))
    HIT_SOFT_WINDOW_FRAMES = max(1, int(round(fps * 1.20)))
    KP_WINDOW_FRAMES = max(1, int(round(fps * 1.20)))
    # Ball struck at predicted_hit_frame → crosses net → bounces on opponent's
    # side. The matching bounce is the first one in (hit, hit + ~1s].
    BOUNCE_AFTER_FRAMES = max(1, int(round(fps * 1.0)))
    MIN_SERVE_INTERVAL_S = 8.0
    FIRST_SERVE_MIN_TS_S = 15.0

    logger.info(
        "T5 Pass 1 (stroke-driven): %d stroke events, %d court bounces, job=%s at %.1f fps",
        len(strokes), len(bounce_rows), job_id, fps,
    )

    diag = {
        "strokes": len(strokes), "bounce_matched": 0, "no_bounce": 0,
        "side_from_bounce": 0, "side_from_attribution": 0,
        "unresolved": 0, "kp_patched": 0, "fired_serve": 0,
    }

    rows_to_insert: List[dict] = []
    last_serve_ts = -999.0
    for i, (hf, raw_stroke_pid, _conf) in enumerate(strokes):
        ts = hf / fps if fps > 0 else 0.0

        # ---- Match a bounce in (hf, hf + ~1s] ----
        bi = bisect.bisect_left(bounce_frames, hf)
        matched_bounce = None
        if bi < len(bounce_frames) and bounce_frames[bi] <= hf + BOUNCE_AFTER_FRAMES:
            matched_bounce = bounce_rows[bi]
        if matched_bounce is not None:
            b_cx, b_cy, b_speed, b_is_in = (
                matched_bounce[1], matched_bounce[2], matched_bounce[3], matched_bounce[4],
            )
            diag["bounce_matched"] += 1
        else:
            b_cx = b_cy = b_speed = b_is_in = None
            diag["no_bounce"] += 1

        # ---- Resolve hitter SIDE (near = court_y > HALF_Y) ----
        hitter_side_near = None
        if matched_bounce is not None:
            # Ball bounced on one half → hitter struck from the OTHER half.
            # Bucket convention (matches bounce-driven path): bounce on the
            # top/far half (court_y < HALF_Y) ⇒ hitter is in the NEAR bucket
            # (court_y > HALF_Y, which includes the near baseline ~23.77).
            hitter_side_near = (b_cy < HALF_Y)
            diag["side_from_bounce"] += 1
        else:
            # Fallback: attributed player's own court position. Biased, last
            # resort — only used when no bounce pins the side.
            mapped_pid = pid_map.get(raw_stroke_pid, str(raw_stroke_pid))
            pid_index = dets_by_pid.get(mapped_pid)
            attr = None
            if pid_index is not None:
                pf, pd_list = pid_index
                attr = (_find_nearest_detection(pf, pd_list, hf, HIT_WINDOW_FRAMES)
                        or _find_nearest_detection(pf, pd_list, hf, HIT_SOFT_WINDOW_FRAMES))
            if attr is not None and attr.get("court_y") is not None:
                hitter_side_near = attr["court_y"] > HALF_Y
                diag["side_from_attribution"] += 1

        if hitter_side_near is None:
            diag["unresolved"] += 1
            continue

        # ---- Look up hitter pose+position on the resolved side ----
        if hitter_side_near:
            h_frames, h_dets = near_frames, near_dets
            kp_frames, kp_dets = near_kp_frames, near_kp_dets
        else:
            h_frames, h_dets = far_frames, far_dets
            kp_frames, kp_dets = far_kp_frames, far_kp_dets

        hitter = _find_nearest_detection(h_frames, h_dets, hf, HIT_WINDOW_FRAMES)
        if hitter is None and h_dets:
            hitter = _find_nearest_detection(h_frames, h_dets, hf, HIT_SOFT_WINDOW_FRAMES)
        # Mirror fallback: no detection on the resolved side → borrow any player
        # with coords and mirror them onto the hitter's side (same shape as the
        # bounce-driven mirror path).
        if hitter is None and any_dets:
            other = _find_nearest_detection(any_frames, any_dets, hf, HIT_SOFT_WINDOW_FRAMES)
            if other is not None and other.get("court_y") is not None:
                other_near = other["court_y"] > HALF_Y
                mirror_y = (COURT_LENGTH_M - other["court_y"]) if other_near != hitter_side_near else other["court_y"]
                mirror_y = max(0.0, min(COURT_LENGTH_M, mirror_y))
                hitter = {
                    "frame_idx": other["frame_idx"], "player_id": other["player_id"],
                    "court_x": other["court_x"], "court_y": mirror_y,
                    "center_x": other.get("center_x"), "center_y": other.get("center_y"),
                    "keypoints": other.get("keypoints"), "_synthesized": True,
                }

        if hitter is None:
            diag["unresolved"] += 1
            continue

        # Dual-window keypoint patch (mirrors the bounce path A4 step).
        if hitter.get("keypoints") is None and not hitter.get("_synthesized"):
            kp_match = _find_nearest_detection(kp_frames, kp_dets, hf, KP_WINDOW_FRAMES)
            if kp_match is not None:
                hitter = dict(hitter)
                hitter["keypoints"] = kp_match.get("keypoints")
                diag["kp_patched"] += 1

        hit_x = hitter.get("court_x")
        hit_y = hitter.get("court_y")

        ball_player_dist = None
        if b_cx is not None and hit_x is not None and hit_y is not None:
            ball_player_dist = math.hypot(b_cx - hit_x, b_cy - hit_y)

        # ---- Serve detection: requires a matched bounce (serves bounce in the
        # box). Same geometric + cooldown + stationarity + first-serve gates as
        # the bounce-driven path. pass3 re-derives serve_d from swing_type +
        # baseline-y, so this mainly drives swing_type='overhead'. ----
        is_serve = False
        if matched_bounce is not None and hit_y is not None:
            geom_ok, _reason = _serve_geometric_check(hit_y, b_cx, b_cy, ball_player_dist)
            if geom_ok:
                cooldown_ok = (ts - last_serve_ts) >= MIN_SERVE_INTERVAL_S
                stationary = _check_hitter_stationary_pre_hit(h_frames, h_dets, hf, hitter, fps)
                stationarity_ok = stationary is not False
                if cooldown_ok and stationarity_ok and not (last_serve_ts < 0 and ts < FIRST_SERVE_MIN_TS_S):
                    is_serve = True
                    last_serve_ts = ts
                    diag["fired_serve"] += 1

        # ---- swing type — prefer the bronze classifier (see bounce-driven path) ----
        flow_class = hitter.get("stroke_class")
        if not is_serve and flow_class in ("fh", "bh", "overhead"):
            swing_type = flow_class  # bronze model wins
        else:
            swing_type = _infer_swing_type_from_keypoints(
                hitter.get("keypoints"), hitter.get("center_x"),
                is_serve, is_left_handed, court_y=hit_y,
            )
            if swing_type == "other" and not is_serve and b_cx is not None:
                # Position fallback needs a ball x — only available with a bounce.
                swing_type = _infer_swing_type_from_position(
                    b_cx, hit_x, hit_y, is_serve, is_left_handed,
                )

        is_volley = hit_y is not None and abs(hit_y - HALF_Y) < VOLLEY_NET_DISTANCE_M
        hitter_pid = str(top_pids[0]) if hitter_side_near else str(top_pids[1])

        rows_to_insert.append({
            "id": i + 1,
            "task_id": task_id,
            "player_id": hitter_pid,
            "valid": True,
            "serve": is_serve,
            "swing_type": swing_type,
            "volley": is_volley,
            "is_in_rally": True,
            "ball_player_distance": ball_player_dist,
            "ball_speed": b_speed,
            "ball_impact_type": None,
            "ball_hit_s": ts,
            "ball_hit_location_x": hit_x,
            "ball_hit_location_y": hit_y,
            "type": "floor",
            "timestamp": ts,
            "court_x": b_cx,
            "court_y": b_cy,
            "model": "t5",
        })

    logger.info("T5 Pass 1 (stroke-driven) diagnostics: %s", diag)
    if not rows_to_insert:
        logger.warning("T5 Pass 1 (stroke-driven): no valid rows to insert")
        return 0
    return _insert_pass1_rows(conn, rows_to_insert)


# ============================================================
# T5 PASS 1 dispatcher
# ============================================================

def _stroke_driven_enabled() -> bool:
    """Read the T5_STROKE_DRIVEN_SILVER gate at call time (so flipping the env
    var + restart applies without a code change / Docker rebuild — same
    rollback pattern as the WASB swap, memory feedback_env_var_rollback_pattern).
    """
    return os.getenv("T5_STROKE_DRIVEN_SILVER", "0").strip().lower() in ("1", "true", "yes", "on")


def _t5_pass1_load(conn: Connection, task_id: str, job_id: str, fps: float) -> int:
    """Pick the Pass-1 row-generation strategy.

    BOUNCE-DRIVEN IS THE LIVE PROD PATH. The stroke-driven path (Phase 6 step 2)
    is GATED OFF behind T5_STROKE_DRIVEN_SILVER and MUST stay off until the T5
    bronze (ml_analysis.*) 18 base fields reconcile to SportAI. Reason (proven
    2026-05-25): stroke-driven row generation overshoots (Match 1: 141 vs SA's
    84 active, near 114/27 vs SA 43/41) because the stroke detector's hitter
    attribution is perspective-biased to the near player and far pose is sparse.
    That is a BRONZE-accuracy problem, not a silver one — see CLAUDE.md "Things
    not to do" #11 and docs/north_star.md. Flip the env var on (no redeploy)
    once far-pose coverage + bounce accuracy land.

    The information_schema existence check runs BEFORE any SELECT on
    stroke_events so a missing table on an older deployment can't poison the
    transaction (memory feedback_postgres_missing_table).
    """
    if _stroke_driven_enabled():
        tbl = conn.execute(sql_text("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'ml_analysis' AND table_name = 'stroke_events'
            LIMIT 1
        """)).scalar()
        if tbl:
            n = conn.execute(sql_text(
                "SELECT COUNT(*) FROM ml_analysis.stroke_events WHERE task_id::text = :tid"
            ), {"tid": task_id}).scalar() or 0
            if n > 0:
                logger.info(
                    "T5 Pass 1: stroke-driven row generation ENABLED via "
                    "T5_STROKE_DRIVEN_SILVER (task=%s, %d stroke events)", task_id, n,
                )
                return _t5_pass1_load_stroke_driven(conn, task_id, job_id, fps)
        logger.info("T5 Pass 1: stroke-driven enabled but no stroke events — bounce-driven (task=%s)", task_id)
    else:
        logger.info("T5 Pass 1: bounce-driven (live default; stroke-driven gated off) task=%s", task_id)
    return _t5_pass1_load_bounce_driven(conn, task_id, job_id, fps)


def _build_detection_index(dets: List[dict]) -> Tuple[List[int], List[dict]]:
    """Pre-compute sorted frame index for binary search."""
    return [d["frame_idx"] for d in dets], dets


def _min_player_distance_m(any_frames: List[int], any_dets: List[dict],
                           bounce_frame: int, bounce_cx: float, bounce_cy: float,
                           frame_window: int) -> Optional[float]:
    """Min court-distance (m) from a bounce to any player detected within
    ±frame_window frames. Returns None if no player detection in the window
    (then the caller keeps the bounce — we can't gate without evidence).

    Used by the bounce-precision proximity guard (BOUNCE_PLAYER_PROXIMITY_M).
    """
    import bisect
    lo = bisect.bisect_left(any_frames, bounce_frame - frame_window)
    hi = bisect.bisect_right(any_frames, bounce_frame + frame_window)
    best: Optional[float] = None
    for d in any_dets[lo:hi]:
        cx, cy = d.get("court_x"), d.get("court_y")
        if cx is None or cy is None:
            continue
        dist = ((cx - bounce_cx) ** 2 + (cy - bounce_cy) ** 2) ** 0.5
        if best is None or dist < best:
            best = dist
    return best


def _check_hitter_stationary_pre_hit(
    h_frames: List[int], h_dets: List[dict],
    hit_frame_est: int, hitter: Optional[dict], fps: float,
    threshold_m: float = 0.5,
) -> Optional[bool]:
    """A5+ pre-hit stationarity gate — serves require the hitter to be
    roughly still in the 1-2 seconds before contact (ball-toss stance,
    preparation). Warmup and between-point bounces fail this because
    both players are wandering.

    Samples the hitter's own side (h_dets) at hit_frame - 1s and
    hit_frame - 2s, each with ±0.3s tolerance. Compares each sample's
    court_x/y to the hitter's current position.

    Returns
    -------
    True
        All prior samples within threshold_m of the hitter. Serve OK.
    False
        At least one prior sample > threshold_m away. Player was moving.
    None
        No prior samples found in either window. Can't confirm, caller
        should give benefit of doubt (sparse far-side tracking often
        leaves real far serves with no prior pose data).

    Reference: academic systems (TenniSet, TAL4Tennis) use rally-state
    labels or player-stationarity windows to reject warmup bounces.
    yastrebksv/TennisProject and ArtLabss/tennis-tracking have no such
    gate and so over-count serve-like bounces during practice footage.
    """
    if hitter is None or hitter.get("court_x") is None or hitter.get("court_y") is None:
        return False

    tol_frames = max(1, int(round(fps * 0.3)))
    samples: List[dict] = []
    for offset_s in (1.0, 2.0):
        target = max(0, hit_frame_est - int(round(fps * offset_s)))
        prior = _find_nearest_detection(
            h_frames, h_dets, target, max_distance_frames=tol_frames,
        )
        if prior is not None and prior.get("court_x") is not None \
                and prior.get("court_y") is not None:
            samples.append(prior)

    if not samples:
        return None

    for s in samples:
        dx = abs(s["court_x"] - hitter["court_x"])
        dy = abs(s["court_y"] - hitter["court_y"])
        if dx > threshold_m or dy > threshold_m:
            return False
    return True


def _find_nearest_detection(frames: List[int], dets: List[dict],
                            target_frame: int,
                            max_distance_frames: Optional[int] = None) -> Optional[dict]:
    """Find the player detection closest in frame_idx to the target (binary search).

    When ``max_distance_frames`` is given, returns ``None`` if the nearest
    detection lies outside that window. This prevents stale detections from
    being silently reused on bounces where the hitter's side has sparse
    temporal coverage (seen with far-player at ~10% frame coverage — every
    bounce would inherit the same single far-side detection, so
    ``serve_side_d`` never alternated and points collapsed).
    """
    if not dets:
        return None

    import bisect
    idx = bisect.bisect_left(frames, target_frame)

    best = None
    best_dist = float("inf")
    for candidate_idx in (idx - 1, idx):
        if 0 <= candidate_idx < len(dets):
            dist = abs(dets[candidate_idx]["frame_idx"] - target_frame)
            if dist < best_dist:
                best_dist = dist
                best = dets[candidate_idx]

    if max_distance_frames is not None and best_dist > max_distance_frames:
        return None
    return best


# ============================================================
# MAIN BUILDER
# ============================================================

def build_silver_match_t5(task_id: str, replace: bool = True,
                          engine=None) -> Dict:
    """
    Build silver.point_detail from T5 ML pipeline bronze data for a singles match.

    Pipeline:
      1. T5 Pass 1: Extract bounces → 18 base fields (player_id, serve, swing_type, etc.)
      2. Pass 3: Point context (serve detection, point numbering, game structure) — SHARED
      3. Pass 4: Zone classification + coordinate normalization — SHARED
      4. Pass 5: Analytics (serve bucket, stroke, rally length, aggression, depth) — SHARED

    Args:
        task_id: task_id/job_id from ml_analysis.video_analysis_jobs
        replace: if True, delete existing T5 rows before rebuilding
        engine: SQLAlchemy engine (auto-resolved if None)

    Returns:
        dict with pass row counts and metadata
    """
    if engine is None:
        from db_init import engine as db_engine
        engine = db_engine

    # Import shared passes from build_silver_v2
    from build_silver_v2 import (
        ensure_schema,
        pass3_point_context,
        pass4_zones_and_normalize,
        pass5_analytics,
    )

    out: Dict = {"task_id": task_id, "model": "t5"}

    with engine.begin() as conn:
        # Ensure schema + columns exist (including model column)
        ensure_schema(conn)

        # Resolve job metadata
        job_row = conn.execute(sql_text("""
            SELECT j.job_id, j.total_frames, j.video_duration_sec, j.video_fps,
                   j.court_detected, j.court_confidence
            FROM ml_analysis.video_analysis_jobs j
            WHERE j.job_id = :tid OR j.task_id = :tid
            LIMIT 1
        """), {"tid": task_id}).mappings().first()

        if not job_row:
            logger.warning("T5 match builder: no job found for task_id=%s", task_id)
            return {"ok": False, "error": "job not found"}

        job_id = job_row["job_id"]
        out["job_id"] = job_id
        out["court_detected"] = job_row.get("court_detected")
        out["court_confidence"] = job_row.get("court_confidence")

        # Derive effective FPS (frame_idx is in sampled space)
        total_frames = job_row.get("total_frames")
        duration = job_row.get("video_duration_sec")
        if total_frames and duration and duration > 0:
            fps = total_frames / duration
        else:
            fps = job_row.get("video_fps") or 25.0
        out["fps"] = fps

        logger.info("T5 match builder: task_id=%s job_id=%s fps=%.1f court_detected=%s",
                     task_id, job_id, fps, job_row.get("court_detected"))

        # Clean slate for T5 rows (preserve SportAI rows if any)
        if replace:
            conn.execute(sql_text(
                f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id = :tid AND model = 't5'"
            ), {"tid": task_id})

        # T5 Pass 1: Extract bounces → 18 base fields
        out["pass1_rows"] = _t5_pass1_load(conn, task_id, job_id, fps)

        if out["pass1_rows"] == 0:
            logger.warning("T5 match builder: pass 1 produced 0 rows — skipping passes 3-5")
            return out

        # Shared passes from build_silver_v2.py — these operate on
        # silver.point_detail WHERE task_id = :tid
        # They work on ALL rows for this task_id (both models if present)
        cfg = SPORT_CONFIG_SINGLES

        try:
            out["pass3_rows"] = pass3_point_context(conn, task_id, cfg)
        except Exception as e:
            logger.warning("T5 match builder: pass 3 failed (non-fatal): %s", e)
            out["pass3_error"] = str(e)
            out["pass3_rows"] = 0

        try:
            out["pass4_rows"] = pass4_zones_and_normalize(conn, task_id, cfg)
        except Exception as e:
            logger.warning("T5 match builder: pass 4 failed (non-fatal): %s", e)
            out["pass4_error"] = str(e)
            out["pass4_rows"] = 0

        try:
            out["pass5_rows"] = pass5_analytics(conn, task_id, cfg)
        except Exception as e:
            logger.warning("T5 match builder: pass 5 failed (non-fatal): %s", e)
            out["pass5_error"] = str(e)
            out["pass5_rows"] = 0

    logger.info("T5 match builder COMPLETE: %s", out)
    return out
