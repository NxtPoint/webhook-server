# db_views.py — Silver = passthrough + derived (from bronze), Gold = thin extract
# ----------------------------------------------------------------------------------
# PRINCIPLES
# - Bronze is authoritative (facts). We never edit bronze.
# - Silver derives business logic from bronze only (no schema changes).
# - All derived columns end with "_d".
# - The removed column "server_behind_baseline_at_first_d" stays removed.
# - Serve logic has NO is_in_rally gating.
# - Sections are clearly delimited for wholesale rip/replace.
# ----------------------------------------------------------------------------------

from sqlalchemy import text
from typing import List

__all__ = ["init_views", "run_views", "VIEW_SQL_STMTS", "VIEW_NAMES", "CREATE_STMTS"]

VIEW_SQL_STMTS: List[str] = []  # populated from VIEW_NAMES/CREATE_STMTS

# ==================================================================================
# SECTION A: Utility / Preflight
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
# SECTION B: View manifest
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
# SECTION C: CREATE statements
# ==================================================================================

CREATE_STMTS = {
    # -------------------------------------------------------------------------
    # C1. Passthrough from bronze: swings
    # -------------------------------------------------------------------------
    "vw_swing_silver": """
        CREATE OR REPLACE VIEW vw_swing_silver AS
        SELECT
          fs.session_id,
          fs.swing_id,
          fs.player_id,
          fs.rally_id,
          fs.start_s, fs.end_s, fs.ball_hit_s,
          fs.start_ts, fs.end_ts, fs.ball_hit_ts,
          fs.ball_hit_x, fs.ball_hit_y,
          fs.ball_speed,
          fs.ball_player_distance,
          fs.is_in_rally,
          fs.serve,             -- raw if present
          fs.serve_type,        -- raw
          fs.swing_type,        -- raw
          fs.meta,              -- raw payload/labels if present
          ds.session_uid AS session_uid_d
        FROM fact_swing fs
        LEFT JOIN dim_session ds USING (session_id);
    """,

    # -------------------------------------------------------------------------
    # C2. Passthrough from bronze: ball positions
    # -------------------------------------------------------------------------
    "vw_ball_position_silver": """
        CREATE OR REPLACE VIEW vw_ball_position_silver AS
        SELECT session_id, ts_s, ts, x, y
        FROM fact_ball_position;
    """,

    # -------------------------------------------------------------------------
    # C3. Passthrough from bronze: bounces
    # -------------------------------------------------------------------------
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
        /* bronze has no bounce_type; expose a stable placeholder for Silver */
        NULL::text AS bounce_type
      FROM fact_bounce b;
  """,

    # -------------------------------------------------------------------------
    # C4. Derived: vw_point_silver (bronze-only)
    #      * numbering: computed from rally order + swing order (no vw_point)
    #      * serve logic: NO is_in_rally gating; optional behind-baseline check
    #      * removed: server_behind_baseline_at_first_d
    # -------------------------------------------------------------------------
    "vw_point_silver": """
        CREATE OR REPLACE VIEW vw_point_silver AS
        WITH
        /* ===================== D1. CONSTANTS ===================== */
        const AS (
          SELECT
            8.23::numeric  AS court_w,
            23.77::numeric AS court_l,
            8.23::numeric / 2 AS mid_x,         -- 4.115 m
            23.77::numeric / 2 AS mid_y,        -- 11.885 m
            0.60::numeric  AS behind_eps        -- tolerance for "behind baseline"
        ),

        /* ===================== D2. BASE SWINGS (bronze) ===================== */
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

        /* ===================== D3. NUMBERING FROM BRONZE ===================== */
        rally_first AS (
          SELECT session_id, rally_id, MIN(ord_ts) AS rally_first_ts
          FROM swings
          GROUP BY session_id, rally_id
        ),
        point_numbers AS (
          SELECT
            rf.session_id,
            rf.rally_id,
            ROW_NUMBER() OVER (
              PARTITION BY rf.session_id
              ORDER BY rf.rally_first_ts
            ) AS point_number_d
          FROM rally_first rf
        ),
        swings_num AS (
          SELECT
            s.*,
            pn.point_number_d,
            ROW_NUMBER() OVER (
              PARTITION BY s.session_id, s.rally_id
              ORDER BY s.ord_ts
            ) AS shot_number_d
          FROM swings s
          JOIN point_numbers pn
            ON pn.session_id = s.session_id AND pn.rally_id = s.rally_id
        ),

        /* ===================== D4. SERVER / RECEIVER BY POINT ===================== */
        pt_first AS (
          SELECT DISTINCT ON (session_id, point_number_d)
            session_id, point_number_d,
            swing_id  AS first_swing_id,
            player_id AS server_id,
            ball_hit_ts AS first_hit_ts,
            ball_hit_s  AS first_hit_s
          FROM swings_num
          ORDER BY session_id, point_number_d, shot_number_d
        ),
        pt_last AS (
          SELECT DISTINCT ON (session_id, point_number_d)
            session_id, point_number_d,
            swing_id  AS last_swing_id,
            player_id AS last_hitter_id,
            ball_hit_ts AS last_hit_ts,
            ball_hit_s  AS last_hit_s
          FROM swings_num
          ORDER BY session_id, point_number_d, shot_number_d DESC
        ),
        pp_receiver_guess AS (
          SELECT
            pf.session_id, pf.point_number_d,
            MIN(p.player_id) AS receiver_id_guess
          FROM pt_first pf
          JOIN swings_num p
            ON p.session_id     = pf.session_id
           AND p.point_number_d = pf.point_number_d
           AND p.shot_number_d  > 1
           AND p.player_id IS NOT NULL
           AND p.player_id <> pf.server_id
          GROUP BY pf.session_id, pf.point_number_d
        ),
        server_receiver AS (
          SELECT
            pf.session_id, pf.point_number_d,
            pf.server_id,
            COALESCE(
              rg.receiver_id_guess,
              (SELECT MIN(dp.player_id)
                 FROM dim_player dp
                WHERE dp.session_id = pf.session_id
                  AND dp.player_id <> pf.server_id)
            ) AS receiver_id
          FROM pt_first pf
          LEFT JOIN pp_receiver_guess rg
            ON rg.session_id     = pf.session_id
           AND rg.point_number_d = pf.point_number_d
        ),
        names AS (
          SELECT
            sr.*,
            dp1.full_name          AS server_name,
            dp1.sportai_player_uid AS server_uid,
            dp2.full_name          AS receiver_name,
            dp2.sportai_player_uid AS receiver_uid
          FROM server_receiver sr
          LEFT JOIN dim_player dp1 ON dp1.session_id = sr.session_id AND dp1.player_id = sr.server_id
          LEFT JOIN dim_player dp2 ON dp2.session_id = sr.session_id AND dp2.player_id = sr.receiver_id
        ),

        /* ===================== D5. GAME/POINT ORDERING ===================== */
        point_headers AS (
          SELECT
            n.*,
            LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number_d) AS prev_server_id,
            CASE WHEN LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number_d)
                      IS DISTINCT FROM n.server_id THEN 1 ELSE 0 END AS new_game_flag
          FROM names n
        ),
        game_numbered AS (
          SELECT
            ph.*,
            SUM(new_game_flag) OVER (
              PARTITION BY ph.session_id
              ORDER BY ph.point_number_d
              ROWS UNBOUNDED PRECEDING
            ) AS game_no_0     -- 0-based; +1 later
          FROM point_headers ph
        ),
        game_seq AS (
          SELECT
            gn.*,
            ROW_NUMBER() OVER (
              PARTITION BY gn.session_id, gn.game_no_0
              ORDER BY gn.point_number_d
            ) - 1 AS point_in_game_0   -- 0-based; +1 later
          FROM game_numbered gn
        ),

        /* ===================== D6. BOUNCE AFTER EACH SWING ===================== */
        s_bounce AS (
          SELECT
            s.swing_id, s.session_id, s.point_number_d, s.shot_number_d,
            b.bounce_id,
            b.bounce_ts, b.bounce_s,
            b.x AS bounce_x, b.y AS bounce_y,
            b.bounce_type AS bounce_type_raw
          FROM swings_num s
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM vw_bounce_silver b
            WHERE b.session_id = s.session_id
              AND (
                    (b.bounce_ts IS NOT NULL AND s.ball_hit_ts IS NOT NULL AND b.bounce_ts >= s.ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR s.ball_hit_ts IS NULL)
                     AND b.bounce_s IS NOT NULL AND s.ball_hit_s IS NOT NULL
                     AND b.bounce_s >= s.ball_hit_s)
                  )
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second'))
            LIMIT 1
          ) b ON TRUE
        ),

        /* ===================== D7. SERVE BUCKETS ===================== */
        serve_bounce AS (
          SELECT
            pf.session_id, pf.point_number_d,
            sb.bounce_x, sb.bounce_y, sb.bounce_type_raw
          FROM pt_first pf
          LEFT JOIN s_bounce sb
            ON sb.session_id = pf.session_id AND sb.swing_id = pf.first_swing_id
        ),
        serve_bucket AS (
          SELECT
            sb.*,
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
          FROM serve_bounce sb
        ),
        last_bounce AS (
          SELECT
            pl.session_id, pl.point_number_d,
            sb.bounce_x AS last_bounce_x, sb.bounce_y AS last_bounce_y,
            sb.bounce_type_raw AS last_bounce_type
          FROM pt_last pl
          LEFT JOIN s_bounce sb
            ON sb.session_id = pl.session_id AND sb.swing_id = pl.last_swing_id
        ),

        /* ===================== D8. SERVER POSITION AT FIRST SWING ===================== */
        pos_at_first AS (
          SELECT
            pf.session_id, pf.point_number_d,
            p1.x AS srv_x_1, p1.y AS srv_y_1,
            p2.x AS srv_x_2, p2.y AS srv_y_2
          FROM pt_first pf
          LEFT JOIN LATERAL (
            SELECT p.x, p.y
            FROM fact_player_position p
            WHERE p.session_id = pf.session_id
              AND p.player_id  = pf.server_id
              AND p.ts IS NOT NULL AND pf.first_hit_ts IS NOT NULL
            ORDER BY ABS(EXTRACT(EPOCH FROM (p.ts - pf.first_hit_ts)))
            LIMIT 1
          ) p1 ON TRUE
          LEFT JOIN LATERAL (
            SELECT p.x, p.y
            FROM fact_player_position p
            WHERE p.session_id = pf.session_id
              AND p.player_id  = pf.server_id
              AND p.ts IS NULL AND p.ts_s IS NOT NULL AND pf.first_hit_s IS NOT NULL
            ORDER BY ABS(p.ts_s - pf.first_hit_s)
            LIMIT 1
          ) p2 ON TRUE
        ),

        /* ===================== D9. SERVING SIDE & SERVE FLAG ===================== */
        serve_side AS (
          SELECT
            pf.session_id, pf.point_number_d,
            CASE
              WHEN COALESCE(paf.srv_y_1, paf.srv_y_2) IS NULL OR COALESCE(paf.srv_x_1, paf.srv_x_2) IS NULL THEN NULL
              WHEN COALESCE(paf.srv_y_1, paf.srv_y_2) <  (SELECT mid_y FROM const)
                THEN CASE WHEN COALESCE(paf.srv_x_1, paf.srv_x_2) < (SELECT mid_x FROM const) THEN 'deuce' ELSE 'ad' END
              ELSE CASE WHEN COALESCE(paf.srv_x_1, paf.srv_x_2) > (SELECT mid_x FROM const) THEN 'deuce' ELSE 'ad' END
            END AS serving_side_d
          FROM pt_first pf
          LEFT JOIN pos_at_first paf
            ON paf.session_id = pf.session_id AND paf.point_number_d = pf.point_number_d
        ),

        serve_flags AS (
          /* SERVE HEURISTIC (NO is_in_rally gating):
             Required: hitter == server for the point.
             Qualifiers (any of):
               - raw serve flag true
               - swing_type suggests serve (e.g. '%overhead%')
               - first swing in point (shot_number_d = 1)
             Optional positional gate when available: server starts behind baseline ±behind_eps.
          */
          SELECT
            p.*,
            CASE
              WHEN p.player_id <> n.server_id THEN FALSE
              ELSE
                CASE
                  WHEN COALESCE(paf.srv_y_1, paf.srv_y_2) IS NULL THEN
                       (COALESCE(p.serve, FALSE)
                        OR (p.swing_type ILIKE '%overhead%')
                        OR (p.shot_number_d = 1))
                  WHEN COALESCE(paf.srv_y_1, paf.srv_y_2) < (SELECT mid_y FROM const) THEN
                       ((COALESCE(paf.srv_y_1, paf.srv_y_2) <= (0.0 + (SELECT behind_eps FROM const)))
                        AND (COALESCE(p.serve, FALSE)
                             OR (p.swing_type ILIKE '%overhead%')
                             OR (p.shot_number_d = 1)))
                  ELSE
                       ((COALESCE(paf.srv_y_1, paf.srv_y_2) >= ((SELECT court_l FROM const) - (SELECT behind_eps FROM const)))
                        AND (COALESCE(p.serve, FALSE)
                             OR (p.swing_type ILIKE '%overhead%')
                             OR (p.shot_number_d = 1)))
                END
            END AS serve_d
          FROM swings_num p
          LEFT JOIN names n
            ON n.session_id = p.session_id AND n.point_number_d = p.point_number_d
          LEFT JOIN pos_at_first paf
            ON paf.session_id = p.session_id AND paf.point_number_d = p.point_number_d
        )

        /* ===================== D10. FINAL SELECT ===================== */
        SELECT
          /* ---- passthrough (bronze) ---- */
          p.session_id,
          p.rally_id,
          p.point_number_d,
          p.shot_number_d,
          p.swing_id,
          p.player_id,
          p.start_s, p.end_s, p.ball_hit_s,
          p.start_ts, p.end_ts, p.ball_hit_ts,
          p.ball_hit_x, p.ball_hit_y,
          p.ball_speed,
          p.ball_player_distance,
          p.is_in_rally,
          p.serve,
          p.serve_type,
          p.swing_type AS swing_type_raw,
          p.meta,

          /* ---- derived (no baseline column) ---- */
          (1 + gs.game_no_0)        AS game_number_d,
          (1 + gs.point_in_game_0)  AS point_in_game_d,
          ss.serving_side_d,
          sf.serve_d,
          lb.last_bounce_type       AS point_end_bounce_type_d,
          sv.serve_bucket_1_8_d,

          /* ---- bounce after each swing (passthrough) ---- */
            /* ---- bounce after each swing (passthrough) ---- */
            sb.bounce_id,
            sb.bounce_x               AS ball_bounce_x,
            sb.bounce_y               AS ball_bounce_y,
            sb.bounce_type_raw        AS bounce_type_raw,


          /* ---- context ---- */
          n.server_id,    n.server_name,    n.server_uid,
          n.receiver_id,  n.receiver_name,  n.receiver_uid

        FROM serve_flags sf
        JOIN swings_num p
          ON p.session_id = sf.session_id AND p.swing_id = sf.swing_id
        LEFT JOIN s_bounce     sb ON sb.session_id = p.session_id AND sb.swing_id = p.swing_id
        LEFT JOIN game_seq     gs ON gs.session_id = p.session_id AND gs.point_number_d = p.point_number_d
        LEFT JOIN last_bounce  lb ON lb.session_id = p.session_id AND lb.point_number_d = p.point_number_d
        LEFT JOIN serve_bucket sv ON sv.session_id = p.session_id AND sv.point_number_d = p.point_number_d
        LEFT JOIN names         n ON n.session_id  = p.session_id AND n.point_number_d  = p.point_number_d
        LEFT JOIN serve_side    ss ON ss.session_id = p.session_id AND ss.point_number_d = p.point_number_d
        ORDER BY p.session_id, p.point_number_d, p.shot_number_d, p.swing_id;
    """,

    # -------------------------------------------------------------------------
    # C5. Gold: thin extract of Silver
    # -------------------------------------------------------------------------
    "vw_point_gold": """
        CREATE OR REPLACE VIEW vw_point_gold AS
        SELECT *
        FROM vw_point_silver;
    """,
}

# ==================================================================================
# SECTION D: Apply views
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
