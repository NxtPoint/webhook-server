"""
ml_pipeline/build_silver_practice.py — Silver builder for serve & rally practice data.

Reads from ml_analysis.ball_detections + player_detections (T5 bronze),
writes to silver.practice_detail.

3-pass approach:
  1. Extract bounces + nearest player positions → insert rows
  2. Sequence detection (serve numbering or rally grouping)
  3. Analytics (zones, depth, serve result)

Usage:
    from ml_pipeline.build_silver_practice import build_silver_practice
    result = build_silver_practice(task_id="...", replace=True)
"""

import logging
from typing import Dict

from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Court geometry (ITF standard, same constants as build_silver_v2)
# ---------------------------------------------------------------------------
COURT_LENGTH_M = 23.77
COURT_WIDTH_SINGLES_M = 8.23
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = COURT_LENGTH_M / 2        # 11.885m — net position
SERVICE_LINE_M = 6.40              # net → service line
FAR_SERVICE_LINE_M = COURT_LENGTH_M - SERVICE_LINE_M  # 17.37m
SINGLES_LEFT_X = (COURT_WIDTH_DOUBLES_M - COURT_WIDTH_SINGLES_M) / 2  # 1.37m
SINGLES_RIGHT_X = SINGLES_LEFT_X + COURT_WIDTH_SINGLES_M              # 9.60m
CENTRE_X = COURT_WIDTH_DOUBLES_M / 2  # 5.485m

# Rally gap threshold (frames) — bounces separated by more than this start a new rally
RALLY_GAP_FRAMES = 25  # matches BOUNCE_MIN_DIRECTION_CHANGE in config.py


def build_silver_practice(task_id: str, replace: bool = False,
                          engine=None) -> Dict:
    """
    Build silver.practice_detail from ml_analysis.* tables.

    Args:
        task_id: job_id from ml_analysis.video_analysis_jobs
        replace: if True, delete existing rows before rebuilding
        engine: SQLAlchemy engine (auto-resolved if None)

    Returns:
        dict with pass row counts and metadata
    """
    if engine is None:
        from ml_pipeline.db_schema import _get_engine
        engine = _get_engine()

    # Ensure silver schema + table exist
    from ml_pipeline.db_schema import ml_analysis_init
    ml_analysis_init(engine)

    with engine.begin() as conn:
        # Resolve practice type from the job row
        job_row = conn.execute(sql_text("""
            SELECT j.job_id, j.video_fps, sc.sport_type
            FROM ml_analysis.video_analysis_jobs j
            LEFT JOIN bronze.submission_context sc ON sc.task_id = j.job_id
            WHERE j.job_id = :tid OR j.task_id = :tid
            LIMIT 1
        """), {"tid": task_id}).mappings().first()

        if not job_row:
            logger.warning("No job found for task_id=%s", task_id)
            return {"ok": False, "error": "job not found"}

        practice_type = job_row.get("sport_type") or "serve_practice"
        job_id = job_row["job_id"]

        # frame_idx in ball_detections is in the ML pipeline's sampled frame
        # space (e.g. 10fps for practice), NOT the video's native fps (e.g. 30fps).
        # Derive the effective sampling fps from total_frames / duration.
        vid_row = conn.execute(sql_text("""
            SELECT total_frames, video_duration_sec, video_fps
            FROM ml_analysis.video_analysis_jobs WHERE job_id = :jid
        """), {"jid": job_id}).mappings().first()

        if vid_row and vid_row["total_frames"] and vid_row["video_duration_sec"]:
            fps = vid_row["total_frames"] / vid_row["video_duration_sec"]
        else:
            fps = job_row.get("video_fps") or 25.0

        logger.info("Building practice silver: task_id=%s type=%s fps=%s",
                     task_id, practice_type, fps)

        # Clean slate if requested
        if replace:
            conn.execute(sql_text(
                "DELETE FROM silver.practice_detail WHERE task_id = :tid"
            ), {"tid": task_id})

        # Pass 1: Extract bounces
        p1 = _pass1_extract_bounces(conn, task_id, job_id, practice_type, fps)
        logger.info("Pass 1 (extract bounces): %d rows", p1)

        # Pass 2: Sequence detection
        if practice_type == "serve_practice":
            p2 = _pass2_serve_sequences(conn, task_id)
        else:
            p2 = _pass2_rally_sequences(conn, task_id)
        logger.info("Pass 2 (sequencing): %d rows updated", p2)

        # Pass 3: Analytics
        p3 = _pass3_analytics(conn, task_id, practice_type)
        logger.info("Pass 3 (analytics): %d rows updated", p3)

    result = {
        "ok": True,
        "task_id": task_id,
        "practice_type": practice_type,
        "pass1_bounces": p1,
        "pass2_sequences": p2,
        "pass3_analytics": p3,
    }
    logger.info("Practice silver complete: %s", result)
    return result


