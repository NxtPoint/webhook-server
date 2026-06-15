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

from ml_pipeline.ball_merge import MAIN_ONLY_WHERE

logger = logging.getLogger(__name__)


def _kps_to_array(raw) -> Optional["np.ndarray"]:
    """Compact JSON/list keypoints to numpy float32 (17, 3) or None.

    Same helper used by serve_detector / stroke_detector — collapses each
    keypoints row from ~2KB Python list to ~204 bytes numpy array. Applied at
    load time in _build_player_buckets, this drops silver-build peak heap on
    a ~44-min match from ~269MB to ~110MB (the dominant allocator was the
    72k-row player_dets list with nested-list keypoints)."""
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
# in the a015bf3a reconcile (Apr 16). Zero SportAI impact (their hy
# sits at ~24.47 or ~0.0, nowhere near the 0.3-1.5m band the widening
# opens). This constant is the one the T5 builder actually uses — the
# matching value in build_silver_v2.py is overridden by this via
# SPORT_CONFIG_SINGLES below. (Still used by pass-3's shared serve_d
# gate via SPORT_CONFIG and by the overlay's baseline snap/demote.)
EPS_BASELINE_M = 1.5

# Thresholds for match analysis
VOLLEY_NET_DISTANCE_M = 2.0  # hitter within 2m of net = volley. Was 4.0 (mid-court),
# which over-counted volleys 13 vs SA's 6 on Match 1. A volley is physically struck
# close to the net, so 2.0m is the motivated value. Now only used by the dormant
# bounce-driven path (the live path reads stroke_events.volley verbatim).
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
# 0.0 = inherit EVERY bronze serve_event verbatim (Tomo, 2026-06-06: "I want
# literally everything verbatim" — RULE #1; bronze precision is bronze's
# problem, silver does no filtering). Env-tunable via T5_SERVE_EVENTS_MIN_CONF
# if a quality gate is ever needed again; the old 0.70 was tuned to
# count-align Match 1 to SA (26≈26) — a silver-side judgment, removed.
SERVE_EVENTS_MIN_CONF_DEFAULT = 0.0
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

# Legacy in-silver serve helpers (_serve_geometric_check, _is_serve_geometric,
# _is_overhead_pose, _check_hitter_stationary_pre_hit) DELETED 2026-06-07 —
# serve is a pure bronze import from ml_analysis.serve_events (RULE #1).
# The serve MODELS live in ml_pipeline/serve_detector + ml_pipeline/serve_model.




# NOTE 2026-06-14 (ADR-02 revision): the silver swing-type heuristics
# _infer_swing_type_from_keypoints() and _infer_swing_type_from_position()
# were DELETED here. Swing type is a BRONZE fact owned by the v2 classifier
# (ml_pipeline/stroke_classifier/, emits stroke_class) and projected verbatim
# in Pass 1. Silver does NO swing inference of its own (rule #1/#2). Volley is
# also a BRONZE fact now (stroke_events.volley, no-bounce-since-hit, 2026-06-15),
# projected verbatim in the stroke-driven path; VOLLEY_NET_DISTANCE_M survives
# ONLY in the dormant bounce-driven path (retirement candidate).
# See docs/_investigation/adr_02_swing_type_classifier_plan.md.


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
        "pid_map": pid_map, "top_pids": top_pids,
        "n_player_dets": len(player_dets),
    }


# ============================================================
# Bounce source — the MODEL table, not the legacy is_bounce flag
# ============================================================

def _bounce_from_model_enabled() -> bool:
    """Silver takes bounce coords from the bounce MODEL (`ml_analysis.ball_bounces`,
    the CNN's curated court-coord events) verbatim, NOT the legacy is_bounce
    velocity-reversal flag on `ball_detections`. Default ON (2026-06-14, the
    bronze-MODEL-first rule #1/#2). Set `T5_BOUNCE_FROM_MODEL=0` to fall back to
    is_bounce. Env-rollback pattern (feedback_env_var_rollback_pattern)."""
    return os.getenv("T5_BOUNCE_FROM_MODEL", "1").strip().lower() in ("1", "true", "yes", "on")


