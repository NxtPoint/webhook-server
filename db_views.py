# db_views.py — Silver = passthrough + derived (from bronze), Gold = thin extract
# ----------------------------------------------------------------------------------
# Reinstated ORIGINAL rules (unchanged from your “looks much better” version):
# - serve_d: overhead AND contact-Y within 0.5m of either baseline
# - serving_side_d: far (y < mid_y) -> deuce if x < mid_x else ad; near (y >= mid_y) -> deuce if x > mid_x else ad
# - points: first serve = point 1; increment when serving side flips
# - games: start 1; increment when server UID changes
# - serve_bucket_1_8_d ONLY on serves; rally_location_d (A–D) ONLY on non-serves
# - bounce linking in vw_point_silver: first FLOOR bounce after swing, capped by next swing time
# - player_x/y_at_hit from fact_player_position (nearest in time)
#
# NEW in this file:
# - vw_bounce_stream_debug: raw bounce stream with raw and normalized XY (for validation)
# - vw_point_bounces_debug: all bounces per swing window with bounce_rank_in_shot
# ----------------------------------------------------------------------------------

from sqlalchemy import text
from typing import List

__all__ = ["init_views", "run_views", "VIEW_SQL_STMTS", "VIEW_NAMES", "CREATE_STMTS"]
VIEW_SQL_STMTS: List[str] = []

# ==================================================================================
# Utilities
# ==================================================================================

def _ensure_raw_ingest(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS raw_ingest (
          id           BIGSERIAL PRIMARY KEY,
          source       TEXT NOT NULL,
          doc_type     TEXT NOT NULL,
          session_uid  TEXT NOT NULL,
          ingest_ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
          payload      JSONB NOT NULL
        );
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_raw_ingest_session_uid ON raw_ingest(session_uid);"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_raw_ingest_doc_type    ON raw_ingest(doc_type);"))

def _table_exists(conn, t: str) -> bool:
    return conn.execute(text("""
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema='public' AND table_name=:t
        LIMIT 1
    """), {"t": t}).first() is not None

