# db_views.py — PURE silver from bronze; gold reads ONLY silver (no inference)
from sqlalchemy import text
from typing import List

VIEW_SQL_STMTS: List[str] = []  # populated from VIEW_NAMES/CREATE_STMTS

# -------- Bronze helper: make raw_ingest table if missing (safe to re-run) --------
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

# -------- Views to build (order matters) --------
# SILVER = strict pass-through from BRONZE (no edits)
# GOLD   = reads only SILVER; rally-based ordering; no inference/backfills
VIEW_NAMES = [
    # SILVER (pure)
    "vw_swing",
    "vw_bounce",
    "vw_ball_position",
    "vw_player_position",

    # GOLD (pure)
    "vw_shot_order_gold",       # rally-only ordering
    "vw_point_log",             # primary BI gold view (pure)
    "vw_point_shot_log_gold",   # alias -> vw_point_log (kept for compatibility)
]

CREATE_STMTS = {
    # ---------------- SILVER (PURE passthrough + labels) ----------------
    "vw_swing": """
        CREATE OR REPLACE VIEW vw_swing AS
        SELECT
          ds.session_uid,
          fs.session_id,
          fs.swing_id,
          fs.player_id,
          dp.full_name          AS player_name,   -- raw label
          dp.sportai_player_uid AS player_uid,    -- raw UID
          fs.rally_id,

          -- raw times
          fs.start_s, fs.end_s, fs.ball_hit_s,
          fs.start_ts, fs.end_ts, fs.ball_hit_ts,

          -- raw XY at hit
          fs.ball_hit_x, fs.ball_hit_y,

          -- raw metrics / flags
          fs.ball_speed,
          fs.ball_player_distance,
          fs.is_in_rally,
          fs.serve, fs.serve_type, fs.swing_type,

          -- raw metadata
          fs.meta
        FROM fact_swing fs
        LEFT JOIN dim_session ds ON ds.session_id = fs.session_id
        LEFT JOIN dim_player  dp ON dp.player_id  = fs.player_id;
    """,

    "vw_bounce": """
        CREATE OR REPLACE VIEW vw_bounce AS
        SELECT
          ds.session_uid,
          b.session_id,
          b.bounce_id,
          b.hitter_player_id,
          dp.full_name          AS hitter_name,       -- raw label
          dp.sportai_player_uid AS hitter_player_uid, -- raw UID
          b.rally_id,

          -- raw time & XY
          b.bounce_s, b.bounce_ts,
          b.x, b.y,

          -- raw classification from SportAI
          b.bounce_type
        FROM fact_bounce b
        LEFT JOIN dim_session ds ON ds.session_id = b.session_id
        LEFT JOIN dim_player  dp ON dp.player_id  = b.hitter_player_id;
    """,

    "vw_ball_position": """
        CREATE OR REPLACE VIEW vw_ball_position AS
        SELECT
          ds.session_uid,
          p.session_id,
          p.ts_s, p.ts,
          p.x, p.y
        FROM fact_ball_position p
        LEFT JOIN dim_session ds ON ds.session_id = p.session_id;
    """,

    "vw_player_position": """
        CREATE OR REPLACE VIEW vw_player_position AS
        SELECT
          ds.session_uid,
          u.session_id,
          u.player_id,
          dp.full_name          AS player_name,   -- raw label
          dp.sportai_player_uid AS player_uid,    -- raw UID
          u.ts_s, u.ts,
          u.x, u.y
        FROM fact_player_position u
        LEFT JOIN dim_session ds ON ds.session_id = u.session_id
        LEFT JOIN dim_player  dp ON dp.player_id  = u.player_id;
    """,

    # ---------------- GOLD (no inference; uses only SILVER) ----------------
    "vw_shot_order_gold": """
        CREATE OR REPLACE VIEW vw_shot_order_gold AS
        SELECT
          fs.session_id,
          fs.rally_id,
          dr.rally_number,
          fs.swing_id,
          ROW_NUMBER() OVER (
            PARTITION BY fs.session_id, fs.rally_id
            ORDER BY COALESCE(fs.ball_hit_s, fs.start_s), fs.swing_id
          ) AS shot_number_in_point
        FROM vw_swing fs
        JOIN dim_rally dr
          ON dr.session_id = fs.session_id
         AND dr.rally_id   = fs.rally_id
        WHERE fs.rally_id IS NOT NULL;
    """,

    # Primary BI gold view (pure, what Power BI reads)
    "vw_point_log": """
        CREATE OR REPLACE VIEW vw_point_log AS
        WITH s AS (
          SELECT * FROM vw_swing
        ),
        ord AS (
          SELECT session_id, rally_id, rally_number, swing_id, shot_number_in_point
          FROM vw_shot_order_gold
        ),
        b_after AS (
          SELECT s2.swing_id,
                 bx.bounce_id,
                 bx.bounce_ts,
                 bx.x AS bounce_x,
                 bx.y AS bounce_y,
                 bx.bounce_type
          FROM s s2
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM vw_bounce b
            WHERE b.session_id = s2.session_id
              AND b.rally_id   = s2.rally_id
              AND b.bounce_ts  >= s2.ball_hit_ts
            ORDER BY b.bounce_ts
            LIMIT 1
          ) bx ON TRUE
        ),
        pp_exact AS (
          SELECT s2.swing_id,
                 p.x AS player_x_at_hit,
                 p.y AS player_y_at_hit
          FROM s s2
          LEFT JOIN vw_player_position p
            ON p.session_id = s2.session_id
           AND p.player_id  = s2.player_id
           AND p.ts         = s2.ball_hit_ts
        )
        SELECT
          -- ids / linking
          s.session_uid,
          s.session_id,
          s.rally_id,
          ord.rally_number         AS point_number,
          ord.shot_number_in_point AS shot_number,
          s.swing_id,

          -- player
          s.player_id, s.player_name, s.player_uid,

          -- raw swing fields
          s.serve, s.serve_type,
          s.swing_type AS swing_type_raw,

          -- raw metrics
          s.ball_speed,
          s.ball_player_distance,

          -- raw times
          s.start_s, s.end_s, s.ball_hit_s,
          s.start_ts, s.end_ts, s.ball_hit_ts,

          -- raw hit XY
          s.ball_hit_x, s.ball_hit_y,

          -- raw first bounce after hit (may be NULL)
          b_after.bounce_id,
          b_after.bounce_x  AS ball_bounce_x,
          b_after.bounce_y  AS ball_bounce_y,
          b_after.bounce_type AS bounce_type_raw,

          -- raw error flag from bounce_type only
          CASE
            WHEN b_after.bounce_type IN ('out','net','long','wide') THEN TRUE
            WHEN b_after.bounce_type IS NULL THEN NULL
            ELSE FALSE
          END AS is_error,
          CASE
            WHEN b_after.bounce_type IN ('out','net','long','wide') THEN b_after.bounce_type
            ELSE NULL
          END AS error_type,

          -- exact player pos at hit (may be NULL)
          pp_exact.player_x_at_hit,
          pp_exact.player_y_at_hit,

          -- placeholders (remain NULL)
          NULL::text AS baseline_zone_abcd,
          NULL::int  AS serve_location_1_8
        FROM s
        LEFT JOIN ord      ON ord.session_id = s.session_id AND ord.swing_id = s.swing_id
        LEFT JOIN b_after  ON b_after.swing_id = s.swing_id
        LEFT JOIN pp_exact ON pp_exact.swing_id = s.swing_id
        ORDER BY s.session_uid, point_number NULLS LAST, shot_number NULLS LAST, s.swing_id;
    """,

    # Compatibility alias (so both names are available)
    "vw_point_shot_log_gold": """
        CREATE OR REPLACE VIEW vw_point_shot_log_gold AS
        SELECT * FROM vw_point_log;
    """,
}