def _load_bounce_index(conn: Connection, job_id: str):
    """Return [(frame_idx, court_x, court_y, speed_kmh, is_in), …] ordered by
    frame_idx — the bounce set the stroke→bounce coordinate join consumes.

    Source = the bounce MODEL (`ml_analysis.ball_bounces`) when enabled (default)
    AND it has rows for this job; otherwise the legacy is_bounce flag. The model
    table carries no speed / in-bounds, so those come back None (ball_speed is
    nullable; is_in is unused downstream). The model is EMPTY on pre-rev-66 tasks
    — those fall through to is_bounce automatically, so no task regresses to zero
    bounces. The tuple shape matches the legacy query exactly, so callers are
    unchanged. MAIN-ONLY on the is_bounce fallback keeps roi-source bounces out
    of silver (the model table is already curated, so no such filter needed)."""
    if _bounce_from_model_enabled():
        rows = conn.execute(sql_text("""
            SELECT frame_idx, court_x, court_y,
                   NULL::float AS speed_kmh, NULL::boolean AS is_in
            FROM ml_analysis.ball_bounces
            WHERE job_id::text = :jid
              AND court_x IS NOT NULL AND court_y IS NOT NULL
            ORDER BY frame_idx
        """), {"jid": job_id}).fetchall()
        if rows:
            return rows
        logger.info("T5 bounce source: ball_bounces empty for job=%s — "
                    "falling back to legacy is_bounce", job_id)
    return conn.execute(sql_text(f"""
        SELECT frame_idx, court_x, court_y, speed_kmh, is_in
        FROM ml_analysis.ball_detections
        WHERE job_id = :jid AND is_bounce = TRUE AND {MAIN_ONLY_WHERE}
          AND court_x IS NOT NULL AND court_y IS NOT NULL
        ORDER BY frame_idx
    """), {"jid": job_id}).fetchall()


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
    # MAIN-ONLY: silver Pass-1 is bounce-driven (one row per bounce); roi-source
    # bounces (roi_prod / roi_far_ball) would add net-new shot events and 2.4x
    # the active count (bronze_ingest_t5.py:272). The roi_far_ball arc feeds the
    # bounce DETECTOR + hit model, NOT silver's bounce-driven row generation.
    bounces = conn.execute(sql_text(f"""
        SELECT frame_idx, x, y, court_x, court_y, speed_kmh, is_in
        FROM ml_analysis.ball_detections
        WHERE job_id = :jid AND is_bounce = TRUE AND {MAIN_ONLY_WHERE}
          AND court_x IS NOT NULL AND court_y IS NOT NULL
        ORDER BY frame_idx
    """), {"jid": job_id}).fetchall()

    if not bounces:
        # Fallback: try without court_x/court_y filter
        bounces = conn.execute(sql_text(f"""
            SELECT frame_idx, x, y, court_x, court_y, speed_kmh, is_in
            FROM ml_analysis.ball_detections
            WHERE job_id = :jid AND is_bounce = TRUE AND {MAIN_ONLY_WHERE}
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

    # SERVE IS A PURE BRONZE IMPORT (Tomo, 2026-06-07): the legacy in-silver
    # geometric serve derivation (geometric gate + overhead pose + cooldown +
    # stationarity + first-serve-min-ts, ~150 lines) was DELETED in this
    # commit. Pass 1 emits serve=False on every row; serves come SOLELY from
    # ml_analysis.serve_events via _apply_serve_events_overlay (unconditional,
    # min-conf 0). RULE #1: silver inherits bronze 100% and does no work.

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

    for i, b in enumerate(bounces):
        frame_idx, px, py, cx, cy, speed_kmh, is_in = b

        ts = frame_idx / fps

        if cx is None or cy is None:
            # No court coords — skip (we can't do spatial analysis)
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
            hitter = _find_nearest_detection(
                h_frames, h_dets, hit_frame_est,
                max_distance_frames=HIT_SOFT_WINDOW_FRAMES,
            )
            if hitter is not None:
                hitter = dict(hitter)  # copy so we don't mutate the index
                hitter["_hitter_stale"] = True

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

        # Ball-player distance — a base field on the row (and pass-3 input).
        ball_player_dist = None
        if hitter and hitter.get("court_x") is not None and hitter.get("court_y") is not None:
            ball_player_dist = math.hypot(
                cx - hitter["court_x"],
                cy - hitter["court_y"],
            )

        # SERVE: pure bronze import — Pass 1 never decides serves. The
        # ~150-line geometric/pose/cooldown/stationarity serve derivation
        # that lived here was deleted 2026-06-07 (Tomo: silver does no
        # work); _apply_serve_events_overlay() sets serve=True on the rows
        # bronze serve_events claims, after this loop.
        is_serve = False

        # Swing type — RULE #1/#2: the swing classifier is the bronze MODEL that
        # OWNS this fact. Prefer its answer (projected verbatim from stroke_class)
        # over silver's pose/position heuristics, which are STOPGAP-until the
        # model covers every hit. The classifier has no serve class, so serves
        # keep their own label (serve is owned by geometry/serve_events); the
        # classifier only applies to non-serves.
        swing_type = "other"
        if hitter:
            flow_class = hitter.get("stroke_class")
            if not is_serve and flow_class in ("fh", "bh", "overhead", "other"):
                swing_type = flow_class  # bronze model owns this fact (projected verbatim)
            # else: no model answer (weights absent / disabled / serve) -> 'other'.
            # Silver does NO swing inference of its own. The pose/position
            # heuristics were DELETED 2026-06-14 (ADR-02 revision; rule #1/#2):
            # the classifier owns {fh,bh,overhead,other} to ceiling; accuracy
            # fills in at train-last. T5 silver is not prod-consumed, so the
            # interim 'other'-heavy output while weights are disabled is benign.

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
    # UNCONDITIONAL: serves are a pure bronze import from
    # ml_analysis.serve_events. There is no fallback serve derivation —
    # the legacy geometric path was deleted 2026-06-07.
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

# T5_SERVE_FROM_EVENTS flag DELETED 2026-06-07: the overlay is now
# unconditional and the legacy geometric serve path it used to toggle
# against no longer exists (pure bronze import — Tomo directive). History:
# shipped default-OFF 2026-05-27 (fc9bc6b), Render env flip never landed,
# prod ran the legacy path 10 days (count coincidence masked it —
# feedback_count_alignment_is_not_provenance), default flipped ON
# 2026-06-06 (d4ebb95), deleted along with the legacy path 2026-06-07.


def _serve_events_min_conf() -> float:
    """Min serve_detector confidence to inherit a serve (env-tunable)."""
    try:
        return float(os.getenv("T5_SERVE_EVENTS_MIN_CONF", str(SERVE_EVENTS_MIN_CONF_DEFAULT)))
    except (TypeError, ValueError):
        return SERVE_EVENTS_MIN_CONF_DEFAULT


def _ab_pid(near: bool, ts: float, top_pids: list, ident) -> str:
    """Map a hitter's SIDE (near) at time `ts` to the STABLE person token (A/B)
    via the identity segments, so player_id survives changeovers (matches SA's
    person-based id). top_pids[0] = A token, top_pids[1] = B token. Falls back to
    the side-based assignment when there's no identity lookup or the game's segment
    is low-confidence (never worse than side-based). Used by BOTH the Pass-1 rally
    rows AND the serve overlay so serve + rally share one consistent A/B id."""
    import bisect as _bis
    side_pid = str(top_pids[0]) if near else str(top_pids[1])
    if not ident:
        return side_pid
    starts, anear, conf = ident
    gi = _bis.bisect_right(starts, ts) - 1
    if gi >= 0 and anear[gi] is not None and conf[gi] >= 0.5:
        return str(top_pids[0]) if (near == anear[gi]) else str(top_pids[1])
    return side_pid


def _apply_serve_events_overlay(
    conn: Connection, task_id: str, rows_to_insert: List[dict], top_pids: list,
    ident_lookup=None,
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
        pid = _ab_pid(near, ts, top_pids, ident_lookup)  # stable A/B (serve rows too)
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

    logger.info("T5 serve overlay (bronze serve_events, unconditional): %s", diag)
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
        SELECT predicted_hit_frame, player_id, confidence,
               ball_hit_location_x, ball_hit_location_y, hitter_side_near, volley
        FROM ml_analysis.stroke_events
        WHERE task_id::text = :tid
        ORDER BY predicted_hit_frame
    """), {"tid": task_id}).fetchall()
    if not strokes:
        logger.info("T5 Pass 1 (stroke-driven): no stroke events for task=%s", task_id)
        return 0

    # Swing-type carriers (bronze stroke_class) by side — silver projects the
    # classifier verbatim. Hit location + SIDE now come VERBATIM from bronze
    # stroke_events (the model owns the hit fact, 867119f), so the hit-
    # reconstruction buckets (near/far/any/kp/pid_map) are no longer
    # read here — that assembly moved to stroke_detector.hit_location.
    buckets = _build_player_buckets(conn, job_id)
    near_sc_frames, near_sc_dets = buckets["near_sc_frames"], buckets["near_sc_dets"]
    far_sc_frames, far_sc_dets = buckets["far_sc_frames"], buckets["far_sc_dets"]
    top_pids = buckets["top_pids"]

    # Bounce index (court coords only) for the stroke→bounce join. Source = the
    # bounce MODEL (ml_analysis.ball_bounces) verbatim when present, else the
    # legacy is_bounce flag (pre-rev-66 tasks). See _load_bounce_index.
    bounce_rows = _load_bounce_index(conn, job_id)
    bounce_frames = [b[0] for b in bounce_rows]

    # Windows / thresholds mirror the bounce-driven path.
    HIT_WINDOW_FRAMES = max(1, int(round(fps * 0.20)))
    HIT_SOFT_WINDOW_FRAMES = max(1, int(round(fps * 1.20)))
    KP_WINDOW_FRAMES = max(1, int(round(fps * 1.20)))
    # Ball struck at predicted_hit_frame → crosses net → bounces on opponent's
    # side. The matching bounce is the first one in (hit, hit + ~1s].
    BOUNCE_AFTER_FRAMES = max(1, int(round(fps * 1.0)))

    logger.info(
        "T5 Pass 1 (stroke-driven): %d stroke events, %d court bounces, job=%s at %.1f fps",
        len(strokes), len(bounce_rows), job_id, fps,
    )

    diag = {
        "strokes": len(strokes), "bounce_matched": 0, "no_bounce": 0,
        "side_from_bounce": 0, "side_from_attribution": 0,
        "unresolved": 0, "kp_patched": 0, "sc_patched": 0, "fired_serve": 0,
        "ab_identity": 0,
    }

    # ---- A/B identity (ADR-03): map each hit's SIDE -> the stable person (A/B)
    # so player_id survives changeovers (matches SA's person-based id). Silver
    # Pass-1 runs before Pass-3 (no game_number yet), so re-derive the game windows
    # from serve_events and join the persisted per-game side->A/B segments. VERBATIM
    # projection of the identity model; falls back to side-based top_pids[0/1] when
    # there's no/low-confidence segment (never worse than the prior behaviour).
    ident_game_starts: List[float] = []
    ident_game_anear: List[Optional[bool]] = []   # is player A on the NEAR side that game?
    ident_game_conf: List[float] = []
    try:
        from ml_pipeline.identity_detector.game_boundaries import derive_game_boundaries
        _sev = conn.execute(sql_text("""
            SELECT ts, player_id FROM ml_analysis.serve_events
            WHERE task_id::text = :tid ORDER BY ts
        """), {"tid": task_id}).fetchall()
        _bounds = (derive_game_boundaries(
            [{"ts": float(t), "player_id": int(p)} for t, p in _sev]) if _sev else [])
        _seg: Dict[int, Tuple[bool, float]] = {}
        _seg_present = conn.execute(sql_text("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='ml_analysis' AND table_name='player_identity_segments' LIMIT 1
        """)).scalar()
        if _seg_present:
            for gn, a_side, conf in conn.execute(sql_text("""
                SELECT game_number, player_a_side, confidence
                FROM ml_analysis.player_identity_segments WHERE job_id::text = :tid
            """), {"tid": task_id}).fetchall():
                _seg[int(gn)] = (a_side == "near", float(conf) if conf is not None else 0.0)
        for gb in _bounds:
            a = _seg.get(gb.game_number)
            ident_game_starts.append(gb.t_start)
            ident_game_anear.append(a[0] if a else None)
            ident_game_conf.append(a[1] if a else 0.0)
    except Exception as _e:
        logger.warning("T5 Pass 1: identity A/B mapping unavailable (%s) — side-based player_id", _e)
    _ident_on = len(ident_game_starts) > 0
    _ident = (ident_game_starts, ident_game_anear, ident_game_conf) if _ident_on else None
    if _ident_on:
        logger.info("T5 Pass 1: A/B identity active — %d game windows", len(ident_game_starts))

    rows_to_insert: List[dict] = []
    for i, (hf, raw_stroke_pid, _conf, bhx, bhy, bsn, s_volley) in enumerate(strokes):
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

        # ---- hit SIDE + LOCATION: VERBATIM from bronze stroke_events (rule
        # #1/#2). The model (stroke_detector.hit_location) owns the hit fact now;
        # silver only projects it. The side-resolution + nearest-detection +
        # mirror reconstruction that lived here was DELETED 2026-06-15.
        # Transition shim: pre-867119f rows have NULL bronze side -> fall back to
        # bounce-opposite (the identical signal). Remove once tasks are re-ingested.
        hitter_side_near = bsn
        if hitter_side_near is None and matched_bounce is not None:
            hitter_side_near = (b_cy < HALF_Y)
        if hitter_side_near is None:
            diag["unresolved"] += 1
            continue
        diag["side_from_bounce" if matched_bounce is not None else "side_from_attribution"] += 1

        hit_x = bhx
        hit_y = bhy

        # swing_type — VERBATIM bronze stroke_class via the nearest same-side
        # classified carrier within KP_WINDOW. The classifier labels the contact
        # detection; sparse pose / fps rounding means silver's hit frame is a
        # neighbour, so adopt the nearest same-side model class. Projection of the
        # bronze fact (rule #1/#2), not silver inference.
        sc_frames, sc_dets = (
            (near_sc_frames, near_sc_dets) if hitter_side_near
            else (far_sc_frames, far_sc_dets)
        )
        sc_match = _find_nearest_detection(sc_frames, sc_dets, hf, KP_WINDOW_FRAMES)
        flow_class = sc_match.get("stroke_class") if sc_match is not None else None
        if flow_class is not None:
            diag["sc_patched"] += 1

        ball_player_dist = None
        if b_cx is not None and hit_x is not None and hit_y is not None:
            ball_player_dist = math.hypot(b_cx - hit_x, b_cy - hit_y)

        # ---- SERVE: pure bronze import (2026-06-07) — this path emits
        # serve=False like the bounce-driven path; the unconditional
        # _apply_serve_events_overlay sets serves from bronze serve_events.
        # The geometric gates that lived here were deleted with the legacy
        # path. (This whole function is dormant behind T5_STROKE_DRIVEN_SILVER
        # and gets rewritten at the B3 stroke flip.) ----
        is_serve = False

        # ---- swing type — project the bronze classifier verbatim (flow_class
        # read above from the nearest same-side stroke_class carrier) ----
        if not is_serve and flow_class in ("fh", "bh", "overhead", "other"):
            swing_type = flow_class  # bronze model owns this fact (projected verbatim)
        else:
            # No model answer (weights absent / disabled / serve) -> 'other'.
            # Silver does NO swing inference (ADR-02 revision 2026-06-14; rule #1/#2;
            # the pose/position heuristics were deleted).
            swing_type = "other"

        # volley — VERBATIM from bronze stroke_events.volley (the model owns it:
        # no ball bounce since the previous hit). The silver net-distance heuristic
        # was DELETED 2026-06-15. NULL bronze (pre-volley rows) -> False.
        is_volley = bool(s_volley)
        # STOPGAP-until-identity-model: WHO is derived from court SIDE (bounce-
        # opposite, or attribution fallback) — NOT a bronze identity fact. The
        # bronze stroke_events.player_id is perspective-biased so it is NOT used
        # (rule #11). Flips to verbatim once the identity model is wired.
        # player_id — stable A/B (person), not side: survives changeovers. Same
        # helper the serve overlay uses, so serve + rally rows share one id basis.
        hitter_pid = _ab_pid(hitter_side_near, ts, top_pids, _ident)
        if _ident is not None:
            diag["ab_identity"] += 1

        # Pass-1 row = VERBATIM bronze projection except where STOPGAP-tagged.
        #   VERBATIM: serve (serve_events), swing_type (player_detections.stroke_class),
        #     ball_speed + court_x/court_y (ball_bounces), ball_hit_s/timestamp
        #     (stroke_events.predicted_hit_frame -> sec).
        #   STOPGAP (model gap, see tags above + the audit doc): player_id (identity),
        #     volley (volley model), and ball_hit_location_x/y below — RECONSTRUCTED
        #     because stroke_events carries no hit location yet (the keystone bronze
        #     enrichment, audit §B). hit_x/hit_y come from the resolved hitter's
        #     player_detections court pos (+ mirror fallback), not a bronze hit fact.
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
    # Serve overlay — sets serve=True on the rows bronze serve_events claims.
    # MUST run for the stroke-driven path too (mirrors the bounce-driven path,
    # ~line 827). Without it silver has ZERO serves, so the shared passes 3-5
    # derive no point/game/serve structure (serves_d=0, points=0, games=0).
    # Regression introduced when stroke-driven flipped default-ON (472b244) —
    # the call lived only in the bounce-driven path; fixed 2026-06-14 after it
    # surfaced on the first real re-run task (93ebb93d).
    _apply_serve_events_overlay(conn, task_id, rows_to_insert, top_pids, ident_lookup=_ident)
    return _insert_pass1_rows(conn, rows_to_insert)


# ============================================================
# T5 PASS 1 dispatcher
# ============================================================

def _stroke_driven_enabled() -> bool:
    """Read the T5_STROKE_DRIVEN_SILVER gate at call time (so flipping the env
    var + restart applies without a code change / Docker rebuild — same
    rollback pattern as the WASB swap, memory feedback_env_var_rollback_pattern).

    DEFAULT FLIPPED ON 2026-06-14 (Tomo): silver is now HIT-DRIVEN by default.
    The architecture decision is settled (one row per stroke event = one shot;
    bounce is an attribute — north_star §"SILVER ROW ARCHITECTURE"). T5 silver is
    not consumed by prod, so the old "wait until bronze is right" hold (rule #11)
    no longer blocks the flip — accuracy fills in at training, the architecture
    is correct now. Set T5_STROKE_DRIVEN_SILVER=0 to roll back to bounce-driven.
    Swing_type is now projected VERBATIM from the bronze classifier
    (`stroke_class`, 4-class incl. `other`); the silver pose/position heuristics
    were DELETED 2026-06-14 (ADR-02 revision) — no swing inference here at all.
    Two documented residuals remain (NOT swing heuristics): (1) the `volley`
    flag is still a net-distance stopgap (VOLLEY_NET_DISTANCE_M) until the volley
    fact lands (derive + validate vs SA player_swing.volley); (2) bounce coords
    still read is_bounce (the bounce MODEL ball_bounces is empty on existing
    tasks — swaps in once it's carried through re-ingest + accrued from uploads).
    """
    return os.getenv("T5_STROKE_DRIVEN_SILVER", "1").strip().lower() in ("1", "true", "yes", "on")


def _t5_pass1_load(conn: Connection, task_id: str, job_id: str, fps: float) -> int:
    """Pick the Pass-1 row-generation strategy.

    STROKE-DRIVEN IS THE LIVE PROD PATH (T5_STROKE_DRIVEN_SILVER defaults ON,
    flipped 2026-06-14, `472b244`). One silver row per bronze stroke_events hit;
    hit location/side/swing/volley are projected VERBATIM from the model (the
    `hit_location` assembly moved to stroke_detector, 2026-06-15). The
    BOUNCE-DRIVEN path below is the dormant fallback, reached only when the flag
    is OFF or a task has no stroke_events (pre-Phase-6 ingests / failed stroke
    detection). It is a retirement candidate once stroke-driven is proven on real
    uploads — see docs/_investigation/t5_cleanup_inventory.md Tier 2 #1.

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


# _check_hitter_stationary_pre_hit DELETED 2026-06-07 with the legacy
# in-silver serve path (pure bronze import — see header of this section).


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