def _column_exists(conn, t: str, c: str) -> bool:
    return conn.execute(text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t AND column_name=:c
        LIMIT 1
    """), {"t": t, "c": c}).first() is not None

def _preflight_or_raise(conn):
    required_tables = [
        "dim_session", "dim_player", "dim_rally",
        "fact_swing", "fact_bounce", "fact_player_position", "fact_ball_position",
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

    checks = [
        ("dim_session", "session_uid"),
        ("dim_player", "sportai_player_uid"),
        ("fact_swing", "swing_id"),
        ("fact_swing", "session_id"),
        ("fact_swing", "player_id"),
        ("fact_swing", "start_s"),
        ("fact_swing", "ball_hit_s"),
        ("fact_swing", "start_ts"),
        ("fact_swing", "ball_hit_ts"),
        ("fact_swing", "ball_hit_x"),
        ("fact_swing", "ball_hit_y"),
        ("fact_swing", "ball_speed"),
        ("fact_swing", "swing_type"),
        ("fact_bounce", "bounce_id"),
        ("fact_bounce", "x"),
        ("fact_bounce", "y"),
        ("fact_player_position", "player_id"),
        ("fact_player_position", "ts_s"),
        ("fact_player_position", "x"),
        ("fact_player_position", "y"),
        ("fact_ball_position", "ts_s"),
        ("fact_ball_position", "x"),
        ("fact_ball_position", "y"),
    ]
    missing_cols = [(t, c) for (t, c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t, c) in missing_cols])
        raise RuntimeError(f"Missing required columns before creating views: {msg}")

def _drop_any(conn, name: str):
    kind = conn.execute(text("""
        SELECT CASE
                 WHEN EXISTS (SELECT 1 FROM information_schema.views
                              WHERE table_schema='public' AND table_name=:n) THEN 'view'
                 WHEN EXISTS (SELECT 1 FROM pg_matviews
                              WHERE schemaname='public' AND matviewname=:n) THEN 'mview'
                 WHEN EXISTS (SELECT 1 FROM information_schema.tables
                              WHERE table_schema='public' AND table_name=:n) THEN 'table'
                 ELSE NULL
               END
    """), {"n": name}).scalar()
    stmts = []
    if kind == 'view':
        stmts = [f'DROP VIEW IF EXISTS "{name}" CASCADE;']
    elif kind == 'mview':
        stmts = [f'DROP MATERIALIZED VIEW IF EXISTS "{name}" CASCADE;']
    elif kind == 'table':
        stmts = [f'DROP TABLE IF EXISTS "{name}" CASCADE;']
    else:
        stmts = [
            f'DROP VIEW IF EXISTS "{name}" CASCADE;',
            f'DROP MATERIALIZED VIEW IF EXISTS "{name}" CASCADE;',
            f'DROP TABLE IF EXISTS "{name}" CASCADE;',
        ]
    for stmt in stmts:
        conn.execute(text(stmt))

# ==================================================================================
# View manifest
# ==================================================================================

VIEW_NAMES = [
    "vw_swing_silver",
    "vw_ball_position_silver",
    "vw_bounce_silver",
    "vw_point_silver",
    "vw_point_gold",
    # debug helpers
    "vw_bounce_stream_debug",
    "vw_point_bounces_debug",
]

LEGACY_OBJECTS = [
    "vw_point_order_by_serve", "vw_point_log", "vw_point_log_gold",
    "vw_point_summary", "vw_point_shot_log", "vw_shot_order_gold",
    "point_log_tbl", "point_summary_tbl",
]

# ==================================================================================
# CREATE statements
# ==================================================================================

CREATE_STMTS = {
    # ---------------------------- swings passthrough ----------------------------
    "vw_swing_silver": """
        CREATE OR REPLACE VIEW vw_swing_silver AS
        SELECT
          fs.session_id,
          fs.swing_id,
          fs.player_id,
          fs.rally_id,
          -- timing
          fs.start_s, fs.end_s, fs.ball_hit_s,
          fs.start_ts, fs.end_ts, fs.ball_hit_ts,
          -- ball at contact
          fs.ball_hit_x, fs.ball_hit_y,
          fs.ball_speed,
          -- labels/raw
          fs.serve, fs.serve_type, fs.swing_type, fs.is_in_rally,
          fs.ball_player_distance,
          fs.meta,
          ds.session_uid AS session_uid_d
        FROM fact_swing fs
        LEFT JOIN dim_session ds USING (session_id);
    """,

    # --------------------- ball position passthrough ---------------------
    "vw_ball_position_silver": """
        CREATE OR REPLACE VIEW vw_ball_position_silver AS
        SELECT session_id, ts_s, ts, x, y
        FROM fact_ball_position;
    """,

    # -------------------------- bounce passthrough -----------------------
    "vw_bounce_silver": """
        CREATE OR REPLACE VIEW vw_bounce_silver AS
        SELECT
          b.session_id,
          b.bounce_id,
          b.hitter_player_id,
          b.rally_id,
          b.bounce_s,
          b.bounce_ts,
          b.x,
          b.y,
          b.bounce_type
        FROM fact_bounce b;
    """,

    # ----------------------------- point silver (unchanged) -----------------------------
    "vw_point_silver": """
        CREATE OR REPLACE VIEW vw_point_silver AS
        WITH
        const AS (
          SELECT
            10.97::numeric       AS court_w,
            23.77::numeric       AS court_l,
            5.5::numeric         AS mid_x,
            23.77::numeric / 2   AS mid_y,
            0.50::numeric        AS serve_eps_m,
            2.5::numeric         AS short_m,
            5.5::numeric         AS mid_m
        ),
        swings AS (
          SELECT
            v.*,
            COALESCE(
              v.ball_hit_ts,
              v.start_ts,
              (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')
            ) AS ord_ts
          FROM vw_swing_silver v
        ),
        hitter_uid AS (
          SELECT s.session_id, s.swing_id, dp.sportai_player_uid AS player_uid
          FROM swings s
          LEFT JOIN dim_player dp
            ON dp.session_id = s.session_id AND dp.player_id = s.player_id
        ),
        serve_flags AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
            s.ball_hit_x AS x_ref,
            s.ball_hit_y AS y_ref,
            (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
            CASE
              WHEN s.ball_hit_y IS NULL THEN NULL
              ELSE (s.ball_hit_y <= (SELECT serve_eps_m FROM const)
                 OR  s.ball_hit_y >= (SELECT court_l FROM const) - (SELECT serve_eps_m FROM const))
            END AS inside_serve_band
          FROM swings s
        ),
        serve_events AS (
          SELECT
            sf.session_id,
            sf.swing_id           AS srv_swing_id,
            sf.player_id          AS server_id,
            dp.sportai_player_uid AS server_uid,
            sf.ord_ts,
            CASE
              WHEN sf.y_ref IS NULL OR sf.x_ref IS NULL THEN NULL
              WHEN sf.y_ref < (SELECT mid_y FROM const)
                THEN CASE WHEN sf.x_ref < (SELECT mid_x FROM const) THEN 'deuce' ELSE 'ad' END
              ELSE CASE WHEN sf.x_ref > (SELECT mid_x FROM const) THEN 'deuce' ELSE 'ad' END
            END AS serving_side_d
          FROM serve_flags sf
          LEFT JOIN dim_player dp
            ON dp.session_id = sf.session_id AND dp.player_id = sf.player_id
          WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),
        serves_numbered AS (
          SELECT
            se.*,
            LAG(se.serving_side_d) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_side,
            LAG(se.server_uid)     OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_server_uid
          FROM serve_events se
        ),
        serve_points AS (
          SELECT
            sn.*,
            SUM(CASE
                  WHEN sn.prev_side IS NULL THEN 1
                  WHEN sn.serving_side_d IS DISTINCT FROM sn.prev_side THEN 1
                  ELSE 0
                END
            ) OVER (PARTITION BY sn.session_id ORDER BY sn.ord_ts, sn.srv_swing_id) AS point_number_d,
            SUM(CASE
                  WHEN sn.prev_server_uid IS NULL THEN 1
                  WHEN sn.server_uid IS DISTINCT FROM sn.prev_server_uid THEN 1
                  ELSE 0
                END
            ) OVER (PARTITION BY sn.session_id ORDER BY sn.ord_ts, sn.srv_swing_id) AS game_number_d
          FROM serves_numbered sn
        ),
        serve_points_ix AS (
          SELECT
            sp.*,
            sp.point_number_d
              - MIN(sp.point_number_d) OVER (PARTITION BY sp.session_id, sp.game_number_d)
              + 1 AS point_in_game_d
          FROM serve_points sp
        ),
        swings_in_point AS (
          SELECT
            s.*,
            sp.point_number_d,
            sp.game_number_d,
            sp.point_in_game_d,
            sp.server_id,
            sp.server_uid,
            sp.serving_side_d
          FROM swings s
          LEFT JOIN LATERAL (
            SELECT sp.*
            FROM serve_points_ix sp
            WHERE sp.session_id = s.session_id
              AND sp.ord_ts <= s.ord_ts
            ORDER BY sp.ord_ts DESC
            LIMIT 1
          ) sp ON TRUE
        ),
        swings_numbered AS (
          SELECT
            sip.*,
            ROW_NUMBER() OVER (
              PARTITION BY sip.session_id, sip.point_number_d
              ORDER BY sip.ord_ts, sip.swing_id
            ) AS shot_number_d,
            LEAD(sip.ball_hit_ts) OVER (PARTITION BY sip.session_id ORDER BY sip.ord_ts, sip.swing_id) AS next_ball_hit_ts,
            LEAD(sip.ball_hit_s)  OVER (PARTITION BY sip.session_id ORDER BY sip.ord_ts, sip.swing_id) AS next_ball_hit_s
          FROM swings_in_point sip
        ),
        swing_bounce_floor AS (
          SELECT
            sn.swing_id, sn.session_id, sn.point_number_d, sn.shot_number_d,
            b.bounce_id, b.bounce_ts, b.bounce_s,
            b.x AS bounce_x,
            ((SELECT mid_y FROM const) + b.y) AS bounce_y,
            b.bounce_type AS bounce_type_raw
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM vw_bounce_silver b
            WHERE b.session_id = sn.session_id
              AND b.bounce_type = 'floor'
              AND (
                    (b.bounce_ts IS NOT NULL AND sn.ball_hit_ts IS NOT NULL AND b.bounce_ts > sn.ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR sn.ball_hit_ts IS NULL)
                      AND b.bounce_s IS NOT NULL AND sn.ball_hit_s IS NOT NULL
                      AND b.bounce_s > sn.ball_hit_s)
                  )
              AND (
                    sn.next_ball_hit_ts IS NULL
                 OR (b.bounce_ts IS NOT NULL AND sn.next_ball_hit_ts IS NOT NULL AND b.bounce_ts <= sn.next_ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR sn.next_ball_hit_ts IS NULL)
                      AND sn.next_ball_hit_s IS NOT NULL AND b.bounce_s <= sn.next_ball_hit_s)
                  )
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second'))
            LIMIT 1
          ) b ON TRUE
        ),
        first_serve_per_point AS (
          SELECT sp.session_id, sp.point_number_d, MIN(sp.ord_ts) AS first_srv_ts
          FROM serve_points_ix sp
          GROUP BY sp.session_id, sp.point_number_d
        ),
        first_srv_ids AS (
          SELECT sp.session_id, sp.point_number_d, sp.srv_swing_id, sp.serving_side_d
          FROM serve_points_ix sp
          JOIN first_serve_per_point f
            ON f.session_id = sp.session_id
           AND f.point_number_d = sp.point_number_d
           AND f.first_srv_ts   = sp.ord_ts
        ),
        serve_bucket AS (
          SELECT
            f.session_id, f.point_number_d, f.serving_side_d,
            sbf.bounce_x, sbf.bounce_y,
            CASE
              WHEN sbf.bounce_x IS NULL OR sbf.bounce_y IS NULL THEN NULL
              ELSE
                (CASE WHEN f.serving_side_d = 'ad' THEN 4 ELSE 0 END) +
                (CASE
                   WHEN sbf.bounce_x < -((SELECT court_w FROM const) / 4.0) THEN 1
                   WHEN sbf.bounce_x <  0                                    THEN 2
                   WHEN sbf.bounce_x <  ((SELECT court_w FROM const) / 4.0)  THEN 3
                   ELSE 4
                 END)
            END AS serve_bucket_1_8_d
          FROM first_srv_ids f
          LEFT JOIN swing_bounce_floor sbf
            ON sbf.session_id = f.session_id
           AND sbf.swing_id   = f.srv_swing_id
        ),
        player_at_hit AS (
          SELECT
            sn.session_id, sn.swing_id,
            pp.x AS player_x_at_hit,
            pp.y AS player_y_at_hit
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT p.*
            FROM fact_player_position p
            WHERE p.session_id = sn.session_id
              AND p.player_id   = sn.player_id
            ORDER BY
              CASE
                WHEN sn.ball_hit_ts IS NOT NULL AND p.ts IS NOT NULL
                  THEN ABS(EXTRACT(EPOCH FROM (p.ts - sn.ball_hit_ts)))
                ELSE 1e9
              END,
              CASE
                WHEN sn.ball_hit_s IS NOT NULL AND p.ts_s IS NOT NULL
                  THEN ABS(p.ts_s - sn.ball_hit_s)
                ELSE 1e9
              END
            LIMIT 1
          ) pp ON TRUE
        )
        SELECT
          sn.session_id,
          sn.session_uid_d,
          sn.swing_id,
          sn.player_id,
          hu.player_uid,
          sn.rally_id,
          sn.start_s, sn.end_s, sn.ball_hit_s,
          sn.start_ts, sn.end_ts, sn.ball_hit_ts,
          sn.ball_hit_x, sn.ball_hit_y,
          sn.ball_speed,
          sn.swing_type AS swing_type_raw,
          sb.bounce_id,
          sb.bounce_type_raw,
          sb.bounce_x AS bounce_x,
          sb.bounce_y AS bounce_y,
          COALESCE(pah.player_x_at_hit, sn.ball_hit_x) AS player_x_at_hit,
          COALESCE(pah.player_y_at_hit, sn.ball_hit_y) AS player_y_at_hit,
          sn.shot_number_d,
          sn.point_number_d,
          sn.game_number_d,
          sn.point_in_game_d,
          (EXISTS (
            SELECT 1 FROM serve_flags sf
            WHERE sf.session_id = sn.session_id
              AND sf.swing_id   = sn.swing_id
              AND sf.is_fh_overhead
              AND COALESCE(sf.inside_serve_band, FALSE)
          )) AS serve_d,
          sn.serving_side_d,
          SUM(CASE WHEN (EXISTS (
                SELECT 1 FROM serve_flags sf
                WHERE sf.session_id = sn.session_id
                  AND sf.swing_id   = sn.swing_id
                  AND sf.is_fh_overhead
                  AND COALESCE(sf.inside_serve_band, FALSE)
          )) THEN 1 ELSE 0 END)
            OVER (PARTITION BY sn.session_id, sn.point_number_d) AS serve_count_in_point_d,
          GREATEST(
            SUM(CASE WHEN (EXISTS (
                  SELECT 1 FROM serve_flags sf
                  WHERE sf.session_id = sn.session_id
                    AND sf.swing_id   = sn.swing_id
                    AND sf.is_fh_overhead
                    AND COALESCE(sf.inside_serve_band, FALSE)
            )) THEN 1 ELSE 0 END)
              OVER (PARTITION BY sn.session_id, sn.point_number_d) - 1,
            0
          ) AS fault_serves_in_point_d,
          CASE
            WHEN (EXISTS (
                SELECT 1 FROM serve_flags sf
                WHERE sf.session_id = sn.session_id AND sf.swing_id = sn.swing_id
                  AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
            )) THEN NULL
            WHEN sb.bounce_y IS NULL THEN NULL
            ELSE CASE
              WHEN LEAST(sb.bounce_y, (SELECT court_l FROM const) - sb.bounce_y) < (SELECT short_m FROM const) THEN 'short'
              WHEN LEAST(sb.bounce_y, (SELECT court_l FROM const) - sb.bounce_y) < (SELECT mid_m   FROM const) THEN 'mid'
              ELSE 'long'
            END
          END AS shot_depth_d,
          CASE
            WHEN (EXISTS (
                SELECT 1 FROM serve_flags sf
                WHERE sf.session_id = sn.session_id AND sf.swing_id = sn.swing_id
                  AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
            )) THEN NULL
            WHEN sb.bounce_x IS NULL THEN NULL
            WHEN sb.bounce_x < -((SELECT court_w FROM const) / 4.0) THEN 'A'
            WHEN sb.bounce_x <  0                                    THEN 'B'
            WHEN sb.bounce_x <  ((SELECT court_w FROM const) / 4.0)  THEN 'C'
            ELSE 'D'
          END AS rally_location_d,
          CASE
            WHEN (EXISTS (
                SELECT 1 FROM serve_flags sf
                WHERE sf.session_id = sn.session_id AND sf.swing_id = sn.swing_id
                  AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
            ))
            THEN sv.serve_bucket_1_8_d
            ELSE NULL
          END AS serve_bucket_1_8_d,
          NULL::int  AS point_winner_id_d,
          NULL::text AS score_str_d,
          NULL::text AS error_type_d,
          NULL::boolean AS is_error_d
        FROM swings_numbered sn
        LEFT JOIN hitter_uid hu
          ON hu.session_id = sn.session_id AND hu.swing_id = sn.swing_id
        LEFT JOIN swing_bounce_floor sb
          ON sb.session_id = sn.session_id AND sb.swing_id = sn.swing_id
        LEFT JOIN serve_bucket sv
          ON sv.session_id = sn.session_id AND sv.point_number_d = sn.point_number_d
        LEFT JOIN player_at_hit pah
          ON pah.session_id = sn.session_id AND pah.swing_id = sn.swing_id
        ORDER BY sn.session_id, sn.point_number_d, sn.shot_number_d, sn.swing_id;
    """,

    # ------------------------------- point gold --------------------------------
    "vw_point_gold": """
        CREATE OR REPLACE VIEW vw_point_gold AS
        SELECT * FROM vw_point_silver;
    """,

    # ------------------------------- DEBUG: raw bounce stream --------------------
    "vw_bounce_stream_debug": """
        CREATE OR REPLACE VIEW vw_bounce_stream_debug AS
        WITH const AS (
          SELECT 23.77::numeric AS court_l, 23.77::numeric/2 AS mid_y
        )
        SELECT
          b.session_id,
          ds.session_uid,
          b.bounce_id,
          b.bounce_type,
          b.bounce_ts,
          b.bounce_s,
          -- raw center-origin coords from fact_bounce
          b.x AS x_center,
          b.y AS y_center,
          -- normalized court coords (0..23.77 on Y)
          (SELECT mid_y FROM const) + b.y AS y_norm,
          -- sanity flags
          CASE WHEN b.bounce_type='floor' THEN 1 ELSE 0 END AS is_floor,
          CASE WHEN b.x IS NULL OR b.y IS NULL THEN 1 ELSE 0 END AS is_xy_null
        FROM fact_bounce b
        LEFT JOIN dim_session ds USING (session_id)
        ORDER BY b.session_id, COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')), b.bounce_id;
    """,

    # ------------------------------- DEBUG: all bounces per swing window --------
    "vw_point_bounces_debug": """
        CREATE OR REPLACE VIEW vw_point_bounces_debug AS
        WITH
        const AS (
          SELECT
            23.77::numeric       AS court_l,
            23.77::numeric / 2   AS mid_y
        ),
        swings AS (
          SELECT
            v.*,
            COALESCE(
              v.ball_hit_ts,
              v.start_ts,
              (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')
            ) AS ord_ts
          FROM vw_swing_silver v
        ),
        -- point attachment only to give point_number_d and ordering
        serve_flags AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
            s.ball_hit_x AS x_ref, s.ball_hit_y AS y_ref,
            (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
            CASE
              WHEN s.ball_hit_y IS NULL THEN NULL
              ELSE (s.ball_hit_y <= 0.50 OR s.ball_hit_y >= (SELECT court_l FROM const) - 0.50)
            END AS inside_serve_band
          FROM swings s
        ),
        serve_events AS (
          SELECT
            sf.session_id, sf.swing_id AS srv_swing_id, sf.player_id AS server_id, sf.ord_ts,
            CASE
              WHEN sf.y_ref IS NULL OR sf.x_ref IS NULL THEN NULL
              WHEN sf.y_ref < (SELECT court_l FROM const)/2
                THEN CASE WHEN sf.x_ref < 5.5 THEN 'deuce' ELSE 'ad' END
              ELSE CASE WHEN sf.x_ref > 5.5 THEN 'deuce' ELSE 'ad' END
            END AS serving_side_d
          FROM serve_flags sf
          WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),
        serves_numbered AS (
          SELECT
            se.*,
            LAG(se.serving_side_d) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_side
          FROM serve_events se
        ),
        serve_points AS (
          SELECT
            sn.*,
            SUM(CASE WHEN sn.prev_side IS NULL OR sn.serving_side_d IS DISTINCT FROM sn.prev_side THEN 1 ELSE 0 END)
              OVER (PARTITION BY sn.session_id ORDER BY sn.ord_ts, sn.srv_swing_id) AS point_number_d
          FROM serves_numbered sn
        ),
        swings_in_point AS (
          SELECT
            s.*,
            sp.point_number_d
          FROM swings s
          LEFT JOIN LATERAL (
            SELECT sp.* FROM serve_points sp
            WHERE sp.session_id = s.session_id AND sp.ord_ts <= s.ord_ts
            ORDER BY sp.ord_ts DESC LIMIT 1
          ) sp ON TRUE
        ),
        swings_numbered AS (
          SELECT
            sip.*,
            ROW_NUMBER() OVER (PARTITION BY sip.session_id, sip.point_number_d ORDER BY sip.ord_ts, sip.swing_id) AS shot_number_d,
            LEAD(sip.ball_hit_ts) OVER (PARTITION BY sip.session_id ORDER BY sip.ord_ts, sip.swing_id) AS next_ball_hit_ts,
            LEAD(sip.ball_hit_s)  OVER (PARTITION BY sip.session_id ORDER BY sip.ord_ts, sip.swing_id) AS next_ball_hit_s
          FROM swings_in_point sip
        ),
        bounces_in_window AS (
          SELECT
            sn.session_id,
            sn.point_number_d,
            sn.swing_id,
            sn.shot_number_d,
            b.bounce_id,
            b.bounce_type,
            b.bounce_ts,
            b.bounce_s,
            b.x AS bounce_x_center,
            b.y AS bounce_y_center,
            ((SELECT mid_y FROM const) + b.y) AS bounce_y_norm,
            ROW_NUMBER() OVER (
              PARTITION BY sn.session_id, sn.swing_id
              ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second'))
            ) AS bounce_rank_in_shot
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM vw_bounce_silver b
            WHERE b.session_id = sn.session_id
              AND b.bounce_type = 'floor'
              AND (
                    (b.bounce_ts IS NOT NULL AND sn.ball_hit_ts IS NOT NULL AND b.bounce_ts > sn.ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR sn.ball_hit_ts IS NULL)
                      AND b.bounce_s IS NOT NULL AND sn.ball_hit_s IS NOT NULL
                      AND b.bounce_s > sn.ball_hit_s)
                  )
              AND (
                    sn.next_ball_hit_ts IS NULL
                 OR COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')) <= sn.next_ball_hit_ts
                  )
          ) b ON TRUE
        )
        SELECT * FROM bounces_in_window
        ORDER BY session_id, point_number_d, shot_number_d, bounce_rank_in_shot, bounce_id;
    """,
}

# ==================================================================================
# Apply
# ==================================================================================

def _apply_views(engine):
    global VIEW_SQL_STMTS
    VIEW_SQL_STMTS = [CREATE_STMTS[name] for name in VIEW_NAMES]
    with engine.begin() as conn:
        _ensure_raw_ingest(conn)
        _preflight_or_raise(conn)

        for obj in LEGACY_OBJECTS:
            _drop_any(conn, obj)

        for name in reversed(VIEW_NAMES):
            _drop_any(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))

init_views = _apply_views
run_views  = _apply_views
