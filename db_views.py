# db_views.py
from sqlalchemy import text
from typing import List

# Symbol must exist for imports and /ops/init-views
VIEW_SQL_STMTS: List[str] = []  # will be populated from VIEW_NAMES/CREATE_STMTS

# Views required for the transaction log + minimal helpers
# NOTE: Order matters. Dependencies must come after their sources.
VIEW_NAMES = [
    # Base helpers
    "vw_swing",
    "vw_swing_norm",           # depends on vw_swing
    "vw_rally",
    "vw_bounce",
    "vw_player_position",

    # XY normalization (new)
    "vw_ball_position_norm",   # depends on dim_rally + fact_ball_position
    "vw_player_position_norm", # depends on vw_ball_position_norm

    "vw_player_swing_dist",    # per-player distribution (kept for dashboards)

    # Ordering helpers
    "vw_shot_order",           # legacy (rally-only)
    "vw_shot_order_norm",      # rally OR serve-based inferred points

    # Point-level summary (winner/error)
    "vw_point_summary",

    # Shot-level transaction log
    "vw_point_log",
]

CREATE_STMTS = {
    # ---------- BASE HELPERS ----------
    "vw_swing": """
        CREATE VIEW vw_swing AS
        SELECT
          s.swing_id,
          s.session_id,
          ds.session_uid,
          s.player_id,
          COALESCE(dp.full_name, 'Player ' || s.player_id::text) AS player_name,
          dp.sportai_player_uid AS player_uid,

          -- Pass through SportAI player distribution JSON (not used in point_log)
          (dp.swing_type_distribution)::jsonb AS player_swing_type_distribution,

          s.rally_id,
          s.start_s, s.end_s, s.ball_hit_s,
          s.start_ts, s.end_ts, s.ball_hit_ts,
          s.ball_hit_x, s.ball_hit_y,
          s.ball_speed, s.ball_player_distance,
          COALESCE(s.is_in_rally, FALSE) AS is_in_rally,
          s.serve, s.serve_type,
          s.swing_type,                 -- raw label if present (from SportAI)
          s.meta                        -- raw per-swing json (used for fallbacks in views)
        FROM fact_swing s
        LEFT JOIN dim_player  dp ON dp.player_id  = s.player_id
        LEFT JOIN dim_session ds ON ds.session_id = s.session_id;
    """,

    # ---------- NORMALIZED SWING VIEW (adds clean seconds + serve normalization) ----------
    "vw_swing_norm": """
        CREATE VIEW vw_swing_norm AS
        WITH base AS (
          SELECT
            vws.*,

            -- Clean seconds derived from timestamps (authoritative)
            EXTRACT(EPOCH FROM vws.start_ts)     AS start_s_clean,
            EXTRACT(EPOCH FROM vws.end_ts)       AS end_s_clean,
            EXTRACT(EPOCH FROM vws.ball_hit_ts)  AS ball_hit_s_clean,

            -- Raw best-available time (for reference only)
            COALESCE(vws.ball_hit_s, vws.start_s, vws.end_s) AS t_raw
          FROM vw_swing vws
        ),
        ordered AS (
          SELECT
            b.*,

            -- Sanitized timeline for ordering (always from *_ts-derived seconds)
            COALESCE(b.ball_hit_s_clean, b.start_s_clean, b.end_s_clean) AS t_clean,

            LAG(b.rally_id) OVER (
              PARTITION BY b.session_id
              ORDER BY COALESCE(b.ball_hit_s_clean, b.start_s_clean, b.end_s_clean), b.swing_id
            ) AS prev_rally_id,

            LAG(COALESCE(b.ball_hit_s_clean, b.start_s_clean, b.end_s_clean)) OVER (
              PARTITION BY b.session_id
              ORDER BY COALESCE(b.ball_hit_s_clean, b.start_s_clean, b.end_s_clean), b.swing_id
            ) AS prev_t_clean
          FROM base b
        ),
        inferred AS (
          SELECT
            o.*,
            CASE
              -- Rally id appears/changes ⇒ start of point
              WHEN o.rally_id IS NOT NULL AND (o.prev_rally_id IS DISTINCT FROM o.rally_id)
              THEN TRUE
              -- No rally yet + clear time gap from prior swing ⇒ likely new point (default 5s)
              WHEN o.rally_id IS NULL AND (o.prev_t_clean IS NULL OR (o.t_clean - o.prev_t_clean) > 5.0)
              THEN TRUE
              ELSE FALSE
            END AS inferred_point_start
          FROM ordered o
        ),
        numbered AS (
          SELECT
            i.*,
            -- Session-scoped running counter of inferred starts
            SUM(CASE WHEN i.inferred_point_start THEN 1 ELSE 0 END)
              OVER (PARTITION BY i.session_id ORDER BY i.t_clean, i.swing_id
                    ROWS UNBOUNDED PRECEDING) AS inferred_point_id
          FROM inferred i
        )
        SELECT
          n.*,
          (COALESCE(n.serve, FALSE) OR n.inferred_point_start) AS inferred_serve,
          CASE
            WHEN (COALESCE(n.serve, FALSE) OR n.inferred_point_start) THEN 'serve'
            ELSE n.swing_type
          END AS normalized_swing_type
        FROM numbered n;
    """,

    "vw_rally": """
        CREATE VIEW vw_rally AS
        SELECT
            r.rally_id,
            r.session_id,
            ds.session_uid,
            r.rally_number,  -- Treat as Point #
            r.start_s, r.end_s,
            r.start_ts, r.end_ts
        FROM dim_rally r
        LEFT JOIN dim_session ds ON ds.session_id = r.session_id;
    """,

    "vw_bounce": """
        CREATE VIEW vw_bounce AS
        SELECT
            b.bounce_id,
            b.session_id,
            ds.session_uid,
            b.hitter_player_id,
            dp.full_name AS hitter_name,
            b.rally_id,
            b.bounce_s, b.bounce_ts,
            b.x, b.y,
            b.bounce_type  -- 'in','out','net','long','wide'
        FROM fact_bounce b
        LEFT JOIN dim_player dp ON dp.player_id = b.hitter_player_id
        LEFT JOIN dim_session ds ON ds.session_id = b.session_id;
    """,

    "vw_player_position": """
        CREATE VIEW vw_player_position AS
        SELECT
            p.id,
            p.session_id,
            ds.session_uid,
            p.player_id,
            dp.full_name AS player_name,
            dp.sportai_player_uid AS player_uid,
            p.ts_s, p.ts, p.x, p.y
        FROM fact_player_position p
        LEFT JOIN dim_session ds ON ds.session_id = p.session_id
        LEFT JOIN dim_player dp ON dp.player_id = p.player_id;
    """,

    # ---------- XY NORMALIZATION (NEW) ----------
    "vw_ball_position_norm": """
        CREATE VIEW vw_ball_position_norm AS
        WITH dir_sample AS (
          -- Sample first 1s of each rally to infer initial ball travel direction
          SELECT
            s.session_id,
            s.rally_id,
            SUM(CASE WHEN s.dy IS NOT NULL THEN s.dy ELSE 0 END) AS sum_dy
          FROM (
            SELECT
              fbp.session_id,
              fbp.rally_id,
              fbp.timestamp_ts,
              fbp.y,
              LAG(fbp.y) OVER (
                PARTITION BY fbp.session_id, fbp.rally_id
                ORDER BY fbp.timestamp_ts
              ) AS y_prev,
              (fbp.y - LAG(fbp.y) OVER (
                PARTITION BY fbp.session_id, fbp.rally_id
                ORDER BY fbp.timestamp_ts
              )) AS dy
            FROM fact_ball_position fbp
            JOIN dim_rally dr
              ON dr.session_id = fbp.session_id
             AND dr.rally_id   = fbp.rally_id
            WHERE fbp.timestamp_ts BETWEEN dr.start_ts AND dr.start_ts + INTERVAL '1 second'
          ) s
          GROUP BY s.session_id, s.rally_id
        ),
        rally_dir AS (
          SELECT
            session_id,
            rally_id,
            CASE
              WHEN sum_dy IS NULL THEN 1   -- no data → keep as-is
              WHEN sum_dy = 0   THEN 1     -- ambiguous → keep as-is
              WHEN sum_dy > 0   THEN 1     -- ball moved toward +Y → keep
              ELSE -1                      -- ball moved toward −Y → flip 180°
            END AS dir_sign
          FROM dir_sample
        )
        SELECT
          fbp.session_id,
          fbp.rally_id,
          fbp.timestamp_ts,
          CASE WHEN rd.dir_sign = -1 THEN -fbp.x ELSE fbp.x END AS x_norm,
          CASE WHEN rd.dir_sign = -1 THEN -fbp.y ELSE fbp.y END AS y_norm,
          fbp.x AS x_orig,
          fbp.y AS y_orig,
          rd.dir_sign
        FROM fact_ball_position fbp
        LEFT JOIN rally_dir rd
          ON rd.session_id = fbp.session_id
         AND rd.rally_id   = fbp.rally_id;
    """,

    "vw_player_position_norm": """
        CREATE VIEW vw_player_position_norm AS
        WITH rally_dir AS (
          SELECT
            session_id,
            rally_id,
            MAX(dir_sign) AS dir_sign
          FROM vw_ball_position_norm
          GROUP BY session_id, rally_id
        )
        SELECT
          fpp.session_id,
          fpp.rally_id,
          fpp.player_id,
          fpp.timestamp_ts,
          CASE WHEN rd.dir_sign = -1 THEN -fpp.x ELSE fpp.x END AS x_norm,
          CASE WHEN rd.dir_sign = -1 THEN -fpp.y ELSE fpp.y END AS y_norm,
          fpp.x AS x_orig,
          fpp.y AS y_orig,
          rd.dir_sign
        FROM fact_player_position fpp
        LEFT JOIN rally_dir rd
          ON rd.session_id = fpp.session_id
         AND rd.rally_id   = fpp.rally_id;
    """,

    # ---------- PER-PLAYER DISTRIBUTION SUMMARY (kept for dashboards) ----------
    "vw_player_swing_dist": """
      CREATE VIEW vw_player_swing_dist AS
      SELECT
        dp.session_id,
        ds.session_uid,
        dp.player_id,
        dp.full_name AS player_name,
        dp.sportai_player_uid AS player_uid,
        (dp.swing_type_distribution)::jsonb AS swing_type_dist,

        -- unpacked numeric fields (SportAI keys)
        ((dp.swing_type_distribution)::jsonb->>'forehand')::float   AS dist_forehand,
        ((dp.swing_type_distribution)::jsonb->>'backhand')::float   AS dist_backhand,
        ((dp.swing_type_distribution)::jsonb->>'fh_slice')::float   AS dist_fh_slice,
        ((dp.swing_type_distribution)::jsonb->>'bh_slice')::float   AS dist_bh_slice,
        ((dp.swing_type_distribution)::jsonb->>'fh_volley')::float  AS dist_fh_volley,
        ((dp.swing_type_distribution)::jsonb->>'bh_volley')::float  AS dist_bh_volley,
        ((dp.swing_type_distribution)::jsonb->>'smash')::float      AS dist_smash,
        ((dp.swing_type_distribution)::jsonb->>'1st_serve')::float  AS dist_first_serve,
        ((dp.swing_type_distribution)::jsonb->>'2nd_serve')::float  AS dist_second_serve,
        ((dp.swing_type_distribution)::jsonb->>'drop_shot')::float  AS dist_drop_shot,
        ((dp.swing_type_distribution)::jsonb->>'tweener')::float    AS dist_tweener,
        ((dp.swing_type_distribution)::jsonb->>'other')::float      AS dist_other,
        (
          COALESCE(((dp.swing_type_distribution)::jsonb->>'1st_serve')::float,0) +
          COALESCE(((dp.swing_type_distribution)::jsonb->>'2nd_serve')::float,0)
        ) AS dist_serve_total
      FROM dim_player dp
      JOIN dim_session ds ON ds.session_id = dp.session_id;
    """,

    # ---------- ORDERING HELPERS ----------
    "vw_shot_order": """
        CREATE VIEW vw_shot_order AS
        SELECT
          fs.swing_id,
          fs.session_id,
          fs.rally_id,
          dr.rally_number,
          fs.player_id,
          COALESCE(fs.ball_hit_s, fs.start_s) AS t_order,
          ROW_NUMBER() OVER (
            PARTITION BY fs.session_id, fs.rally_id
            ORDER BY COALESCE(fs.ball_hit_s, fs.start_s), fs.swing_id
          ) AS shot_number_in_point
        FROM fact_swing fs
        JOIN dim_rally  dr ON dr.session_id = fs.session_id AND dr.rally_id = fs.rally_id
        WHERE fs.rally_id IS NOT NULL;
    """,

    # Serve-based grouping when rally_id is missing (uses vw_swing_norm)
    "vw_shot_order_norm": """
        CREATE VIEW vw_shot_order_norm AS
        WITH seq AS (
          SELECT
            s.session_id,
            s.swing_id,
            s.rally_id,
            s.t_clean,
            (COALESCE(s.serve, FALSE) OR COALESCE(s.inferred_serve, FALSE)) AS is_serve
          FROM vw_swing_norm s
        ),
        first_serve AS (
          SELECT session_id, MIN(t_clean) AS first_t
          FROM seq
          WHERE is_serve
          GROUP BY session_id
        ),
        seq2 AS (
          SELECT
            q.*,
            CASE
              WHEN fs.first_t IS NOT NULL AND q.t_clean >= fs.first_t THEN
                SUM(CASE WHEN q.is_serve THEN 1 ELSE 0 END)
                  OVER (PARTITION BY q.session_id ORDER BY q.t_clean, q.swing_id)
              ELSE NULL
            END AS serve_point_id
          FROM seq q
          LEFT JOIN first_serve fs ON fs.session_id = q.session_id
        ),
        ordered AS (
          SELECT
            s2.session_id,
            s2.swing_id,
            s2.rally_id,
            s2.serve_point_id,
            ROW_NUMBER() OVER (
              PARTITION BY s2.session_id, COALESCE(s2.rally_id, s2.serve_point_id)
              ORDER BY s2.t_clean, s2.swing_id
            ) AS shot_number_in_point
          FROM seq2 s2
          WHERE s2.serve_point_id IS NOT NULL OR s2.rally_id IS NOT NULL
        )
        SELECT
          o.session_id,
          o.swing_id,
          o.rally_id,
          o.serve_point_id,
          o.shot_number_in_point
        FROM ordered o;
    """,

    # ---------- POINT SUMMARY (winner/error) ----------
    "vw_point_summary": """
        CREATE VIEW vw_point_summary AS
        WITH ordered AS (
          SELECT
            so.session_id, so.rally_id, so.rally_number,
            so.swing_id, so.player_id, so.shot_number_in_point
          FROM vw_shot_order so
        ),
        first_last AS (
          SELECT DISTINCT ON (session_id, rally_id)
            session_id, rally_id, rally_number,
            (ARRAY_AGG(swing_id ORDER BY shot_number_in_point))[1] AS first_swing_id,
            (ARRAY_AGG(player_id ORDER BY shot_number_in_point))[1] AS first_hitter_id,
            (ARRAY_AGG(swing_id ORDER BY shot_number_in_point DESC))[1] AS last_swing_id,
            (ARRAY_AGG(player_id ORDER BY shot_number_in_point DESC))[1] AS last_hitter_id,
            (ARRAY_AGG(shot_number_in_point ORDER BY shot_number_in_point DESC))[1] AS total_shots
          FROM ordered
          GROUP BY session_id, rally_id, rally_number
        ),
        serve_row AS (
          SELECT
            fl.session_id, fl.rally_id,
            COALESCE( (SELECT fs.player_id
                       FROM fact_swing fs
                       WHERE fs.session_id = fl.session_id
                         AND fs.rally_id   = fl.rally_id
                         AND COALESCE(fs.serve,FALSE) = TRUE
                       ORDER BY COALESCE(fs.ball_hit_s, fs.start_s), fs.swing_id
                       LIMIT 1),
                     fl.first_hitter_id) AS server_player_id
          FROM first_last fl
        ),
        last_swing_bounce AS (
          SELECT
            fl.session_id, fl.rally_id,
            b.bounce_id, b.bounce_type, b.x AS bounce_x, b.y AS bounce_y
          FROM first_last fl
          JOIN vw_swing s ON s.swing_id = fl.last_swing_id
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM vw_bounce b
            WHERE b.session_id = fl.session_id
              AND b.rally_id   = fl.rally_id
              AND b.bounce_ts >= s.ball_hit_ts
            ORDER BY b.bounce_ts
            LIMIT 1
          ) b ON TRUE
        )
        SELECT
          ds.session_uid,
          fl.session_id,
          fl.rally_id,
          fl.rally_number AS point_number,
          fl.first_swing_id,
          fl.first_hitter_id,
          sr.server_player_id,
          fl.last_swing_id,
          fl.last_hitter_id,
          fl.total_shots,
          CASE
            WHEN COALESCE(lsb.bounce_type,'in') IN ('out','net','long','wide') THEN 'error'
            ELSE 'winner'
          END AS point_result_type,
          CASE
            WHEN COALESCE(lsb.bounce_type,'in') IN ('out','net','long','wide') THEN
                 (SELECT dp2.player_id
                  FROM dim_player dp2
                  WHERE dp2.session_id = fl.session_id
                    AND dp2.player_id <> fl.last_hitter_id
                  LIMIT 1)
            ELSE fl.last_hitter_id
          END AS winner_player_id
        FROM first_last fl
        JOIN dim_session ds ON ds.session_id = fl.session_id
        LEFT JOIN serve_row sr ON sr.session_id = fl.session_id AND sr.rally_id = fl.rally_id
        LEFT JOIN last_swing_bounce lsb ON lsb.session_id = fl.session_id AND lsb.rally_id = fl.rally_id;
    """,

    # ---------- SHOT-LEVEL TRANSACTION LOG ----------
    "vw_point_log": """
    CREATE VIEW vw_point_log AS
    WITH base AS (
      SELECT DISTINCT ON (s.swing_id)
        s.swing_id,
        s.session_id,
        s.session_uid,
        s.rally_id,
        r.rally_number AS point_number_real,
        so.shot_number_in_point,
        s.inferred_point_id,
        s.player_id,
        s.player_name,
        s.player_uid,
        s.serve,
        s.serve_type,

        -- Use seconds that share the same origin as other fact tables.
        COALESCE(s.start_s,     s.start_s_clean)     AS start_s,
        COALESCE(s.end_s,       s.end_s_clean)       AS end_s,
        COALESCE(s.ball_hit_s,  s.ball_hit_s_clean)  AS ball_hit_s,

        -- keep *_ts for traceability/timecodes
        s.start_ts, s.end_ts, s.ball_hit_ts,

        s.ball_hit_x, s.ball_hit_y,

        COALESCE(
          s.ball_speed,
          NULLIF(s.meta->>'ball_speed','')::double precision
        ) AS ball_speed,
        COALESCE(
          s.ball_player_distance,
          NULLIF(s.meta->>'ball_player_distance','')::double precision
        ) AS ball_player_distance,

        s.inferred_serve,
        s.normalized_swing_type,

        LOWER(NULLIF(COALESCE(
          s.meta->>'swing_type',
          s.meta->>'stroke',
          s.meta->>'shot_type',
          s.meta->>'label',
          s.meta->>'predicted_class'
        , ''), '')) AS swing_text
      FROM vw_swing_norm s
      JOIN vw_shot_order_norm so
        ON so.session_id = s.session_id AND so.swing_id = s.swing_id
      LEFT JOIN vw_rally r
        ON r.session_id = s.session_id AND r.rally_id = s.rally_id
      ORDER BY s.swing_id, s.t_clean
    ),

    -- Nearest player position by seconds (same origin)
    player_loc AS (
      SELECT
        b.swing_id,
        pp.x AS player_x_at_hit,
        pp.y AS player_y_at_hit
      FROM base b
      LEFT JOIN LATERAL (
        SELECT p.*
        FROM fact_player_position p
        WHERE p.session_id = b.session_id
          AND p.player_id  = b.player_id
          AND p.ts_s IS NOT NULL
        ORDER BY ABS(p.ts_s - b.ball_hit_s)     -- seconds vs seconds
        LIMIT 1
      ) pp ON TRUE
    ),

    -- Ball XY at contact (fallback for ball_hit_x/y)
    ball_pos_at_hit AS (
      SELECT
        b.swing_id,
        pb.x AS hit_x_from_ballpos,
        pb.y AS hit_y_from_ballpos
      FROM base b
      LEFT JOIN LATERAL (
        SELECT pb.*
        FROM fact_ball_position pb
        WHERE pb.session_id = b.session_id
          AND pb.ts_s BETWEEN b.ball_hit_s - 1 AND b.ball_hit_s + 1
        ORDER BY ABS(pb.ts_s - b.ball_hit_s)
        LIMIT 1
      ) pb ON TRUE
    ),

    -- First bounce after the hit (by seconds)
    first_bounce_after_hit AS (
      SELECT
        b.swing_id,
        bb.bounce_id,
        bb.x AS bounce_x,
        bb.y AS bounce_y,
        bb.bounce_type
      FROM base b
      LEFT JOIN LATERAL (
        SELECT bb.*
        FROM fact_bounce bb
        WHERE bb.session_id = b.session_id
          AND (
                (b.rally_id IS NOT NULL AND bb.rally_id = b.rally_id AND bb.bounce_s >= b.ball_hit_s)
            OR (b.rally_id IS NULL     AND bb.bounce_s >= b.ball_hit_s
                                      AND bb.bounce_s <= b.ball_hit_s + 2)
              )
        ORDER BY bb.bounce_s
        LIMIT 1
      ) bb ON TRUE
    ),

    -- Approximate bounce from first ball position after the hit (fallback)
    approx_bounce_from_ballpos AS (
      SELECT
        b.swing_id,
        pb2.x AS approx_bounce_x,
        pb2.y AS approx_bounce_y
      FROM base b
      LEFT JOIN LATERAL (
        SELECT pb2.*
        FROM fact_ball_position pb2
        WHERE pb2.session_id = b.session_id
          AND pb2.ts_s >  b.ball_hit_s
          AND pb2.ts_s <= b.ball_hit_s + 2
        ORDER BY pb2.ts_s
        LIMIT 1
      ) pb2 ON TRUE
    ),

    classify AS (
      SELECT
        b.*,
        pl.player_x_at_hit, pl.player_y_at_hit,
        fb.bounce_id, fb.bounce_x, fb.bounce_y, fb.bounce_type,

        -- ball-hit XY with fallback
        COALESCE(b.ball_hit_x, bh.hit_x_from_ballpos) AS ball_hit_x_final,
        COALESCE(b.ball_hit_y, bh.hit_y_from_ballpos) AS ball_hit_y_final,

        -- bounce XY with fallback
        COALESCE(fb.bounce_x, ab.approx_bounce_x) AS bounce_x_final,
        COALESCE(fb.bounce_y, ab.approx_bounce_y) AS bounce_y_final,

        CASE
          WHEN fb.bounce_type IN ('out','net','long','wide') THEN 'out'
          WHEN fb.bounce_type IS NULL THEN NULL
          ELSE 'in'
        END AS shot_result,

        CASE
          WHEN fb.bounce_type = 'net' THEN 'net'
          WHEN fb.bounce_type IN ('out','long','wide') THEN 'out_of_court'
          WHEN fb.bounce_type IS NULL THEN NULL
          ELSE 'floor'
        END AS ball_bounce_surface,
        CASE
          WHEN fb.bounce_type IS NULL THEN NULL
          ELSE (fb.bounce_type NOT IN ('net','out','long','wide'))
        END AS ball_bounce_is_floor,

        CASE
          WHEN fb.bounce_type IN ('net') THEN 'net'
          WHEN fb.bounce_type IN ('long','wide','out') THEN fb.bounce_type
          WHEN COALESCE(fb.bounce_y, ab.approx_bounce_y) IS NULL THEN NULL
          WHEN COALESCE(fb.bounce_y, ab.approx_bounce_y) <= -2.5 THEN 'deep'
          WHEN COALESCE(fb.bounce_y, ab.approx_bounce_y) BETWEEN -2.5 AND 2.5 THEN 'mid'
          ELSE 'short'
        END AS shot_description_depth
      FROM base b
      LEFT JOIN player_loc                 pl ON pl.swing_id = b.swing_id
      LEFT JOIN ball_pos_at_hit            bh ON bh.swing_id = b.swing_id
      LEFT JOIN first_bounce_after_hit     fb ON fb.swing_id = b.swing_id
      LEFT JOIN approx_bounce_from_ballpos ab ON ab.swing_id = b.swing_id
    ),

    final_map AS (
      SELECT
        c.*,
        CASE
          WHEN (c.inferred_serve OR c.serve) THEN
            CASE
              WHEN TRIM(COALESCE(c.serve_type,'')) ILIKE '1%' OR TRIM(COALESCE(c.serve_type,'')) ILIKE 'first%'  THEN '1st_serve'
              WHEN TRIM(COALESCE(c.serve_type,'')) ILIKE '2%' OR TRIM(COALESCE(c.serve_type,'')) ILIKE 'second%' THEN '2nd_serve'
              ELSE 'serve'
            END
          ELSE
            CASE
              WHEN c.swing_text IS NULL OR c.swing_text = ''                THEN 'other'
              WHEN c.swing_text ~* 'tweener'                                THEN 'tweener'
              WHEN c.swing_text ~* 'drop'                                    THEN 'drop_shot'
              WHEN c.swing_text ~* '(overhead|smash|^oh$)'                   THEN 'smash'
              WHEN c.swing_text ~* 'volley' AND c.swing_text ~* '(^fh|forehand)'  THEN 'fh_volley'
              WHEN c.swing_text ~* 'volley' AND c.swing_text ~* '(^bh|backhand)'  THEN 'bh_volley'
              WHEN c.swing_text ~* 'slice'  AND c.swing_text ~* '(^fh|forehand)'  THEN 'fh_slice'
              WHEN c.swing_text ~* 'slice'  AND c.swing_text ~* '(^bh|backhand)'  THEN 'bh_slice'
              WHEN c.swing_text ~* '(^fh|forehand)'                          THEN 'forehand'
              WHEN c.swing_text ~* '(^bh|backhand)'                          THEN 'backhand'
              ELSE 'other'
            END
        END AS swing_type_final,

        -- timecodes (derived from seconds; just for display)
        TO_CHAR((TIME '00:00' + (c.start_s    * INTERVAL '1 second')), 'HH24:MI:SS.MS') AS start_timecode,
        TO_CHAR((TIME '00:00' + (c.end_s      * INTERVAL '1 second')), 'HH24:MI:SS.MS') AS end_timecode,
        TO_CHAR((TIME '00:00' + (c.ball_hit_s * INTERVAL '1 second')), 'HH24:MI:SS.MS') AS ball_hit_timecode
      FROM classify c
    )
    SELECT
      f.session_uid,
      f.session_id,
      f.rally_id,
      COALESCE(f.point_number_real, f.inferred_point_id) AS point_number,
      f.shot_number_in_point AS shot_number,
      f.swing_id,
      f.player_id,
      f.player_name,
      f.player_uid,
      f.swing_type_final,
      f.shot_result,
      f.shot_description_depth,
      f.ball_bounce_surface,
      f.ball_bounce_is_floor,
      f.bounce_x_final AS ball_bounce_x,
      f.bounce_y_final AS ball_bounce_y,
      f.serve_type,
      f.serve,
      f.player_x_at_hit, f.player_y_at_hit,
      f.ball_hit_x_final AS ball_hit_x,
      f.ball_hit_y_final AS ball_hit_y,
      f.start_s, f.end_s, f.ball_hit_s,
      f.start_ts, f.end_ts, f.ball_hit_ts,
      f.start_timecode, f.end_timecode, f.ball_hit_timecode,
      f.ball_speed,
      f.ball_player_distance,
      f.inferred_serve
    FROM final_map f
    ORDER BY session_uid, point_number, shot_number;
    """,
}

