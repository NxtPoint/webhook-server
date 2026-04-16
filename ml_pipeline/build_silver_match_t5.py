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
    floats) OR as a nested list [[x,y,c], ...] (17 entries). Either form is
    accepted; missing/malformed input returns None.
    """
    if keypoints is None:
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
    # Backhand: dominant wrist crosses to the non-dominant side of the body.
    # The clearest signal is: wrist x is past the OFF-side shoulder.
    # Forehand: dominant wrist is on the dominant side of the DOMINANT shoulder.
    #
    # We also accept a weaker signal using center_x when shoulder coords are
    # unavailable — wrist on the dominant side of body centre = forehand.
    if have_dom_wrist:
        # Strong signal: compare wrist to both shoulder anchors
        if have_dom_shoulder and have_off_shoulder:
            if is_left_handed:
                # Left-handed: dominant wrist crosses right of right shoulder → BH
                crossed_body = dom_wrist_x > off_shoulder_x
            else:
                # Right-handed: dominant wrist crosses left of left shoulder → BH
                crossed_body = dom_wrist_x < off_shoulder_x
            return "bh" if crossed_body else "fh"

        # Medium signal: compare to dominant shoulder only
        if have_dom_shoulder:
            if is_left_handed:
                # Left wrist extends left of left shoulder → FH
                return "fh" if dom_wrist_x < dom_shoulder_x else "bh"
            else:
                # Right wrist extends right of right shoulder → FH
                return "fh" if dom_wrist_x > dom_shoulder_x else "bh"

        # Weak signal: wrist vs body centre_x (original fallback)
        if center_x is not None:
            if is_left_handed:
                return "fh" if dom_wrist_x < center_x else "bh"
            else:
                return "fh" if dom_wrist_x > center_x else "bh"

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
        SELECT frame_idx, player_id, court_x, court_y, center_x, center_y, keypoints, stroke_class
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

    # Diagnostic counters — why serves are accepted/rejected
    serve_diag = {
        "total_bounces": 0,
        "no_hitter": 0,
        "no_hitter_stale_only": 0,  # detections exist but none within window
        "geometric_pass": 0,
        "geometric_fail_no_dist": 0,   # ball_player_distance > 1.5
        "geometric_fail_hitter_y": 0,  # hitter not past baseline
        "geometric_fail_bounce_x": 0,  # bounce_x out of range
        "geometric_fail_wrong_side": 0, # bounce on same side as hitter
        "pose_pass": 0,
        "pose_no_keypoints": 0,
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
        # The ±window rejects stale detections; sparse-side coverage yields
        # hitter=None rather than a recycled position from seconds earlier.
        hit_frame_est = max(0, frame_idx - HIT_BEFORE_BOUNCE_FRAMES)
        hitter = _find_nearest_detection(
            h_frames, h_dets, hit_frame_est,
            max_distance_frames=HIT_WINDOW_FRAMES,
        )
        if hitter is None and h_dets:
            serve_diag["no_hitter_stale_only"] += 1

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
        serve_diag["total_bounces"] += 1
        is_serve = False

        if hitter is None:
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

        # Per-candidate trace: log every bounce that passes the geometric gate
        # so we can see exactly where serves are being accepted/rejected.
        if geom_ok and logger.isEnabledFor(logging.INFO):
            hy = hitter.get("court_y") if hitter else None
            logger.info(
                "T5 serve cand frame=%d ts=%.2f hy=%.2f bx=%.2f by=%.2f dist=%s overhead=%s cooldown_ok=%s (since_last=%.1fs)",
                frame_idx, ts,
                hy if hy is not None else float("nan"),
                cx, cy,
                f"{ball_player_dist:.2f}" if ball_player_dist is not None else "None",
                is_overhead, cooldown_ok, ts_since_last_serve,
            )

        # Primary trigger: geometric + cooldown.
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
        if geom_ok and cooldown_ok:
            is_serve = True
            serve_diag["fired_primary"] += 1
            if is_overhead:
                # Bonus signal — track for diagnostics, doesn't change outcome
                serve_diag["fired_secondary"] += 1
        elif geom_ok and not cooldown_ok:
            serve_diag["cooldown_block"] += 1

        if is_serve:
            last_serve_ts = ts

        # Swing type inference — three-tier cascade:
        # 1. Pose keypoints (near player, 200-400px, high confidence)
        # 2. Optical flow classifier (far player, stored in stroke_class)
        # 3. Position-based fallback (ball vs player side → fh/bh)
        swing_type = "other"
        if hitter:
            swing_type = _infer_swing_type_from_keypoints(
                hitter.get("keypoints"), hitter.get("center_x"),
                is_serve, is_left_handed,
                court_y=hitter.get("court_y"),
            )
            if swing_type == "other" and not is_serve:
                # Try optical flow classification (far player)
                flow_class = hitter.get("stroke_class")
                if flow_class and flow_class != "other":
                    swing_type = flow_class
                else:
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

    # Log serve detection diagnostics — shows where the filter is rejecting
    logger.info("T5 serve diagnostics: %s", serve_diag)

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
