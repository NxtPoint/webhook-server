# db_views.py — Silver = passthrough + derived (from bronze), Gold = thin extract
# ----------------------------------------------------------------------------------
# NOTES
# - SportAI sends coordinates in METERS. We do not autoscale; we treat x/y as meters.
# - Serve bucket logic / winner / score NOT added here (we'll layer later).
# - For transparency, we expose the FIRST FLOOR bounce per swing window and publish
#   bounce_x_center_m / bounce_y_center_m / bounce_y_norm_m (meters + normalized Y).
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
        "dim_session", "dim_player",
        "fact_swing", "fact_bounce",
        "fact_player_position", "fact_ball_position",
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
        ("fact_bounce", "bounce_type"),
        ("fact_bounce", "bounce_ts"),
        ("fact_bounce", "bounce_s"),
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

    # ----------------------------- point silver -----------------------------
    "vw_point_silver": """
        CREATE OR REPLACE VIEW vw_point_silver AS
        WITH
        const AS (
          SELECT
            10.97::numeric       AS court_w_m,
            23.77::numeric       AS court_l_m,
            10.97::numeric/2     AS half_w_m,
            23.77::numeric/2     AS mid_y_m,
            0.50::numeric        AS serve_eps_m
        ),

        -- S1. Base swings (original ordering)
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

        -- S2. Serve detection (original: overhead + serve band)
        serve_flags AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
            s.ball_hit_x AS x_ref,
            s.ball_hit_y AS y_ref,
            (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
            CASE
              WHEN s.ball_hit_y IS NULL THEN NULL
              ELSE (s.ball_hit_y <= (SELECT serve_eps_m FROM const)
                 OR  s.ball_hit_y >= (SELECT court_l_m FROM const) - (SELECT serve_eps_m FROM const))
            END AS inside_serve_band,
            CASE
              WHEN s.ball_hit_y IS NULL OR s.ball_hit_x IS NULL THEN NULL
              WHEN s.ball_hit_y < (SELECT mid_y_m FROM const)
                THEN CASE WHEN s.ball_hit_x < (SELECT half_w_m FROM const) THEN 'deuce' ELSE 'ad' END
              ELSE CASE WHEN s.ball_hit_x > (SELECT half_w_m FROM const) THEN 'deuce' ELSE 'ad' END
            END AS serving_side_d
          FROM swings s
        ),

        serve_events AS (
          SELECT
            sf.session_id,
            sf.swing_id           AS srv_swing_id,
            sf.player_id          AS server_id,
            dp.sportai_player_uid AS server_uid,
            sf.ord_ts,
            sf.serving_side_d
          FROM serve_flags sf
          LEFT JOIN dim_player dp
            ON dp.session_id = sf.session_id AND dp.player_id = sf.player_id
          WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),

        -- S3. Original point/game numbering
        serve_events_numbered AS (
          SELECT
            se.*,
            LAG(se.serving_side_d) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_side,
            LAG(se.server_uid)     OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_server_uid
          FROM serve_events se
        ),
        serve_points AS (
          SELECT
            sen.*,
            SUM(CASE
                  WHEN sen.prev_side IS NULL THEN 1
                  WHEN sen.serving_side_d IS DISTINCT FROM sen.prev_side THEN 1
                  ELSE 0
                END
            ) OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts, sen.srv_swing_id
                    ROWS UNBOUNDED PRECEDING) AS point_number_d,
            SUM(CASE
                  WHEN sen.prev_server_uid IS NULL THEN 1
                  WHEN sen.server_uid     IS DISTINCT FROM sen.prev_server_uid THEN 1
                  ELSE 0
                END
            ) OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts, sen.srv_swing_id
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

        -- S4. Normalize all bounces to meters + y_norm (this is the new layer)
        bounces_norm AS (
          SELECT
            b.session_id,
            b.bounce_id,
            b.bounce_ts,
            b.bounce_s,
            b.bounce_type,
            b.x AS bounce_x_center_m,
            b.y AS bounce_y_center_m,
            ((SELECT mid_y_m FROM const) + b.y) AS bounce_y_norm_m
          FROM vw_bounce_silver b
        ),

        -- S5. Attach swings to most recent serve (context)
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
            SELECT sp.* FROM serve_points_ix sp
            WHERE sp.session_id = s.session_id AND sp.ord_ts <= s.ord_ts
            ORDER BY sp.ord_ts DESC
            LIMIT 1
          ) sp ON TRUE
        ),

        -- S6. Shot number within point + next swing timing
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

        -- S7. First FLOOR bounce between this swing and the next swing (robust window)
        swing_bounce_floor AS (
          SELECT
            sn.swing_id, sn.session_id, sn.point_number_d, sn.shot_number_d,
            b.bounce_id, b.bounce_ts, b.bounce_s,
            b.bounce_x_center_m,
            b.bounce_y_center_m,
            b.bounce_y_norm_m,
            b.bounce_type AS bounce_type_raw
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT b.* FROM bounces_norm b
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
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')), b.bounce_id
            LIMIT 1
          ) b ON TRUE
        ),

        -- S8. Why the bounce is missing (debugging only)
        swing_bounce_any AS (
          SELECT
            sn.swing_id, sn.session_id,
            b.bounce_id AS any_bounce_id, b.bounce_type AS any_bounce_type
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT b.* FROM bounces_norm b
            WHERE b.session_id = sn.session_id
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
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')), b.bounce_id
            LIMIT 1
          ) b ON TRUE
        ),
        bounce_explain AS (
          SELECT sn.session_id, sn.swing_id,
                 CASE
                   WHEN sb.bounce_id IS NOT NULL THEN NULL
                   WHEN sba.any_bounce_id IS NULL THEN 'no_bounce_in_window'
                   WHEN sba.any_bounce_type <> 'floor' THEN 'no_floor_in_window'
                   ELSE 'unknown'
                 END AS why_null
          FROM swings_numbered sn
          LEFT JOIN swing_bounce_floor sb
            ON sb.session_id=sn.session_id AND sb.swing_id=sn.swing_id
          LEFT JOIN swing_bounce_any sba
            ON sba.session_id=sn.session_id AND sba.swing_id=sn.swing_id
        )

        -- FINAL SELECT (row = swing; base logic intact, only XY fields added)
        SELECT
          sn.session_id,
          sn.session_uid_d,
          sn.swing_id,
          sn.player_id,
          sn.rally_id,

          sn.start_s, sn.end_s, sn.ball_hit_s,
          sn.start_ts, sn.end_ts, sn.ball_hit_ts,
          sn.ball_hit_x, sn.ball_hit_y,
          sn.ball_speed,
          sn.swing_type AS swing_type_raw,

          -- FIRST FLOOR bounce (XY in meters + normalized Y)
          sb.bounce_id,
          sb.bounce_type_raw,
          sb.bounce_s                  AS bounce_s_d,
          sb.bounce_x_center_m         AS bounce_x_center_m,
          sb.bounce_y_center_m         AS bounce_y_center_m,
          sb.bounce_y_norm_m           AS bounce_y_norm_m,

          -- original serve flag (true/false)
          (EXISTS (
            SELECT 1 FROM serve_flags sf
            WHERE sf.session_id = sn.session_id
              AND sf.swing_id   = sn.swing_id
              AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
          )) AS serve_d,

          sn.point_number_d,
          sn.game_number_d,
          sn.point_in_game_d,
          sn.serving_side_d,

          be.why_null
        FROM swings_numbered sn
        LEFT JOIN swing_bounce_floor sb
          ON sb.session_id = sn.session_id AND sb.swing_id = sn.swing_id
        LEFT JOIN bounce_explain be
          ON be.session_id = sn.session_id AND be.swing_id = sn.swing_id
        ORDER BY sn.session_id, sn.point_number_d, sn.shot_number_d, sn.swing_id;
    """,

    # ------------------------------ DEBUG: streams ------------------------------
    "vw_bounce_stream_debug": """
        CREATE OR REPLACE VIEW vw_bounce_stream_debug AS
        WITH const AS (
          SELECT 23.77::numeric AS court_l_m, 23.77::numeric/2 AS mid_y_m, 10.97::numeric AS court_w_m
        )
        SELECT
          b.session_id,
          ds.session_uid,
          b.bounce_id,
          b.bounce_type,
          b.bounce_ts,
          b.bounce_s,
          b.x AS x_center,                -- meters from source
          b.y AS y_center,                -- meters from source
          b.x AS x_m_center,              -- explicit alias
          b.y AS y_m_center,              -- explicit alias
          ((SELECT mid_y_m FROM const) + b.y) AS y_m_norm,
          CASE WHEN b.bounce_type='floor' THEN 1 ELSE 0 END AS is_floor
        FROM fact_bounce b
        LEFT JOIN dim_session ds USING (session_id)
        ORDER BY b.session_id,
                 COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')),
                 b.bounce_id;
    """,

    # --------------------- DEBUG: all bounces per swing window ------------------
    "vw_point_bounces_debug": """
        CREATE OR REPLACE VIEW vw_point_bounces_debug AS
        WITH
        const AS (
          SELECT 23.77::numeric AS court_l_m, 23.77::numeric/2 AS mid_y_m
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
        serve_flags AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
            (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
            CASE
              WHEN s.ball_hit_y IS NULL THEN NULL
              ELSE (s.ball_hit_y <= 0.50 OR s.ball_hit_y >= 23.77 - 0.50)
            END AS inside_serve_band
          FROM swings s
        ),
        serve_events AS (
          SELECT sf.session_id, sf.swing_id AS srv_swing_id, sf.player_id AS server_id, sf.ord_ts
          FROM serve_flags sf
          WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),
        serves_numbered AS (
          SELECT
            se.*,
            LAG(se.srv_swing_id) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_srv
          FROM serve_events se
        ),
        serve_points AS (
          SELECT
            sn.*,
            SUM(CASE WHEN sn.prev_srv IS NULL OR sn.srv_swing_id <> sn.prev_srv THEN 1 ELSE 0 END)
              OVER (PARTITION BY sn.session_id ORDER BY sn.ord_ts, sn.srv_swing_id) AS point_number_d
          FROM serves_numbered sn
        ),
        swings_in_point AS (
          SELECT s.*, sp.point_number_d
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
            sn.session_id, sn.point_number_d, sn.swing_id, sn.shot_number_d,
            b.bounce_id, b.bounce_type, b.bounce_ts, b.bounce_s,
            b.x             AS bounce_x_center_m,
            b.y             AS bounce_y_center_m,
            ((SELECT mid_y_m FROM const) + b.y) AS bounce_y_norm_m,
            ROW_NUMBER() OVER (
              PARTITION BY sn.session_id, sn.swing_id
              ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')), b.bounce_id
            ) AS bounce_rank_in_shot
          FROM swings_numbered sn
          JOIN LATERAL (
            SELECT b.* FROM vw_bounce_silver b
            WHERE b.session_id = sn.session_id
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

        # proactively drop blockers
        for obj in LEGACY_OBJECTS:
            _drop_any(conn, obj)

        # drop in reverse, create in forward order
        for name in reversed(VIEW_NAMES):
            _drop_any(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))

# Back-compat
init_views = _apply_views
run_views  = _apply_views