def _pass1_extract_bounces(conn, task_id, job_id, practice_type, fps):
    """
    Extract bounce detections from ml_analysis.ball_detections,
    join nearest player position, insert into silver.practice_detail.

    Uses court_x/court_y when available from the ML pipeline.
    Falls back to pixel-to-court estimation when court coords are missing
    (known issue: ML pipeline may not propagate court coords despite
    successful court detection).
    """
    # Check if bronze has court coords — if not, estimate from pixels
    has_coords = conn.execute(sql_text("""
        SELECT count(*) FROM ml_analysis.ball_detections
        WHERE job_id = :job_id AND is_bounce = TRUE AND court_x IS NOT NULL
    """), {"job_id": job_id}).scalar()

    if has_coords > 0:
        # Bronze has court coords — use them directly
        ball_x_expr = "b.court_x"
        ball_y_expr = "b.court_y"
        speed_expr  = "b.speed_kmh"
        coord_filter = "AND b.court_x IS NOT NULL AND b.court_y IS NOT NULL"
        player_x_expr = "p.court_x"
        player_y_expr = "p.court_y"
        logger.info("Pass 1: using bronze court coords (%d bounces with coords)", has_coords)
    else:
        # Fallback: estimate court coords from pixel positions
        # Get video dimensions for mapping
        vid = conn.execute(sql_text("""
            SELECT video_width, video_height
            FROM ml_analysis.video_analysis_jobs WHERE job_id = :job_id
        """), {"job_id": job_id}).mappings().first()
        vw = float((vid or {}).get("video_width") or 1280)
        vh = float((vid or {}).get("video_height") or 720)
        logger.warning(
            "Pass 1: NO court coords in bronze — estimating from pixels (%dx%d)",
            int(vw), int(vh),
        )
        # Map pixel x → court width (doubles width for full frame)
        # Map pixel y → court length (flipped: top of frame = far baseline)
        ball_x_expr = f"(b.x / {vw}) * {COURT_WIDTH_DOUBLES_M}"
        ball_y_expr = f"(b.y / {vh}) * {COURT_LENGTH_M}"
        speed_expr  = "NULL"  # can't compute speed without real court coords
        coord_filter = ""     # accept all bounces
        player_x_expr = f"(p.center_x / {vw}) * {COURT_WIDTH_DOUBLES_M}"
        player_y_expr = f"(p.center_y / {vh}) * {COURT_LENGTH_M}"

    result = conn.execute(sql_text(f"""
        INSERT INTO silver.practice_detail
            (task_id, practice_type, frame_idx, timestamp_s,
             ball_x, ball_y, ball_speed_kmh, is_bounce, is_in,
             player_id, player_court_x, player_court_y,
             sequence_num, shot_ix)
        SELECT
            :task_id,
            :practice_type,
            b.frame_idx,
            b.frame_idx / :fps,
            {ball_x_expr},
            {ball_y_expr},
            {speed_expr},
            TRUE,
            b.is_in,
            p.player_id,
            {player_x_expr},
            {player_y_expr},
            0,
            0
        FROM ml_analysis.ball_detections b
        LEFT JOIN LATERAL (
            SELECT pd.player_id, pd.court_x, pd.court_y, pd.center_x, pd.center_y
            FROM ml_analysis.player_detections pd
            WHERE pd.job_id = :job_id
            ORDER BY ABS(pd.frame_idx - b.frame_idx), pd.player_id
            LIMIT 1
        ) p ON TRUE
        WHERE b.job_id = :job_id
          AND b.is_bounce = TRUE
          {coord_filter}
        ORDER BY b.frame_idx
    """), {
        "task_id": task_id,
        "job_id": job_id,
        "practice_type": practice_type,
        "fps": fps,
    })
    return result.rowcount


