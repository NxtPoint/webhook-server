# db_views.py — Silver = passthrough + derived, Gold = thin extract of Silver
from sqlalchemy import text
from typing import List

# Public exports (back-compat with your ops/init-views endpoint)
__all__ = ["init_views", "run_views", "VIEW_SQL_STMTS", "VIEW_NAMES", "CREATE_STMTS"]

VIEW_SQL_STMTS: List[str] = []  # populated from VIEW_NAMES/CREATE_STMTS

# ---------------- constants / assumptions (geometry) ----------------
# These are used only for *derived* helpers. Everything raw is passed through untouched.
# If SportAI later confirms exact coordinates, adjust here in one place.
BASELINE_Y_THRESHOLD = 4.0        # "behind baseline" if |y| >= 4.0 (meters-ish in current feed)
SHORT_DEPTH_M        = 2.5        # 0–2.5 short, 2.5–5.5 mid, >5.5 long
MID_DEPTH_M          = 5.5

# ---------------- bronze helper: safe raw_ingest table ----------------
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

# ---------------- which views to (re)build ----------------
VIEW_NAMES = [
    # pure passthroughs for convenience
    "vw_swing_silver",
    "vw_ball_position_silver",
    "vw_bounce_silver",

    # silver point view = raw + derived (single source of truth)
    "vw_point_silver",

    # gold = thin extract of silver (no new logic)
    "vw_point_gold",
]

# objects we proactively drop if they still exist
LEGACY_OBJECTS = [
    "vw_point_order_by_serve", "vw_point_log", "vw_point_log_gold",
    "vw_point_summary", "vw_point_shot_log", "vw_shot_order_gold",
    "point_log_tbl", "point_summary_tbl",
]

def _drop_any(conn, name: str):
    """Drop any object named `name` in public schema, regardless of type."""
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

