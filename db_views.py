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
    # ---- SILVER passthroughs (no edits; just labeling) ----
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
    "vw_point_silver": f"""
        CREATE OR REPLACE VIEW vw_point_silver AS
        WITH
        -- swings with an ordering timestamp
        s AS (
          SELECT
            v.*,
            COALESCE(v.ball_hit_ts, v.start_ts,
                     (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')
                    ) AS ord_ts
          FROM vw_swing_silver v
        ),

        -- per-session centerline from player positions (min+max)/2
        center_x AS (
          SELECT session_id, (MIN(x) + MAX(x)) / 2.0 AS cx
          FROM fact_player_position
          GROUP BY session_id
        ),

        -- nearest player (server) position at ball-hit for each swing
        pos_at_hit AS (
          SELECT
            s.session_id, s.swing_id, s.player_id,
            pp.x AS x_player_at_hit, pp.y AS y_player_at_hit
          FROM s
          LEFT JOIN LATERAL (
            SELECT p.x, p.y
            FROM fact_player_position p
            WHERE p.session_id = s.session_id
              AND p.player_id  = s.player_id
              AND (
                    (p.ts   IS NOT NULL AND s.ball_hit_ts IS NOT NULL AND p.ts   = s.ball_hit_ts)
                 OR (p.ts_s IS NOT NULL AND s.ball_hit_s  IS NOT NULL AND p.ts_s = s.ball_hit_s)
                  )
            ORDER BY 1
            LIMIT 1
          ) pp ON TRUE
        ),

        -- bounce immediately after each swing (for serve location & last-bounce)
        s_bounce AS (
          SELECT
            s.session_id, s.swing_id,
            b.bounce_id, b.bounce_ts, b.bounce_s, b.x AS bx, b.y AS by, b.bounce_type AS bounce_type_raw
          FROM s
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

        -- join handy labels
        swing_labeled AS (
          SELECT
            s.*,
            p.x_player_at_hit, p.y_player_at_hit,
            cx.cx,
            CASE
              WHEN COALESCE(ABS(p.y_player_at_hit), 0) >= {BASELINE_Y_THRESHOLD}
              THEN TRUE ELSE FALSE
            END AS behind_baseline_at_hit,
            CASE
              WHEN s.swing_type ILIKE '%overhead%' AND COALESCE(ABS(p.y_player_at_hit),0) >= {BASELINE_Y_THRESHOLD}
              THEN TRUE
              WHEN COALESCE(s.serve, FALSE) THEN TRUE
              ELSE FALSE
            END AS serve_like,            -- candidate for "serve" at this swing
            sb.bounce_id, sb.bounce_type_raw,
            sb.bx AS serve_bounce_x, sb.by AS serve_bounce_y
          FROM s
          LEFT JOIN pos_at_hit p
            ON p.session_id = s.session_id AND p.swing_id = s.swing_id
          LEFT JOIN center_x cx
            ON cx.session_id = s.session_id
          LEFT JOIN s_bounce sb
            ON sb.session_id = s.session_id AND sb.swing_id = s.swing_id
        ),

        -- mark swings which BEGIN a point (valid serve only)
        serve_begins AS (
          SELECT
            sl.*,
            CASE WHEN sl.serve_like THEN 1 ELSE 0 END AS is_serve_begin
          FROM swing_labeled sl
        ),

        -- assign point numbers (1 + running sum of serve-begins)
        seq AS (
          SELECT
            b.*,
            CASE
              WHEN SUM(is_serve_begin) OVER (PARTITION BY b.session_id) = 0 THEN 1
              ELSE 1 + SUM(is_serve_begin) OVER (
                     PARTITION BY b.session_id
                     ORDER BY b.ord_ts, b.swing_id
                     ROWS UNBOUNDED PRECEDING)
            END AS point_number_d
          FROM serve_begins b
        ),

        -- add shot numbers within each point
        shots AS (
          SELECT
            q.*,
            ROW_NUMBER() OVER (
              PARTITION BY q.session_id, q.point_number_d
              ORDER BY q.ord_ts, q.swing_id
            ) AS shot_number_d,
            MIN(q.ord_ts) OVER (PARTITION BY q.session_id, q.point_number_d) AS point_ts0
          FROM seq q
        ),

        -- first & last swing per point
        pt_first AS (
          SELECT DISTINCT ON (session_id, point_number_d)
            session_id, point_number_d,
            swing_id  AS first_swing_id,
            player_id AS server_id,
            ord_ts    AS first_ord_ts
          FROM shots
          ORDER BY session_id, point_number_d, shot_number_d
        ),
        pt_last AS (
          SELECT DISTINCT ON (session_id, point_number_d)
            session_id, point_number_d,
            swing_id  AS last_swing_id,
            player_id AS last_hitter_id,
            ord_ts    AS last_ord_ts
          FROM shots
          ORDER BY session_id, point_number_d, shot_number_d DESC
        ),

        -- game numbering: new game when server changes
        point_headers AS (
          SELECT
            f.*,
            LAG(f.server_id) OVER (PARTITION BY f.session_id ORDER BY f.point_number_d) AS prev_server_id,
            CASE WHEN LAG(f.server_id) OVER (PARTITION BY f.session_id ORDER BY f.point_number_d)
                      IS DISTINCT FROM f.server_id THEN 1 ELSE 0 END AS new_game_flag
          FROM pt_first f
        ),
        game_numbered AS (
          SELECT
            ph.*,
            1 + SUM(new_game_flag) OVER (PARTITION BY ph.session_id ORDER BY ph.point_number_d
                                         ROWS UNBOUNDED PRECEDING) AS game_number_d
          FROM point_headers ph
        ),
        game_seq AS (
          SELECT
            gn.*,
            ROW_NUMBER() OVER (
              PARTITION BY gn.session_id, gn.game_number_d
              ORDER BY gn.point_number_d
            ) AS point_in_game_d
          FROM game_numbered gn
        ),

        -- serving side from server's x-at-hit relative to center
        first_positions AS (
          SELECT
            sh.session_id, sh.point_number_d,
            p.x_player_at_hit AS server_x_at_first,
            p.y_player_at_hit AS server_y_at_first,
            sh.cx
          FROM shots sh
          JOIN pt_first pf
            ON pf.session_id = sh.session_id AND pf.first_swing_id = sh.swing_id
          LEFT JOIN pos_at_hit p
            ON p.session_id = sh.session_id AND p.swing_id = sh.swing_id
        ),
        serving_side AS (
          SELECT
            fp.session_id, fp.point_number_d,
            CASE WHEN fp.server_x_at_first IS NULL OR fp.cx IS NULL THEN NULL
                 WHEN fp.server_x_at_first >= fp.cx THEN 'deuce'
                 ELSE 'ad'
            END AS serving_side_d,
            CASE WHEN COALESCE(ABS(fp.server_y_at_first),0) >= {BASELINE_Y_THRESHOLD}
                 THEN TRUE ELSE FALSE END AS server_behind_baseline_at_first_d
          FROM first_positions fp
        ),

        -- serve placement bucket (1..8) using *serve* bounce (from first swing only)
        serve_bounce AS (
          SELECT
            pf.session_id, pf.point_number_d,
            sb.bx, sb.by
          FROM pt_first pf
          LEFT JOIN s_bounce sb
            ON sb.session_id = pf.session_id AND sb.swing_id = pf.first_swing_id
        ),
        serve_bucket AS (
          SELECT
            sb.session_id, sb.point_number_d,
            CASE
              WHEN sb.bx IS NULL OR sb.by IS NULL THEN NULL
              ELSE (CASE WHEN sb.by >= 0 THEN 1 ELSE 0 END) * 4
                   + (CASE
                        WHEN sb.bx < -0.5 THEN 1
                        WHEN sb.bx <  0.0 THEN 2
                        WHEN sb.bx <  0.5 THEN 3
                        ELSE 4
                      END)
            END AS serve_bucket_1_8_d
          FROM serve_bounce sb
        )

        SELECT
          -- ========= raw passthrough (exact names from bronze) =========
          sh.session_id,
          ds.session_uid                           AS session_uid_d,
          sh.swing_id,
          sh.player_id,
          dp.sportai_player_uid                   AS player_uid,         -- raw "player uid"
          sh.rally_id,
          sh.start_s, sh.end_s, sh.ball_hit_s,                           -- seconds
          sh.start_ts, sh.end_ts, sh.ball_hit_ts,                        -- timestamps
          sh.ball_hit_x, sh.ball_hit_y,                                  -- hit location (raw)
          sh.ball_speed,                                                  -- raw speed
          sh.swing_type                              AS swing_type_raw,   -- raw type
          sb.bounce_id,
          sb.bounce_type_raw,
          sb.bx                                     AS bounce_x,          -- bounce coords (next-bounce)
          sb.by                                     AS bounce_y,
          ap.x_player_at_hit                         AS player_x_at_hit,   -- player position at hit (raw)
          ap.y_player_at_hit                         AS player_y_at_hit,

          -- ========= derived fields (all *_d) =========
          -- row/shot numbering
          sh.shot_number_d,
          sh.point_number_d,
          gs.game_number_d,
          gs.point_in_game_d,

          -- serve & errors
          CASE WHEN sh.serve_like THEN TRUE ELSE FALSE END              AS serve_d,
          CASE WHEN COALESCE(sh.ball_speed,0) = 0 THEN TRUE ELSE FALSE END AS is_error_d,

          -- serve extras
          sv.serve_bucket_1_8_d,
          ss.serving_side_d,
          ss.server_behind_baseline_at_first_d,

          -- serve counts/faults inside point
          SUM(CASE WHEN sh.serve_like THEN 1 ELSE 0 END) OVER (
              PARTITION BY sh.session_id, sh.point_number_d
          ) AS serve_count_in_point_d,
          SUM(CASE WHEN sh.serve_like AND COALESCE(sh.ball_speed,0)=0 THEN 1 ELSE 0 END) OVER (
              PARTITION BY sh.session_id, sh.point_number_d
          ) AS fault_serves_in_point_d,

          -- depth buckets from |player_y_at_hit|
          CASE
            WHEN ap.y_player_at_hit IS NULL THEN NULL
            WHEN ABS(ap.y_player_at_hit) <  {SHORT_DEPTH_M} THEN 'short'
            WHEN ABS(ap.y_player_at_hit) <= {MID_DEPTH_M}   THEN 'mid'
            ELSE 'long'
          END AS shot_depth_d,

          -- rally location helper (quadrant-ish by side/depth)
          CASE
            WHEN ap.x_player_at_hit IS NULL OR sh.cx IS NULL THEN NULL
            WHEN ap.x_player_at_hit >= sh.cx
              THEN CASE WHEN ABS(ap.y_player_at_hit) <  {SHORT_DEPTH_M} THEN 'A'
                        WHEN ABS(ap.y_player_at_hit) <= {MID_DEPTH_M}   THEN 'B'
                        ELSE 'C' END
            ELSE CASE WHEN ABS(ap.y_player_at_hit) <  {SHORT_DEPTH_M} THEN 'D'
                      WHEN ABS(ap.y_player_at_hit) <= {MID_DEPTH_M}   THEN 'E'
                      ELSE 'F' END
          END AS rally_location_d,

          -- placeholders requested
          NULL::INTEGER AS point_winner_id_d,
          NULL::TEXT    AS score_str_d,
          NULL::TEXT    AS error_type_d

        FROM shots sh
        LEFT JOIN dim_session ds USING (session_id)
        LEFT JOIN dim_player  dp
               ON dp.session_id = sh.session_id AND dp.player_id = sh.player_id
        LEFT JOIN s_bounce    sb
               ON sb.session_id = sh.session_id AND sb.swing_id = sh.swing_id
        LEFT JOIN pos_at_hit  ap
               ON ap.session_id = sh.session_id AND ap.swing_id = sh.swing_id
        LEFT JOIN game_seq    gs
               ON gs.session_id = sh.session_id AND gs.point_number_d = sh.point_number_d
        LEFT JOIN serving_side ss
               ON ss.session_id = sh.session_id AND ss.point_number_d  = sh.point_number_d
        LEFT JOIN serve_bucket sv
               ON sv.session_id = sh.session_id AND sv.point_number_d  = sh.point_number_d

        ORDER BY sh.session_id, sh.point_number_d, sh.shot_number_d, sh.swing_id;
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