# ---------- helpers ----------
def _drop_any(conn, name: str):
    # robust: try to drop view, matview, or table — whatever exists — without caring about relkind
    conn.execute(text(f"DROP VIEW IF EXISTS {name} CASCADE;"))
    conn.execute(text(f"DROP MATERIALIZED VIEW IF EXISTS {name} CASCADE;"))
    conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE;"))

def _table_exists(conn, t: str) -> bool:
    return conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name=:t
        LIMIT 1
    """), {"t": t}).first() is not None

def _column_exists(conn, t: str, c: str) -> bool:
    return conn.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t AND column_name=:c
        LIMIT 1
    """), {"t": t, "c": c}).first() is not None

def _preflight_or_raise(conn):
    required_tables = [
        "dim_session", "dim_player", "dim_rally",
        "fact_swing", "fact_bounce",
        "fact_player_position", "fact_ball_position",
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

    checks = [
        ("dim_session", "session_uid"),
        ("dim_player", "full_name"),
        ("dim_player", "sportai_player_uid"),
        ("dim_rally", "rally_id"),
        ("dim_rally", "rally_number"),

        ("fact_swing", "swing_id"),
        ("fact_swing", "session_id"),
        ("fact_swing", "player_id"),
        ("fact_swing", "rally_id"),
        ("fact_swing", "start_s"),
        ("fact_swing", "end_s"),
        ("fact_swing", "ball_hit_s"),
        ("fact_swing", "start_ts"),
        ("fact_swing", "end_ts"),
        ("fact_swing", "ball_hit_ts"),
        ("fact_swing", "ball_hit_x"),
        ("fact_swing", "ball_hit_y"),
        ("fact_swing", "ball_speed"),
        ("fact_swing", "ball_player_distance"),
        ("fact_swing", "is_in_rally"),
        ("fact_swing", "serve"),
        ("fact_swing", "serve_type"),
        ("fact_swing", "swing_type"),
        ("fact_swing", "meta"),

        ("fact_bounce", "bounce_id"),
        ("fact_bounce", "session_id"),
        ("fact_bounce", "hitter_player_id"),
        ("fact_bounce", "rally_id"),
        ("fact_bounce", "bounce_s"),
        ("fact_bounce", "bounce_ts"),
        ("fact_bounce", "x"),
        ("fact_bounce", "y"),
        ("fact_bounce", "bounce_type"),

        ("fact_player_position", "session_id"),
        ("fact_player_position", "player_id"),
        ("fact_player_position", "ts_s"),
        ("fact_player_position", "ts"),
        ("fact_player_position", "x"),
        ("fact_player_position", "y"),

        ("fact_ball_position", "session_id"),
        ("fact_ball_position", "ts_s"),
        ("fact_ball_position", "ts"),
        ("fact_ball_position", "x"),
        ("fact_ball_position", "y"),
    ]
    missing_cols = [(t,c) for (t,c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t,c) in missing_cols])
        raise RuntimeError(f"Missing required columns before creating views: {msg}")

# ---------- apply all views ----------
def _apply_views(engine):
    """Drops & recreates all views listed in VIEW_NAMES."""
    global VIEW_SQL_STMTS
    VIEW_SQL_STMTS = [CREATE_STMTS[name] for name in VIEW_NAMES]

    with engine.begin() as conn:
        _ensure_raw_ingest(conn)
        _preflight_or_raise(conn)

        # DROP in reverse dependency order (robust) then CREATE in forward order
        for name in reversed(VIEW_NAMES):
            _drop_any(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))

# Back-compat exports
init_views = _apply_views
run_views  = _apply_views

__all__ = ["init_views", "run_views", "VIEW_SQL_STMTS", "VIEW_NAMES", "CREATE_STMTS"]