# ---------------- CREATE statements ----------------
CREATE_STMTS = {
    # ---- SILVER passthroughs (include point/shot for downstream) ----
    "vw_swing_silver": """
        CREATE OR REPLACE VIEW vw_swing_silver AS
        SELECT
          fs.session_id,
          fs.swing_id,
          fs.player_id,
          fs.rally_id,
          fs.point_number,             -- needed downstream
          fs.shot_number_in_point,     -- needed downstream
          fs.start_s, fs.end_s, fs.ball_hit_s,
          fs.start_ts, fs.end_ts, fs.ball_hit_ts,
          fs.ball_hit_x, fs.ball_hit_y,
          fs.ball_speed,
          fs.ball_player_distance,
          fs.is_in_rally,
          fs.serve,             -- raw flag if present
          fs.serve_type,        -- raw
          fs.swing_type,        -- raw
          fs.meta,              -- raw payload/labels if present
          ds.session_uid AS session_uid_d
        FROM fact_swing fs
        LEFT JOIN dim_session ds USING (session_id);
    """,

    "vw_ball_position_silver": """
        CREATE OR REPLACE VIEW vw_ball_position_silver AS
        SELECT session_id, ts_s, ts, x, y
        FROM fact_ball_position;
    """,

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

    # ---- SILVER point view = raw + derived (single place) ----
    # NOTE: fixed to reference columns that exist in sources; removed references to non-existing fields.
    "vw_point_silver": """
        CREATE OR REPLACE VIEW vw_point_silver AS
        WITH
        /* ------------------------------------------------------------------ */
        /* constants from SportAI coordinate frame                             */
        /* ------------------------------------------------------------------ */
        const AS (
          SELECT
            8.23::numeric  AS court_w,
            23.77::numeric AS court_l,
            8.23::numeric / 2 AS mid_x,     -- 4.115 m
            23.77::numeric / 2 AS mid_y,    -- 11.885 m
            0.60::numeric  AS behind_eps    -- ~60 cm tolerance for "behind baseline"
        ),

        /* ------------------------------------------------------------------ */
        /* Base swings in point order (raw swings + an ordering timestamp)     */
        /* ------------------------------------------------------------------ */
        po AS (
          SELECT
            v.*,
            COALESCE(
              v.ball_hit_ts,
              v.start_ts,
              (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')
            ) AS ord_ts
          FROM vw_swing_silver v
        ),

        /* first & last swing per point (for server/receiver and last bounce) */
        pt_first AS (
          SELECT DISTINCT ON (session_id, point_number)
            session_id, point_number,
            swing_id  AS first_swing_id,
            player_id AS server_id,
            ball_hit_ts AS first_hit_ts,
            ball_hit_s  AS first_hit_s
          FROM po
          ORDER BY session_id, point_number, shot_number_in_point
        ),
        pt_last AS (
          SELECT DISTINCT ON (session_id, point_number)
            session_id, point_number,
            swing_id  AS last_swing_id,
            player_id AS last_hitter_id,
            ball_hit_ts AS last_hit_ts,
            ball_hit_s  AS last_hit_s
          FROM po
          ORDER BY session_id, point_number, shot_number_in_point DESC
        ),

        /* Guess receiver (first opposing hitter in the point) */
        pp_receiver_guess AS (
          SELECT
            pf.session_id, pf.point_number,
            MIN(p.player_id) AS receiver_id_guess
          FROM pt_first pf
          LEFT JOIN po p
            ON p.session_id   = pf.session_id
           AND p.point_number = pf.point_number
           AND p.shot_number_in_point > 1
           AND p.player_id IS NOT NULL
           AND p.player_id <> pf.server_id
          GROUP BY pf.session_id, pf.point_number
        ),
        server_receiver AS (
          SELECT
            pf.session_id, pf.point_number,
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
            ON rg.session_id  = pf.session_id
           AND rg.point_number = pf.point_number
        ),

        /* names for context */
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

        /* game numbering from server changes → then point_in_game within game */
        point_headers AS (
          SELECT
            n.*,
            LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number) AS prev_server_id,
            CASE WHEN LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number)
                      IS DISTINCT FROM n.server_id THEN 1 ELSE 0 END AS new_game_flag
          FROM names n
        ),
        game_numbered AS (
          SELECT
            ph.*,
            SUM(new_game_flag) OVER (
              PARTITION BY ph.session_id
              ORDER BY ph.point_number
              ROWS UNBOUNDED PRECEDING
            ) AS game_no_0     -- 0-based; we’ll +1 later
          FROM point_headers ph
        ),
        game_seq AS (
          SELECT
            gn.*,
            ROW_NUMBER() OVER (
              PARTITION BY gn.session_id, gn.game_no_0
              ORDER BY gn.point_number
            ) - 1 AS point_in_game_0   -- 0-based; we’ll +1 later
          FROM game_numbered gn
        ),

        /* bounce immediately after each swing */
        s_bounce AS (
          SELECT
            s.swing_id, s.session_id,
            b.bounce_id,
            b.bounce_ts, b.bounce_s,
            b.x AS bounce_x, b.y AS bounce_y,
            b.bounce_type AS bounce_type_raw
          FROM po s
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

        /* serve bucket */
        serve_bounce AS (
          SELECT
            pf.session_id, pf.point_number,
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

        /* last bounce per point (passthrough) */
        last_bounce AS (
          SELECT
            pl.session_id, pl.point_number,
            sb.bounce_x AS last_bounce_x, sb.bounce_y AS last_bounce_y,
            sb.bounce_type_raw AS last_bounce_type
          FROM pt_last pl
          LEFT JOIN s_bounce sb
            ON sb.session_id = pl.session_id AND sb.swing_id = pl.last_swing_id
        ),

        /* server position at the first swing (court coordinates proxy = raw x/y) */
        pos_at_first AS (
          SELECT
            pf.session_id, pf.point_number,
            pp.x AS srv_x,
            pp.y AS srv_y
          FROM pt_first pf
          LEFT JOIN LATERAL (
            SELECT p.x, p.y
              FROM fact_player_position p
             WHERE p.session_id = pf.session_id
               AND p.player_id  = pf.server_id
               AND p.ts_s IS NOT NULL AND pf.first_hit_s IS NOT NULL
             ORDER BY ABS(p.ts_s - pf.first_hit_s)
             LIMIT 1
          ) pp ON TRUE
        ),

        /* serving side and serve flag logic */
        serve_side AS (
          SELECT
            pf.session_id, pf.point_number,
            CASE
              WHEN paf.srv_y IS NULL OR paf.srv_x IS NULL THEN NULL
              WHEN paf.srv_y <  (SELECT mid_y FROM const)
                THEN CASE WHEN paf.srv_x < (SELECT mid_x FROM const) THEN 'deuce' ELSE 'ad' END
              ELSE CASE WHEN paf.srv_x > (SELECT mid_x FROM const) THEN 'deuce' ELSE 'ad' END
            END AS serving_side_d
          FROM pt_first pf
          LEFT JOIN pos_at_first paf
            ON paf.session_id = pf.session_id AND paf.point_number = pf.point_number
        ),
        serve_flags AS (
          /* Mark serve swings with conservative criteria */
          SELECT
            p.*,
            CASE
              WHEN p.player_id <> n.server_id THEN FALSE
              WHEN p.swing_type ILIKE '%overhead%' IS NOT TRUE THEN FALSE
              WHEN COALESCE(p.is_in_rally, FALSE) THEN FALSE
              ELSE
                CASE
                  WHEN paf.srv_y IS NULL THEN TRUE
                  WHEN paf.srv_y < (SELECT mid_y FROM const)
                    THEN (paf.srv_y <= (0.0 + (SELECT behind_eps FROM const)))
                  ELSE
                    (paf.srv_y >= ((SELECT court_l FROM const) - (SELECT behind_eps FROM const)))
                END
            END AS serve_d
          FROM po p
          LEFT JOIN names n
            ON n.session_id = p.session_id AND n.point_number = p.point_number
          LEFT JOIN pos_at_first paf
            ON paf.session_id = p.session_id AND paf.point_number = p.point_number
        )

        SELECT
          /* ---------- passthrough bronze ---------- */
          p.session_id,
          p.rally_id,
          p.point_number,
          p.shot_number_in_point AS shot_number,
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

          /* ---------- derived (keep names; one deletion requested) ---------- */
          (1 + gs.game_no_0)        AS game_number_d,
          (1 + gs.point_in_game_0)  AS point_in_game_d,
          ss.serving_side_d,
          sf.serve_d,
          /* "server_behind_baseline_at_first_d" intentionally removed */
          lb.last_bounce_type       AS point_end_bounce_type_d,
          sv.serve_bucket_1_8_d,

          /* bounce after each swing (passthrough) */
          sb.bounce_id,
          sb.bounce_x               AS ball_bounce_x,
          sb.bounce_y               AS ball_bounce_y,
          sb.bounce_type            AS bounce_type_raw,

          /* context */
          n.server_id,    n.server_name,    n.server_uid,
          n.receiver_id,  n.receiver_name,  n.receiver_uid

        FROM serve_flags sf
        JOIN po p
          ON p.session_id = sf.session_id AND p.swing_id = sf.swing_id
        LEFT JOIN s_bounce     sb ON sb.session_id = p.session_id AND sb.swing_id = p.swing_id
        LEFT JOIN game_seq     gs ON gs.session_id = p.session_id AND gs.point_number = p.point_number
        LEFT JOIN last_bounce  lb ON lb.session_id = p.session_id AND lb.point_number = p.point_number
        LEFT JOIN serve_bucket sv ON sv.session_id = p.session_id AND sv.point_number = p.point_number
        LEFT JOIN names         n ON n.session_id  = p.session_id AND n.point_number  = p.point_number
        LEFT JOIN serve_side    ss ON ss.session_id = p.session_id AND ss.point_number = p.point_number
        ORDER BY p.session_id, p.point_number, p.shot_number_in_point, p.swing_id;
    """,

    # ---- GOLD = thin extract of Silver (no logic; tweak columns here if you want a reporting subset) ----
    "vw_point_gold": """
        CREATE OR REPLACE VIEW vw_point_gold AS
        SELECT *
        FROM vw_point_silver;
    """,
}

# ---------------- preflight (fail fast if base tables missing) ----------------
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
        ("fact_swing", "point_number"),           # added: required downstream
        ("fact_swing", "shot_number_in_point"),   # added: required downstream
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

# ---------------- apply all views ----------------
def _apply_views(engine):
    """Drops legacy objects, then (re)creates all views listed in VIEW_NAMES."""
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

# Back-compat names used by the ops endpoint
init_views = _apply_views
run_views  = _apply_views
