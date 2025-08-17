# db_views.py
from sqlalchemy import text

# --- Views to (re)create ---
VIEW_NAMES = [
    # your originals
    "vw_swing",
    "vw_bounce",
    "vw_rally",
    "vw_ball_position",
    "vw_player_position",

    # new, Power BI–friendly views
    "vw_session_summary",
    "vw_dim_player",
    "vw_fact_swing_enriched",
    "vw_dim_rally_enriched",
    "vw_player_position_1s",
]

CREATE_STMTS = {
    # -----------------------
    # ORIGINAL VIEWS (UNCHANGED)
    # -----------------------
    "vw_swing": """
        CREATE VIEW vw_swing AS
        WITH membership AS (
          SELECT DISTINCT
            ts.session_id,
            'front'::text AS side,
            x::text       AS player_uid
          FROM team_session ts,
               jsonb_array_elements_text(ts.data->'team_front') AS x
          UNION
          SELECT DISTINCT
            ts.session_id,
            'back'::text AS side,
            x::text      AS player_uid
          FROM team_session ts,
               jsonb_array_elements_text(ts.data->'team_back') AS x
        )
        SELECT
          s.swing_id,
          s.session_id,
          ds.session_uid,
          s.player_id,
          dp.full_name AS player_name,
          dp.sportai_player_uid AS player_uid,
          COALESCE(dp.full_name, dp.sportai_player_uid) AS player_label,
          m.side AS player_side,
          CASE
            WHEN m.side IS NOT NULL
              THEN COALESCE(dp.full_name, dp.sportai_player_uid) || ' (' || m.side || ')'
            ELSE COALESCE(dp.full_name, dp.sportai_player_uid)
          END AS player_display,
          s.sportai_swing_uid,
          s.start_s, s.end_s, s.ball_hit_s,
          s.start_ts, s.end_ts, s.ball_hit_ts,
          s.ball_hit_x, s.ball_hit_y,
          s.ball_speed, s.ball_player_distance,
          COALESCE(s.is_in_rally, FALSE) AS is_in_rally,
          s.serve, s.serve_type,
          s.meta
        FROM fact_swing s
        LEFT JOIN dim_player  dp ON dp.player_id   = s.player_id
        LEFT JOIN dim_session ds ON ds.session_id   = s.session_id
        LEFT JOIN membership  m  ON m.session_id    = s.session_id
                                AND m.player_uid    = dp.sportai_player_uid;
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
            b.x, b.y, b.bounce_type
        FROM fact_bounce b
        LEFT JOIN dim_player  dp ON dp.player_id   = b.hitter_player_id
        LEFT JOIN dim_session ds ON ds.session_id  = b.session_id;
    """,

    "vw_rally": """
        CREATE VIEW vw_rally AS
        SELECT
            r.rally_id,
            r.session_id,
            ds.session_uid,
            r.rally_number,
            r.start_s, r.end_s,
            r.start_ts, r.end_ts
        FROM dim_rally r
        LEFT JOIN dim_session ds ON ds.session_id = r.session_id;
    """,

    "vw_ball_position": """
        CREATE VIEW vw_ball_position AS
        SELECT
            p.id,
            p.session_id,
            ds.session_uid,
            p.ts_s, p.ts, p.x, p.y
        FROM fact_ball_position p
        LEFT JOIN dim_session ds ON ds.session_id = p.session_id;
    """,

    "vw_player_position": """
        CREATE VIEW vw_player_position AS
        SELECT
            p.id,
            p.session_id,
            ds.session_uid,
            p.player_id,
            dp.full_name AS player_name,
            p.ts_s, p.ts, p.x, p.y
        FROM fact_player_position p
        LEFT JOIN dim_session ds ON ds.session_id = p.session_id
        LEFT JOIN dim_player  dp ON dp.player_id  = p.player_id;
    """,

    # -----------------------
    # NEW VIEWS (FOR POWER BI)
    # -----------------------
    "vw_session_summary": """
        CREATE VIEW vw_session_summary AS
        SELECT
          s.session_id,
          s.session_uid,
          s.session_date,
          s.fps,
          (SELECT COUNT(*) FROM dim_player dp  WHERE dp.session_id=s.session_id) AS players,
          (SELECT COUNT(*) FROM dim_rally  dr  WHERE dr.session_id=s.session_id) AS rallies,
          (SELECT COUNT(*) FROM fact_swing fs  WHERE fs.session_id=s.session_id) AS swings,
          (SELECT COUNT(*) FROM fact_bounce b  WHERE b.session_id=s.session_id) AS ball_bounces,
          (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id=s.session_id) AS ball_positions,
          (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id=s.session_id) AS player_positions
        FROM dim_session s;
    """,

    "vw_dim_player": """
        CREATE VIEW vw_dim_player AS
        SELECT
          dp.player_id,
          dp.session_id,
          s.session_uid,
          (s.session_uid || ':' || dp.sportai_player_uid) AS player_key,
          dp.sportai_player_uid AS player_uid,
          COALESCE(dp.full_name, dp.sportai_player_uid) AS player_name,
          dp.handedness,
          dp.age,
          dp.utr
        FROM dim_player dp
        JOIN dim_session s ON s.session_id = dp.session_id;
    """,

    "vw_fact_swing_enriched": """
        CREATE VIEW vw_fact_swing_enriched AS
        SELECT
          fs.session_id,
          s.session_uid,
          dp.player_id,
          dp.sportai_player_uid AS player_uid,
          (s.session_uid || ':' || dp.sportai_player_uid) AS player_key,
          fs.start_s,
          fs.end_s,
          fs.ball_hit_s,
          fs.ball_hit_x, fs.ball_hit_y,
          fs.serve, fs.serve_type
        FROM fact_swing fs
        LEFT JOIN dim_player dp ON dp.player_id = fs.player_id
        JOIN dim_session s ON s.session_id = fs.session_id;
    """,

    "vw_dim_rally_enriched": """
        CREATE VIEW vw_dim_rally_enriched AS
        SELECT
          r.session_id,
          s.session_uid,
          r.rally_number,
          r.start_s,
          r.end_s,
          (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=r.session_id AND b.rally_id=r.rally_id) AS bounces
        FROM dim_rally r
        JOIN dim_session s ON s.session_id = r.session_id;
    """,

    "vw_player_position_1s": """
        CREATE VIEW vw_player_position_1s AS
        WITH ranked AS (
          SELECT
            p.session_id,
            s.session_uid,
            dp.player_id,
            dp.sportai_player_uid AS player_uid,
            (s.session_uid || ':' || dp.sportai_player_uid) AS player_key,
            floor(p.ts_s)::int AS t_sec,
            p.ts_s, p.x, p.y,
            row_number() OVER (
              PARTITION BY p.session_id, p.player_id, floor(p.ts_s)
              ORDER BY p.ts_s
            ) AS rn
          FROM fact_player_position p
          JOIN dim_player  dp ON dp.player_id   = p.player_id
          JOIN dim_session s  ON s.session_id   = p.session_id
        )
        SELECT session_id, session_uid, player_id, player_uid, player_key, t_sec, ts_s, x, y
        FROM ranked WHERE rn=1;
    """,
}

