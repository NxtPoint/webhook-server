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
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

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
EPS_BASELINE_M = 0.30

# Thresholds for match analysis
SERVE_GAP_S = 3.0       # seconds gap before a bounce to consider it a serve (was 5.0 — too strict for sparse bounce data)
VOLLEY_NET_DISTANCE_M = 4.0  # player within 4m of net = volley
SERVE_BOX_TOLERANCE_M = 1.5  # extra tolerance for service box check (real wide serves bounce in doubles alley)

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


def _infer_swing_type_from_keypoints(
    keypoints: Optional[list],
    center_x: Optional[float],
    is_serve: bool,
    is_left_handed: bool,
) -> str:
    """Infer swing type from COCO pose keypoints.

    Returns one of: 'overhead', 'fh', 'bh', 'other'
    """
    if is_serve:
        return "overhead"

    if keypoints is None or center_x is None:
        return "other"

    # Parse keypoints if string
    if isinstance(keypoints, str):
        try:
            keypoints = json.loads(keypoints)
        except (json.JSONDecodeError, TypeError):
            return "other"

    # COCO: right_wrist=10, left_wrist=9
    wrist_idx = 9 if is_left_handed else 10
    if len(keypoints) <= wrist_idx:
        return "other"

    wrist_x, _, wrist_conf = keypoints[wrist_idx]
    if wrist_conf < 0.3:
        return "other"

    # Wrist on dominant side of center = forehand
    if is_left_handed:
        return "fh" if wrist_x < center_x else "bh"
    else:
        return "fh" if wrist_x > center_x else "bh"


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
# T5 PASS 1: Extract bounces → 18 base fields
# ============================================================

def _t5_pass1_load(conn: Connection, task_id: str, job_id: str, fps: float) -> int:
    """
    Transform T5 ml_analysis.* bounce data into silver.point_detail base fields.

    For each bounce detection:
      1. Determine which player hit the ball (ball direction: hitter is on opposite
         side of net from where the ball bounced)
      2. Find the nearest player detection on the hitting side
      3. Infer serve, swing_type, volley from context
      4. INSERT the 18 base fields + model='t5'
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

    # ---- Step 2: Fetch all player detections (for nearest-player lookup) ----
    player_dets = conn.execute(sql_text("""
        SELECT frame_idx, player_id, court_x, court_y, center_x, center_y, keypoints
        FROM ml_analysis.player_detections
        WHERE job_id = :jid
        ORDER BY frame_idx
    """), {"jid": job_id}).fetchall()

    # ---- Step 3: Identify the two players ----
    # Group by player_id, use the top 2 by detection count
    from collections import Counter
    pid_counts = Counter(p[1] for p in player_dets)
    top_pids = [pid for pid, _ in pid_counts.most_common(2)]
    if len(top_pids) < 2:
        logger.warning("T5 Pass 1: only %d player(s) detected — assigning alternating IDs", len(top_pids))
        if not top_pids:
            top_pids = [0, 1]
        elif len(top_pids) == 1:
            top_pids.append(top_pids[0] + 1)

    # Map any ghost player IDs to the top 2
    pid_map = {}
    for pid in pid_counts:
        if pid == top_pids[0]:
            pid_map[pid] = str(top_pids[0])
        else:
            pid_map[pid] = str(top_pids[1])

    # Build player detection index for fast nearest-frame lookup.
    # Three lists:
    #   near_dets: players with court_y > HALF_Y AND valid coords
    #   far_dets:  players with court_y < HALF_Y AND valid coords
    #   any_with_coords: ALL players with valid coords (used as ultimate fallback
    #                    when side-specific lookup returns nothing usable)
    near_dets = []
    far_dets = []
    any_with_coords = []
    for pd in player_dets:
        frame_idx, pid, cx, cy, centerx, centery, kps = pd
        mapped_pid = pid_map.get(pid, str(pid))
        entry = {
            "frame_idx": frame_idx,
            "player_id": mapped_pid,
            "court_x": cx, "court_y": cy,
            "center_x": centerx, "center_y": centery,
            "keypoints": kps,
        }
        if cy is not None and cx is not None:
            any_with_coords.append(entry)
            if cy > HALF_Y:
                near_dets.append(entry)
            else:
                far_dets.append(entry)
        # Note: entries with NULL court coords are NOT useful for hit_x/y
        # so we don't add them to any list. The fallback uses any_with_coords.

    # Pre-build frame indices for binary search
    near_frames, near_dets = _build_detection_index(near_dets)
    far_frames, far_dets = _build_detection_index(far_dets)
    any_frames, any_dets = _build_detection_index(any_with_coords)

    logger.info(
        "T5 Pass 1: player buckets — near=%d far=%d any_with_coords=%d (of %d total)",
        len(near_dets), len(far_dets), len(any_dets), len(player_dets),
    )

    # ---- Step 4: Look up dominant hand ----
    hand_row = conn.execute(sql_text("""
        SELECT COALESCE(m.dominant_hand, 'right') AS hand
        FROM bronze.submission_context sc
        LEFT JOIN billing.member m
            ON lower(m.email) = lower(sc.email) AND m.is_primary = true
        WHERE sc.task_id = :tid
        LIMIT 1
    """), {"tid": task_id}).fetchone()
    is_left_handed = (hand_row[0] if hand_row else "right") == "left"

    # ---- Step 5: Process each bounce → build row ----
    rows_to_insert = []
    prev_ts = -999.0

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

        # Find nearest player detection on the hitter's side
        hitter = _find_nearest_detection(h_frames, h_dets, frame_idx)

        # Fallback: if no player on the hitter's side, use ANY player with
        # valid coords and mirror them to the hitter's side. This handles the
        # case where ML only tracks one player (always on one side).
        if hitter is None and any_dets:
            other = _find_nearest_detection(any_frames, any_dets, frame_idx)
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

        # Serve detection: gap > threshold AND bounce in service box
        is_serve = False
        if (gap_s > SERVE_GAP_S or i == 0) and _is_in_service_box(cx, cy):
            is_serve = True

        # Swing type inference
        swing_type = "other"
        if hitter:
            swing_type = _infer_swing_type_from_keypoints(
                hitter.get("keypoints"), hitter.get("center_x"),
                is_serve, is_left_handed,
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

        # Ball-player distance
        ball_player_dist = None
        if hitter and hitter.get("court_x") is not None and hitter.get("court_y") is not None:
            ball_player_dist = math.hypot(
                cx - hitter["court_x"],
                cy - hitter["court_y"],
            )

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
            "ball_speed": (speed_kmh / 3.6) if speed_kmh else None,  # km/h → m/s
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

    if not rows_to_insert:
        logger.warning("T5 Pass 1: no valid rows to insert")
        return 0

    # ---- Step 6: Bulk INSERT ----
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


def _build_detection_index(dets: List[dict]) -> Tuple[List[int], List[dict]]:
    """Pre-compute sorted frame index for binary search."""
    return [d["frame_idx"] for d in dets], dets


def _find_nearest_detection(frames: List[int], dets: List[dict],
                            target_frame: int) -> Optional[dict]:
    """Find the player detection closest in frame_idx to the target (binary search)."""
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