def _pass2_serve_sequences(conn, task_id):
    """
    Number serves sequentially. Each bounce after a gap > RALLY_GAP_FRAMES
    from the previous bounce starts a new serve. Alternate deuce/ad side.
    """
    # Get bounces ordered by frame
    rows = conn.execute(sql_text("""
        SELECT id, frame_idx
        FROM silver.practice_detail
        WHERE task_id = :tid
        ORDER BY frame_idx
    """), {"tid": task_id}).fetchall()

    if not rows:
        return 0

    serve_num = 1
    prev_frame = rows[0][1]
    updates = []

    for row_id, frame_idx in rows:
        if frame_idx - prev_frame > RALLY_GAP_FRAMES and row_id != rows[0][0]:
            serve_num += 1
        side = "deuce" if serve_num % 2 == 1 else "ad"
        updates.append({"rid": row_id, "seq": serve_num, "side": side})
        prev_frame = frame_idx

    # Batch update
    for u in updates:
        conn.execute(sql_text("""
            UPDATE silver.practice_detail
            SET sequence_num = :seq, shot_ix = 1, serve_side = :side
            WHERE id = :rid
        """), u)

    return len(updates)


def _pass2_rally_sequences(conn, task_id):
    """
    Group bounces into rallies. A gap > RALLY_GAP_FRAMES starts a new rally.
    Number rallies (sequence_num) and shots within rallies (shot_ix).
    """
    rows = conn.execute(sql_text("""
        SELECT id, frame_idx
        FROM silver.practice_detail
        WHERE task_id = :tid
        ORDER BY frame_idx
    """), {"tid": task_id}).fetchall()

    if not rows:
        return 0

    rally_num = 1
    shot_in_rally = 1
    prev_frame = rows[0][1]
    updates = []

    for row_id, frame_idx in rows:
        if frame_idx - prev_frame > RALLY_GAP_FRAMES and row_id != rows[0][0]:
            rally_num += 1
            shot_in_rally = 1
        updates.append({"rid": row_id, "seq": rally_num, "shot": shot_in_rally})
        shot_in_rally += 1
        prev_frame = frame_idx

    for u in updates:
        conn.execute(sql_text("""
            UPDATE silver.practice_detail
            SET sequence_num = :seq, shot_ix = :shot
            WHERE id = :rid
        """), u)

    return len(updates)