# ---------- helpers ----------
def _table_exists(conn, t):
    return conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name=:t
        LIMIT 1
    """), {"t": t}).first() is not None

def _column_exists(conn, t, c):
    return conn.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t AND column_name=:c
        LIMIT 1
    """), {"t": t, "c": c}).first() is not None

def _get_relkind(conn, name):
    row = conn.execute(text("""
        SELECT c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname='public' AND lower(c.relname)=lower(:name)
        LIMIT 1
    """), {"name": name}).first()
    return row[0] if row else None  # 'v' view, 'm' matview, 'r' table, None

def _drop_view_or_matview(conn, name):
    kind = _get_relkind(conn, name)
    if kind == 'v':
        conn.execute(text(f"DROP VIEW IF EXISTS {name} CASCADE;"))
    elif kind == 'm':
        conn.execute(text(f"DROP MATERIALIZED VIEW IF EXISTS {name} CASCADE;"))
    elif kind == 'r':
        conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE;"))
    else:
        conn.execute(text(f"DROP VIEW IF EXISTS {name} CASCADE;"))
        conn.execute(text(f"DROP MATERIALIZED VIEW IF EXISTS {name} CASCADE;"))

def _preflight_or_raise(conn):
    required_tables = [
        "dim_session", "dim_player", "dim_rally",
        "fact_swing", "fact_bounce", "fact_player_position",
        "fact_ball_position"
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

    checks = [
        ("dim_session", "session_uid"),
        ("dim_rally", "rally_id"),
        ("dim_rally", "rally_number"),
        ("dim_rally", "start_s"),
        ("dim_rally", "end_s"),
        ("fact_swing", "swing_id"),
        ("fact_swing", "session_id"),
        ("fact_swing", "player_id"),
        ("fact_swing", "start_s"),
        ("fact_swing", "end_s"),
        ("fact_swing", "ball_hit_s"),
        ("fact_swing", "ball_hit_ts"),
        ("fact_swing", "ball_hit_x"),
        ("fact_swing", "ball_hit_y"),
        ("fact_swing", "serve"),
        ("fact_bounce", "bounce_ts"),
        ("fact_bounce", "x"),
        ("fact_bounce", "y"),
        ("fact_player_position", "ts"),
        ("fact_ball_position", "ts"),
        ("dim_player", "swing_type_distribution"),
    ]
    missing_cols = [(t,c) for (t,c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t,c) in missing_cols])
        raise RuntimeError(f"Missing required columns before creating views: {msg}")

# The endpoint expects this function name:
def init_views(engine):
    # keep VIEW_SQL_STMTS populated for any code importing it
    global VIEW_SQL_STMTS
    VIEW_SQL_STMTS = [CREATE_STMTS[name] for name in VIEW_NAMES]
    
    # ---- back-compat for older /ops/init-views import paths ----
    def run_views(engine):
        # Older code does: from db_views import run_views
        # Keep it working by delegating to init_views.
        return init_views(engine)

    with engine.begin() as conn:
        _preflight_or_raise(conn)
        # drop first to avoid dependency issues
        for name in VIEW_NAMES:
            _drop_view_or_matview(conn, name)
        # create in the declared order
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))