# Helpful indexes (idempotent)
INDEX_STMTS = [
    "CREATE INDEX IF NOT EXISTS fact_swing_sid_bhs      ON fact_swing (session_id, ball_hit_s);",
    "CREATE INDEX IF NOT EXISTS fact_player_pos_sid_pid ON fact_player_position (session_id, player_id, ts_s);",
    "CREATE INDEX IF NOT EXISTS fact_bounce_sid_s       ON fact_bounce (session_id, bounce_s);",
    "CREATE INDEX IF NOT EXISTS dim_rally_sid_num       ON dim_rally (session_id, rally_number);",
    "CREATE INDEX IF NOT EXISTS dim_player_sid_uid      ON dim_player (session_id, sportai_player_uid);",
]

# ---- helpers ----
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
        WHERE n.nspname='public' AND c.relname=:name
        LIMIT 1
    """), {"name": name}).first()
    return row[0] if row else None  # 'v' view, 'm' matview, None

def _drop_view_or_matview(conn, name):
    kind = _get_relkind(conn, name)
    if kind == 'v':
        conn.execute(text(f"DROP VIEW IF EXISTS {name} CASCADE;"))
    elif kind == 'm':
        conn.execute(text(f"DROP MATERIALIZED VIEW IF EXISTS {name} CASCADE;"))

def _preflight_or_raise(conn):
    required_tables = [
        "dim_session", "dim_player", "dim_rally",
        "fact_swing", "fact_bounce", "fact_ball_position", "fact_player_position"
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

    checks = [
        ("dim_session", "session_uid"),
        ("dim_player", "full_name"),
        ("fact_swing", "start_s"),
        ("fact_swing", "start_ts"),
        ("fact_swing", "ball_hit_ts"),
        ("fact_bounce", "bounce_s"),
        ("fact_bounce", "bounce_ts"),
        ("fact_ball_position", "ts"),
        ("fact_player_position", "ts"),
    ]
    missing_cols = [(t,c) for (t,c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t,c) in missing_cols])
        raise RuntimeError(f"Missing required columns before creating views: {msg}")

def run_views(engine):
    with engine.begin() as conn:
        _preflight_or_raise(conn)
        # Drop then recreate (stable order)
        for name in VIEW_NAMES:
            _drop_view_or_matview(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))
        # Helpful indexes (no-op if already present)
        for stmt in INDEX_STMTS:
            conn.execute(text(stmt))
