# db_views.py — Silver = passthrough + derived (from bronze), Gold = thin extract
# ----------------------------------------------------------------------------------
# RULES
# - serve_d per swing: (swing_type in {'fh_overhead','fh-overhead'}) AND (within 0.5 m inside baseline to back fence)
# - serving side mapping:
#     top (y < mid_y)    -> deuce if x > mid_x else ad
#     bottom (y >= mid_y)-> deuce if x < mid_x else ad
# - points: first serve = point 1; point increments only when serving side flips between consecutive serves
# - games: start at 1; increment when server UID changes
# - Final Silver outputs exactly the agreed columns (minus server_behind_baseline_at_first_d which you removed)
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
    missing_cols = [(t,c) for (t,c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t,c) in missing_cols])
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
          ds.session_uid AS session_uid_d,
          fs.swing_id,
          fs.player_id,
          fs.rally_id,
          fs.start_s, fs.end_s, fs.ball_hit_s,
          fs.start_ts, fs.end_ts, fs.ball_hit_ts,
          fs.ball_hit_x, fs.ball_hit_y,
          fs.ball_speed,
          fs.swing_type
        FROM fact_swing fs
        LEFT JOIN dim_session ds USING (session_id);
    """,

    # ------------------------- ball position passthrough ------------------------
    "vw_ball_position_silver": """
        CREATE OR REPLACE VIEW vw_ball_position_silver AS
        SELECT
          session_id,
          ts_s,
          x,
          y
        FROM fact_ball_position;
    """,

    # ----------------------------- bounce passthrough ---------------------------
    "vw_bounce_silver": """
        CREATE OR REPLACE VIEW vw_bounce_silver AS
        SELECT
          session_id,
          bounce_id,
          rally_id,
          bounce_s,
          bounce_ts,
          x,
          y,
          bounce_type
        FROM fact_bounce;
    """,

    # -------------------------------- point silver ------------------------------
    "vw_point_silver": """
        CREATE OR REPLACE VIEW vw_point_silver AS
        WITH
        -- D1. Constants ----------------------------------------------------------
        const AS (
          SELECT
            8.23::numeric  AS court_w,
            23.77::numeric AS court_l,
            8.23::numeric / 2 AS mid_x,
            23.77::numeric / 2 AS mid_y,
            0.50::numeric AS serve_eps_m,  -- 0.5 m inside baseline allowance
            2.5::numeric  AS short_m,
            5.5::numeric  AS mid_m
        ),

        -- D2. Base swings (with ordering timestamp) ------------------------------
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

        -- D3. Hitter UID for each swing ------------------------------------------
        hitter_uid AS (
          SELECT
            s.session_id, s.swing_id,
            dp.sportai_player_uid AS player_uid
          FROM swings s
          LEFT JOIN dim_player dp
            ON dp.session_id = s.session_id AND dp.player_id = s.player_id
        ),

        -- D4. Player position at hit (nearest by ts or by s) ----------------------
        pos_at_hit AS (
          SELECT
            s.session_id, s.swing_id,
            h1.x AS x1, h1.y AS y1,
            h2.x AS x2, h2.y AS y2
          FROM swings s
          LEFT JOIN LATERAL (
            SELECT p.x, p.y
            FROM fact_player_position p
            WHERE p.session_id = s.session_id
              AND p.player_id  = s.player_id
              AND p.ts IS NOT NULL AND s.ball_hit_ts IS NOT NULL
            ORDER BY ABS(EXTRACT(EPOCH FROM (p.ts - s.ball_hit_ts)))
            LIMIT 1
          ) h1 ON TRUE
          LEFT JOIN LATERAL (
            SELECT p.x, p.y
            FROM fact_player_position p
            WHERE p.session_id = s.session_id
              AND p.player_id  = s.player_id
              AND p.ts IS NULL AND p.ts_s IS NOT NULL AND s.ball_hit_s IS NOT NULL
            ORDER BY ABS(p.ts_s - s.ball_hit_s)
            LIMIT 1
          ) h2 ON TRUE
        ),

        -- D5. Serve flags for EVERY swing (strict) + robust XY fallback ----------
        serve_flags_all AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
            COALESCE(pah.x1, pah.x2, s.ball_hit_x) AS x_ref,
            COALESCE(pah.y1, pah.y2, s.ball_hit_y) AS y_ref,
            (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
            CASE
              WHEN COALESCE(pah.y1, pah.y2, s.ball_hit_y) IS NULL THEN NULL
              WHEN COALESCE(pah.y1, pah.y2, s.ball_hit_y) <  (SELECT mid_y FROM const)
                THEN (COALESCE(pah.y1, pah.y2, s.ball_hit_y) <= (0.0 + (SELECT serve_eps_m FROM const)))
              ELSE
                (COALESCE(pah.y1, pah.y2, s.ball_hit_y) >= ((SELECT court_l FROM const) - (SELECT serve_eps_m FROM const)))
            END AS inside_serve_band
          FROM swings s
          LEFT JOIN pos_at_hit pah
            ON pah.session_id = s.session_id AND pah.swing_id = s.swing_id
        ),

        -- D6. Serve EVENTS with serving side from x_ref/y_ref --------------------
        -- Mapping we agreed:
        --   Far (y < mid_y):   deuce if x < mid_x, else ad
        --   Near (y >= mid_y): deuce if x > mid_x, else ad
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
              ELSE
                CASE WHEN sf.x_ref > (SELECT mid_x FROM const) THEN 'deuce' ELSE 'ad' END
            END AS serving_side_d
          FROM serve_flags_all sf
          LEFT JOIN dim_player dp
            ON dp.session_id = sf.session_id AND dp.player_id = sf.player_id
          WHERE sf.is_fh_overhead
            AND COALESCE(sf.inside_serve_band, FALSE)
        ),

        -- D7. Number serves into points & games ----------------------------------
        serve_events_numbered AS (
          SELECT
            se.*,
            LAG(se.serving_side_d) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts) AS prev_side,
            LAG(se.server_uid)     OVER (PARTITION BY se.session_id ORDER BY se.ord_ts) AS prev_server_uid
          FROM serve_events se
        ),
        serve_points AS (
          SELECT
            sen.*,
            -- first serve => point 1; new point when side flips
            SUM(CASE
                  WHEN sen.prev_side IS NULL THEN 1
                  WHEN sen.serving_side_d IS DISTINCT FROM sen.prev_side THEN 1
                  ELSE 0
                END
            ) OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts
                    ROWS UNBOUNDED PRECEDING) AS point_number_d,
            -- first serve => game 1; new game when server UID changes
            SUM(CASE
                  WHEN sen.prev_server_uid IS NULL THEN 1
                  WHEN sen.server_uid     IS DISTINCT FROM sen.prev_server_uid THEN 1
                  ELSE 0
                END
            ) OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts
                    ROWS UNBOUNDED PRECEDING) AS game_number_d
          FROM serve_events_numbered sen
        ),
        serve_points_ix AS (
          SELECT
            sp.*,
            sp.point_number_d
              - MIN(sp.point_number_d) OVER (PARTITION BY sp.session_id, sp.game_number_d)
              + 1 AS point_in_game_d
          FROM serve_points sp
        ),

        -- D8. Assign each swing to the latest serve at/preceding it ---------------
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

        -- D9. Shot number within assigned point ----------------------------------
        swings_numbered AS (
          SELECT
            sip.*,
            ROW_NUMBER() OVER (
              PARTITION BY sip.session_id, sip.point_number_d
              ORDER BY sip.ord_ts, sip.swing_id
            ) AS shot_number_d
          FROM swings_in_point sip
        ),

        -- D10. First serve per point (for serve bucket) ---------------------------
        first_serve_per_point AS (
          SELECT sp.session_id, sp.point_number_d,
                 MIN(sp.ord_ts) AS first_srv_ts
          FROM serve_points_ix sp
          GROUP BY sp.session_id, sp.point_number_d
        ),
        first_srv_ids AS (
          SELECT sp.session_id, sp.point_number_d, sp.srv_swing_id
          FROM serve_points_ix sp
          JOIN first_serve_per_point f
            ON f.session_id = sp.session_id
           AND f.point_number_d = sp.point_number_d
           AND f.first_srv_ts   = sp.ord_ts
        ),

        -- D11. Bounce after each swing (passthrough) ------------------------------
        swing_bounce AS (
          SELECT
            sn.swing_id, sn.session_id, sn.point_number_d, sn.shot_number_d,
            b.bounce_id, b.bounce_ts, b.bounce_s,
            b.x AS bounce_x, b.y AS bounce_y,
            b.bounce_type AS bounce_type_raw
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM vw_bounce_silver b
            WHERE b.session_id = sn.session_id
              AND (
                (b.bounce_ts IS NOT NULL AND sn.ball_hit_ts IS NOT NULL AND b.bounce_ts >= sn.ball_hit_ts)
                OR ((b.bounce_ts IS NULL OR sn.ball_hit_ts IS NULL)
                    AND b.bounce_s IS NOT NULL AND sn.ball_hit_s IS NOT NULL
                    AND b.bounce_s >= sn.ball_hit_s)
              )
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second'))
            LIMIT 1
          ) b ON TRUE
        ),

        -- D12. Serve bucket from FIRST serve's bounce -----------------------------
        serve_bucket AS (
          SELECT
            f.session_id, f.point_number_d,
            sb.bounce_x, sb.bounce_y,
            CASE
              WHEN sb.bounce_x IS NULL OR sb.bounce_y IS NULL THEN NULL
              ELSE (CASE WHEN sb.bounce_y >= 0 THEN 1 ELSE 0 END) * 4
                 + (CASE
                      WHEN sb.bounce_x < -0.5 THEN 1
                      WHEN sb.bounce_x <  0.0 THEN 2
                      WHEN sb.bounce_x <  0.5 THEN 3
                      ELSE 4
                    END)
            END AS serve_bucket_1_8_d
          FROM first_srv_ids f
          LEFT JOIN swing_bounce sb
            ON sb.session_id = f.session_id AND sb.swing_id = f.srv_swing_id
        )

        -- FINAL SELECT (exact agreed columns) ------------------------------------
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

          sfall.x_ref AS player_x_at_hit,
          sfall.y_ref AS player_y_at_hit,

          sn.shot_number_d,
          sn.point_number_d,
          sn.game_number_d,
          sn.point_in_game_d,

          (sfall.is_fh_overhead AND COALESCE(sfall.inside_serve_band, FALSE)) AS serve_d,
          FALSE AS is_error_d,

          sv.serve_bucket_1_8_d,
          sn.serving_side_d,

          COUNT(*) FILTER (WHERE (sfall.is_fh_overhead AND COALESCE(sfall.inside_serve_band, FALSE)))
             OVER (PARTITION BY sn.session_id, sn.point_number_d) AS serve_count_in_point_d,
          GREATEST(
            COUNT(*) FILTER (WHERE (sfall.is_fh_overhead AND COALESCE(sfall.inside_serve_band, FALSE)))
             OVER (PARTITION BY sn.session_id, sn.point_number_d) - 1,
            0
          ) AS fault_serves_in_point_d,

          CASE
            WHEN sb.bounce_y IS NULL THEN NULL
            ELSE CASE
              WHEN LEAST(sb.bounce_y, (SELECT court_l FROM const) - sb.bounce_y) < (SELECT short_m FROM const) THEN 'short'
              WHEN LEAST(sb.bounce_y, (SELECT court_l FROM const) - sb.bounce_y) < (SELECT mid_m   FROM const) THEN 'mid'
              ELSE 'long'
            END
          END AS shot_depth_d,
          CASE
            WHEN sb.bounce_x IS NULL THEN NULL
            WHEN ABS(sb.bounce_x) <= 0.5 THEN 'C'
            WHEN sb.bounce_x <  0      THEN 'L'
            ELSE 'R'
          END AS rally_location_d,

          NULL::int  AS point_winner_id_d,
          NULL::text AS score_str_d,
          NULL::text AS error_type_d

        FROM swings_numbered sn
        LEFT JOIN serve_flags_all sfall
          ON sfall.session_id = sn.session_id AND sfall.swing_id = sn.swing_id
        LEFT JOIN hitter_uid hu
          ON hu.session_id = sn.session_id AND hu.swing_id = sn.swing_id
        LEFT JOIN swing_bounce sb
          ON sb.session_id = sn.session_id AND sb.swing_id = sn.swing_id
        LEFT JOIN serve_bucket sv
          ON sv.session_id = sn.session_id AND sv.point_number_d = sn.point_number_d

        ORDER BY sn.session_id, sn.point_number_d, sn.shot_number_d, sn.swing_id;
    """,

    # -------------------------------- point gold --------------------------------
    "vw_point_gold": """
        CREATE OR REPLACE VIEW vw_point_gold AS
        SELECT * FROM vw_point_silver;
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

# Back-compat
init_views = _apply_views
run_views  = _apply_views