def _pass3_analytics(conn, task_id, practice_type):
    """
    Compute derived analytics: placement zone, depth, serve zone/result,
    rally length/duration.
    """
    updated = 0

    # Placement zone (A/B/C/D based on court quadrant)
    updated += conn.execute(sql_text("""
        UPDATE silver.practice_detail
        SET placement_zone = CASE
            WHEN ball_x < :cx AND ball_y < :hy THEN 'A'
            WHEN ball_x >= :cx AND ball_y < :hy THEN 'B'
            WHEN ball_x < :cx AND ball_y >= :hy THEN 'C'
            WHEN ball_x >= :cx AND ball_y >= :hy THEN 'D'
        END
        WHERE task_id = :tid AND ball_x IS NOT NULL AND ball_y IS NOT NULL
    """), {"tid": task_id, "cx": CENTRE_X, "hy": HALF_Y}).rowcount

    # Depth classification (from bounce Y relative to nearest baseline)
    # LEAST(ball_y, court_length - ball_y) = distance from nearest baseline
    # Deep = within ~3m of baseline, Middle = 3–6.4m, Short = beyond service line
    updated += conn.execute(sql_text("""
        UPDATE silver.practice_detail
        SET depth_d = CASE
            WHEN LEAST(ball_y, :cl - ball_y) < :deep THEN 'Deep'
            WHEN LEAST(ball_y, :cl - ball_y) < :mid  THEN 'Middle'
            ELSE 'Short'
        END
        WHERE task_id = :tid AND ball_y IS NOT NULL
    """), {"tid": task_id, "cl": COURT_LENGTH_M,
           "deep": 3.0, "mid": SERVICE_LINE_M}).rowcount

    if practice_type == "serve_practice":
        # Serve zone: Wide / Body / T (based on bounce X relative to service box)
        updated += conn.execute(sql_text("""
            UPDATE silver.practice_detail
            SET serve_zone = CASE
                WHEN ball_x < :sl + (:sw * 0.33) THEN 'wide'
                WHEN ball_x > :sr - (:sw * 0.33) THEN 'wide'
                WHEN ABS(ball_x - :cx) < (:sw * 0.15) THEN 'T'
                ELSE 'body'
            END
            WHERE task_id = :tid AND ball_x IS NOT NULL
        """), {
            "tid": task_id,
            "sl": SINGLES_LEFT_X,
            "sr": SINGLES_RIGHT_X,
            "sw": COURT_WIDTH_SINGLES_M,
            "cx": CENTRE_X,
        }).rowcount

        # Serve result: In / Fault based on is_in
        updated += conn.execute(sql_text("""
            UPDATE silver.practice_detail
            SET serve_result = CASE
                WHEN is_in = TRUE THEN 'in'
                WHEN is_in = FALSE THEN 'fault'
                ELSE NULL
            END
            WHERE task_id = :tid
        """), {"tid": task_id}).rowcount

    else:
        # Rally: compute rally_length and rally_duration_s per rally
        updated += conn.execute(sql_text("""
            UPDATE silver.practice_detail pd
            SET rally_length = sub.cnt,
                rally_duration_s = sub.dur
            FROM (
                SELECT sequence_num,
                       COUNT(*) AS cnt,
                       MAX(timestamp_s) - MIN(timestamp_s) AS dur
                FROM silver.practice_detail
                WHERE task_id = :tid
                GROUP BY sequence_num
            ) sub
            WHERE pd.task_id = :tid
              AND pd.sequence_num = sub.sequence_num
        """), {"tid": task_id}).rowcount

    # Stroke inference (forehand / backhand) from ball position vs player position
    # Uses dominant_hand from billing.member (default: right)
    updated += _pass3_stroke_inference(conn, task_id)

    return updated


def _pass3_stroke_inference(conn, task_id):
    """
    Infer stroke type from ball-x vs player-x, adjusted for court end and handedness.

    Near-side player (court_y < half): facing net (toward higher y).
      Right-arm side = lower x (bird's eye view).
    Far-side player (court_y >= half): facing net (toward lower y).
      Right-arm side = higher x (bird's eye view).

    Right-hander: ball on right-arm side → forehand, other → backhand.
    Left-hander: flipped.
    """
    # Look up dominant hand via submission_context email → billing.member
    hand_row = conn.execute(sql_text("""
        SELECT COALESCE(m.dominant_hand, 'right') AS hand
        FROM bronze.submission_context sc
        LEFT JOIN billing.member m
            ON lower(m.email) = lower(sc.email) AND m.is_primary = true
        WHERE sc.task_id = :tid
        LIMIT 1
    """), {"tid": task_id}).fetchone()
    is_left = (hand_row[0] if hand_row else "right") == "left"

    # For a right-hander on the near side:
    #   ball_x < player_court_x  →  ball is on their right-arm side  →  forehand
    # For a right-hander on the far side:
    #   ball_x > player_court_x  →  ball is on their right-arm side  →  forehand
    # Left-hander: swap forehand/backhand
    fh = "forehand"
    bh = "backhand"
    if is_left:
        fh, bh = bh, fh

    return conn.execute(sql_text("""
        UPDATE silver.practice_detail
        SET stroke_d = CASE
            WHEN player_court_y < :hy THEN
                CASE WHEN ball_x < player_court_x THEN :fh ELSE :bh END
            ELSE
                CASE WHEN ball_x > player_court_x THEN :fh ELSE :bh END
        END
        WHERE task_id = :tid
          AND ball_x IS NOT NULL
          AND player_court_x IS NOT NULL
    """), {"tid": task_id, "hy": HALF_Y, "fh": fh, "bh": bh}).rowcount
