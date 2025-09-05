# db_views.py — Silver = passthrough + derived (from bronze), Gold = thin extract
# ----------------------------------------------------------------------------------
# This version restores stable point/game numbering and deuce/ad:
# - Use fact_swing.serve as primary serve signal (fallback only if absent)
# - Points = cumulative serves; Games increment when server UID changes
# - Serving side = deuce for point_in_game odd; ad for even
# - Bounce -> swing: first FLOOR bounce after swing, capped at next serve timestamp
# - Player-at-hit from fact_player_position (nearest sample)
# - Rally location = A/B/C/D (width quartiles), depth = short/mid/long
# - Terminal error classification (net / wide / long) + winner id
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
        -- D1. Court constants (singles width; coordinates centered at 0 on midline for X)
        const AS (
          SELECT
            8.23::numeric      AS court_w,
            23.77::numeric     AS court_l,
            23.77::numeric/2   AS mid_y,     -- 11.885
            8.23::numeric/2    AS half_w,    -- 4.115
            0.50::numeric      AS serve_eps_m,
            2.50::numeric      AS short_m,
            5.50::numeric      AS mid_m
        ),

        -- D2. Base swings (ordered)
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

        -- D3. Hitter UID
        hitter_uid AS (
          SELECT s.session_id, s.swing_id, dp.sportai_player_uid AS player_uid
          FROM swings s
          LEFT JOIN dim_player dp
            ON dp.session_id = s.session_id AND dp.player_id = s.player_id
        ),

        -- D4. Serve events (GROUND TRUTH from fact_swing.serve; fallback = overhead in serve band)
        serve_flags_fallback AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
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
            s.session_id,
            s.swing_id        AS srv_swing_id,
            s.player_id       AS server_id,
            dp.sportai_player_uid AS server_uid,
            s.ord_ts
          FROM swings s
          LEFT JOIN dim_player dp
            ON dp.session_id = s.session_id AND dp.player_id = s.player_id
          WHERE COALESCE(s.serve, FALSE) = TRUE

          UNION ALL

          SELECT
            sf.session_id,
            sf.swing_id,
            sf.player_id,
            dp.sportai_player_uid,
            sf.ord_ts
          FROM serve_flags_fallback sf
          LEFT JOIN dim_player dp
            ON dp.session_id = sf.session_id AND dp.player_id = sf.player_id
          WHERE COALESCE((SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id = sf.session_id) > 0, TRUE)
            AND NOT EXISTS (SELECT 1 FROM vw_swing_silver ss
                            WHERE ss.session_id = sf.session_id AND ss.swing_id = sf.swing_id AND COALESCE(ss.serve,FALSE))
            AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),

        -- D5. Numbering serves -> points and games
        serves_numbered AS (
          SELECT
            se.*,
            ROW_NUMBER() OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS point_number_d,
            LAG(se.server_uid) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_server_uid
          FROM serve_events se
        ),
        serve_points AS (
          SELECT
            sn.*,
            SUM(CASE WHEN sn.prev_server_uid IS NULL OR sn.prev_server_uid IS DISTINCT FROM sn.server_uid THEN 1 ELSE 0 END)
              OVER (PARTITION BY sn.session_id ORDER BY sn.ord_ts, sn.srv_swing_id ROWS UNBOUNDED PRECEDING) AS game_number_d
          FROM serves_numbered sn
        ),
        serve_points_ix AS (
          SELECT
            sp.*,
            ROW_NUMBER() OVER (PARTITION BY sp.session_id, sp.game_number_d ORDER BY sp.ord_ts, sp.srv_swing_id) AS point_in_game_d,
            LEAD(sp.ord_ts) OVER (PARTITION BY sp.session_id ORDER BY sp.ord_ts, sp.srv_swing_id) AS next_srv_ts,
            CASE WHEN (ROW_NUMBER() OVER (PARTITION BY sp.session_id, sp.game_number_d ORDER BY sp.ord_ts, sp.srv_swing_id)) % 2 = 1
                 THEN 'deuce' ELSE 'ad' END AS serving_side_d
          FROM serve_points sp
        ),

        -- D6. Attach each swing to the latest serve at/preceding it
        swings_in_point AS (
          SELECT
            s.*,
            sp.point_number_d,
            sp.game_number_d,
            sp.point_in_game_d,
            sp.server_id,
            sp.server_uid,
            sp.serving_side_d,
            sp.next_srv_ts
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

        -- D7. Shot number within point
        swings_numbered AS (
          SELECT
            sip.*,
            ROW_NUMBER() OVER (
              PARTITION BY sip.session_id, sip.point_number_d
              ORDER BY sip.ord_ts, sip.swing_id
            ) AS shot_number_d
          FROM swings_in_point sip
        ),

        -- D8. First FLOOR bounce after swing, capped at next serve (keeps inside point)
        swing_bounce_floor AS (
          SELECT
            sn.swing_id, sn.session_id, sn.point_number_d, sn.shot_number_d,
            b.bounce_id, b.bounce_ts, b.bounce_s,
            b.x AS bounce_x,                           -- center-origin in meters
            ( (SELECT mid_y FROM const) + b.y ) AS bounce_y,  -- normalize to 0..23.77
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
                    sn.next_srv_ts IS NULL
                 OR COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')) <= sn.next_srv_ts
                  )
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second'))
            LIMIT 1
          ) b ON TRUE
        ),

        -- D9. Serve bucket from the serve swing's bounce and serving_side rule (deuce 1-4, ad 5-8)
        serve_bucket AS (
          SELECT
            sp.session_id,
            sp.point_number_d,
            sp.serving_side_d,
            sbf.bounce_x, sbf.bounce_y,
            CASE
              WHEN sbf.bounce_x IS NULL OR sbf.bounce_y IS NULL THEN NULL
              ELSE (CASE WHEN sp.serving_side_d = 'ad' THEN 4 ELSE 0 END) +
                   CASE
                     WHEN sbf.bounce_x < -((SELECT half_w FROM const)/2.0) THEN 1
                     WHEN sbf.bounce_x <  0                                 THEN 2
                     WHEN sbf.bounce_x <  ((SELECT half_w FROM const)/2.0)  THEN 3
                     ELSE 4
                   END
            END AS serve_bucket_1_8_d
          FROM serve_points_ix sp
          LEFT JOIN swing_bounce_floor sbf
            ON sbf.session_id = sp.session_id
           AND sbf.swing_id   = sp.srv_swing_id
        ),

        -- D10. Player position at hit (nearest-in-time)
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
        ),

        -- D11. Receiver id (the other player in that point, if present)
        receiver_per_point AS (
          SELECT
            sn.session_id,
            sn.point_number_d,
            MAX(sn.player_id) FILTER (WHERE sn.player_id <> sn.server_id) AS receiver_id
          FROM swings_numbered sn
          GROUP BY sn.session_id, sn.point_number_d, sn.server_id
        ),

        -- D12. Last shot per point
        last_shot_per_point AS (
          SELECT session_id, point_number_d, MAX(shot_number_d) AS max_shot
          FROM swings_numbered
          GROUP BY session_id, point_number_d
        )

        -- FINAL SELECT
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

          -- player position at hit (fallback to ball contact if missing)
          COALESCE(pah.player_x_at_hit, sn.ball_hit_x) AS player_x_at_hit,
          COALESCE(pah.player_y_at_hit, sn.ball_hit_y) AS player_y_at_hit,

          sn.shot_number_d,
          sn.point_number_d,
          sn.game_number_d,
          sn.point_in_game_d,

          -- serve flag (from ground truth serve events mapping to this swing)
          (sn.shot_number_d = 1) AS serve_d,

          -- serving side from rule (1st point in game = deuce; alternate)
          sn.serving_side_d,

          -- serves in point & faults (naive: additional serves beyond first)
          1 + COALESCE((
            SELECT COUNT(*) FROM swings_numbered zz
            WHERE zz.session_id    = sn.session_id
              AND zz.point_number_d = sn.point_number_d
              AND zz.shot_number_d  > 1
              AND zz.serve IS TRUE
          ),0) AS serve_count_in_point_d,
          GREATEST(
            COALESCE((
              SELECT COUNT(*) FROM swings_numbered zz
              WHERE zz.session_id    = sn.session_id
                AND zz.point_number_d = sn.point_number_d
                AND zz.serve IS TRUE
            ),1) - 1, 0
          ) AS fault_serves_in_point_d,

          -- depth by distance to nearest baseline
          CASE
            WHEN sb.bounce_y IS NULL THEN NULL
            ELSE CASE
              WHEN LEAST(sb.bounce_y, (SELECT court_l FROM const) - sb.bounce_y) < (SELECT short_m FROM const) THEN 'short'
              WHEN LEAST(sb.bounce_y, (SELECT court_l FROM const) - sb.bounce_y) < (SELECT mid_m   FROM const) THEN 'mid'
              ELSE 'long'
            END
          END AS shot_depth_d,

          -- rally location A–D across width (quarters)
          CASE
            WHEN sb.bounce_x IS NULL THEN NULL
            WHEN sb.bounce_x < -((SELECT half_w FROM const))          THEN 'A'
            WHEN sb.bounce_x <  0                                     THEN 'B'
            WHEN sb.bounce_x <  ((SELECT half_w FROM const))          THEN 'C'
            ELSE 'D'
          END AS rally_location_d,

          -- serve bucket 1..8
          sv.serve_bucket_1_8_d,

          -- terminal error + winner
          (sn.shot_number_d = lsp.max_shot) AS is_terminal_shot_d,
          CASE
            WHEN sn.shot_number_d = lsp.max_shot THEN
              (
                CASE
                  WHEN sb.bounce_type_raw = 'net' THEN TRUE
                  WHEN sb.bounce_x IS NULL OR sb.bounce_y IS NULL THEN NULL
                  WHEN ABS(sb.bounce_x) > ((SELECT half_w FROM const)) THEN TRUE   -- wide
                  WHEN sb.bounce_y < 0 OR sb.bounce_y > (SELECT court_l FROM const) THEN TRUE  -- long
                  ELSE FALSE
                END
              )
            ELSE NULL
          END AS is_error_d,

          CASE
            WHEN sn.shot_number_d = lsp.max_shot THEN
              CASE
                WHEN sb.bounce_type_raw = 'net' THEN 'net'
                WHEN sb.bounce_x IS NULL OR sb.bounce_y IS NULL THEN NULL
                WHEN ABS(sb.bounce_x) > ((SELECT half_w FROM const)) THEN 'wide'
                WHEN sb.bounce_y < 0 OR sb.bounce_y > (SELECT court_l FROM const) THEN 'long'
                ELSE NULL
              END
            ELSE NULL
          END AS error_type_d,

          CASE
            WHEN sn.shot_number_d = lsp.max_shot THEN
              CASE
                WHEN (sb.bounce_type_raw = 'net'
                      OR ABS(sb.bounce_x) > ((SELECT half_w FROM const))
                      OR sb.bounce_y < 0 OR sb.bounce_y > (SELECT court_l FROM const))
                  THEN COALESCE(rpp.receiver_id, NULL)   -- hitter erred: opponent wins
                ELSE sn.player_id                         -- otherwise hitter wins (incl. aces)
              END
            ELSE NULL
          END AS point_winner_id_d,

          NULL::text AS score_str_d
        FROM swings_numbered sn
        LEFT JOIN hitter_uid hu
          ON hu.session_id = sn.session_id AND hu.swing_id = sn.swing_id
        LEFT JOIN swing_bounce_floor sb
          ON sb.session_id = sn.session_id AND sb.swing_id = sn.swing_id
        LEFT JOIN serve_bucket sv
          ON sv.session_id = sn.session_id AND sv.point_number_d = sn.point_number_d
        LEFT JOIN player_at_hit pah
          ON pah.session_id = sn.session_id AND pah.swing_id = sn.swing_id
        LEFT JOIN receiver_per_point rpp
          ON rpp.session_id = sn.session_id AND rpp.point_number_d = sn.point_number_d
        LEFT JOIN last_shot_per_point lsp
          ON lsp.session_id = sn.session_id AND lsp.point_number_d = sn.point_number_d
        ORDER BY sn.session_id, sn.point_number_d, sn.shot_number_d, sn.swing_id;
    """,

    # ------------------------------- point gold --------------------------------
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
