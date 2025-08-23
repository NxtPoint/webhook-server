# db_views.py
from sqlalchemy import text

# Views required for the transaction log + minimal helpers
VIEW_NAMES = [
    # Base helpers
    "vw_swing",
    "vw_rally",
    "vw_bounce",
    "vw_player_position",

    # Ordering helper
    "vw_shot_order",

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
          dp.full_name AS player_name,
          dp.sportai_player_uid AS player_uid,
          s.rally_id,
          s.start_s, s.end_s, s.ball_hit_s,
          s.start_ts, s.end_ts, s.ball_hit_ts,
          s.ball_hit_x, s.ball_hit_y,
          s.ball_speed, s.ball_player_distance,
          COALESCE(s.is_in_rally, FALSE) AS is_in_rally,
          s.serve, s.serve_type,
          s.swing_type,            -- if present in fact_swing (kept for traceability)
          s.meta                   -- raw per-swing json for deep dives
        FROM fact_swing s
        LEFT JOIN dim_player  dp ON dp.player_id   = s.player_id
        LEFT JOIN dim_session ds ON ds.session_id   = s.session_id;
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
            b.bounce_type  -- expect values like 'in','out','net','long','wide'
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

    # ---------- ORDERING HELPER ----------
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
          -- server is the first swing flagged serve=TRUE, else fallback to first hitter
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
          -- first bounce after the last swing (within same rally)
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
                  LIMIT 1)   -- opponent wins
            ELSE fl.last_hitter_id           -- last hitter wins (ball not returned)
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
          SELECT
            s.swing_id,
            s.session_id,
            s.session_uid,
            s.rally_id,
            r.rally_number AS point_number,
            so.shot_number_in_point,
            s.player_id,
            s.player_name,
            s.player_uid,
            s.serve, s.serve_type,
            s.start_s, s.end_s, s.ball_hit_s,
            s.start_ts, s.end_ts, s.ball_hit_ts,
            s.ball_hit_x, s.ball_hit_y,
            s.ball_speed, s.ball_player_distance,
            s.meta
          FROM vw_swing s
          JOIN vw_rally r
            ON r.session_id = s.session_id AND r.rally_id = s.rally_id
          JOIN vw_shot_order so
            ON so.session_id = s.session_id AND so.rally_id = s.rally_id AND so.swing_id = s.swing_id
        ),
        player_loc AS (
          -- nearest player position at hit time
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
            ORDER BY ABS(EXTRACT(EPOCH FROM (p.ts - b.ball_hit_ts)))
            LIMIT 1
          ) pp ON TRUE
        ),
        first_bounce_after_hit AS (
          SELECT
            b.swing_id,
            bx.bounce_id,
            bx.x AS bounce_x,
            bx.y AS bounce_y,
            bx.bounce_type
          FROM base b
          LEFT JOIN LATERAL (
            SELECT bb.*
            FROM vw_bounce bb
            WHERE bb.session_id = b.session_id
              AND bb.rally_id   = b.rally_id
              AND bb.bounce_ts >= b.ball_hit_ts
            ORDER BY bb.bounce_ts
            LIMIT 1
          ) bx ON TRUE
        ),
        classify AS (
          SELECT
            b.*,
            pl.player_x_at_hit, pl.player_y_at_hit,
            fb.bounce_id, fb.bounce_x, fb.bounce_y, fb.bounce_type,

            -- Shot result: prefer explicit bounce_type if present
            CASE
              WHEN fb.bounce_type IN ('out','net','long','wide') THEN 'out'
              WHEN fb.bounce_type IS NULL THEN NULL
              ELSE 'in'
            END AS shot_result,

            -- Depth label (tune thresholds to your coordinate system)
            CASE
              WHEN fb.bounce_type IN ('net') THEN 'net'
              WHEN fb.bounce_type IN ('long','wide','out') THEN fb.bounce_type
              WHEN fb.bounce_y IS NULL THEN NULL
              WHEN fb.bounce_y <= -2.5 THEN 'deep'
              WHEN fb.bounce_y BETWEEN -2.5 AND 2.5 THEN 'mid'
              ELSE 'short'
            END AS shot_description_depth,

            -- Rally box A-D (quadrant-style; adjust to your axes)
            CASE
              WHEN b.serve THEN NULL
              WHEN fb.bounce_x IS NULL OR fb.bounce_y IS NULL THEN NULL
              WHEN fb.bounce_x < 0 AND fb.bounce_y >= 0 THEN 'A'
              WHEN fb.bounce_x >= 0 AND fb.bounce_y >= 0 THEN 'B'
              WHEN fb.bounce_x < 0 AND fb.bounce_y < 0 THEN 'C'
              WHEN fb.bounce_x >= 0 AND fb.bounce_y < 0 THEN 'D'
            END AS rally_box_ad,

            -- Serve target 1–8 (only for serves; simple split example)
            CASE
              WHEN b.serve IS NOT TRUE THEN NULL
              WHEN fb.bounce_x IS NULL OR fb.bounce_y IS NULL THEN NULL
              ELSE
                CASE
                  WHEN fb.bounce_y >= 0 THEN   -- deuce court 1–4
                    CASE
                      WHEN fb.bounce_x <  -1.5 THEN 1
                      WHEN fb.bounce_x BETWEEN -1.5 AND -0.5 THEN 2
                      WHEN fb.bounce_x BETWEEN -0.5 AND  0.5 THEN 3
                      ELSE 4
                    END
                  ELSE                          -- ad court 5–8
                    CASE
                      WHEN fb.bounce_x <  -1.5 THEN 5
                      WHEN fb.bounce_x BETWEEN -1.5 AND -0.5 THEN 6
                      WHEN fb.bounce_x BETWEEN -0.5 AND  0.5 THEN 7
                      ELSE 8
                    END
                END
            END AS serve_target_1_8,

            -- Simple shot confidence if present in meta JSON
            NULLIF((b.meta->>'confidence'), '')::float AS shot_confidence
          FROM base b
          LEFT JOIN player_loc pl ON pl.swing_id = b.swing_id
          LEFT JOIN first_bounce_after_hit fb ON fb.swing_id = b.swing_id
        )
        SELECT
          c.session_uid,
          c.session_id,
          c.rally_id,
          c.point_number,                        -- Point #
          c.shot_number_in_point AS shot_number, -- Shot # within point
          c.swing_id,

          -- Player who executed the shot
          c.player_id,
          c.player_name,
          c.player_uid,

          -- Result & description
          c.shot_result,                         -- 'in' or 'out' (net/long/wide treated as out)
          c.shot_description_depth,              -- 'short'/'mid'/'deep'/'net'/'long'/'wide'

          -- Positions / classifications
          c.serve_type,
          c.serve,
          c.serve_target_1_8,                    -- serves only
          c.rally_box_ad,                        -- rally shots only (A–D)
          c.player_x_at_hit, c.player_y_at_hit,
          c.ball_hit_x, c.ball_hit_y,
          c.bounce_x AS ball_bounce_x, c.bounce_y AS ball_bounce_y,

          -- Timing
          c.start_s, c.end_s, c.ball_hit_s,
          c.start_ts, c.end_ts, c.ball_hit_ts,

          -- Extras
          c.ball_speed,
          c.ball_player_distance,
          c.shot_confidence
        FROM classify c
        ORDER BY c.session_uid, c.point_number, c.shot_number_in_point;
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
        "fact_swing", "fact_bounce", "fact_player_position"
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

    # Minimal columns used by these views
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
    ]
    missing_cols = [(t,c) for (t,c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t,c) in missing_cols])
        raise RuntimeError(f"Missing required columns before creating views: {msg}")

def run_views(engine):
    with engine.begin() as conn:
        _preflight_or_raise(conn)
        # drop first to avoid dependency issues
        for name in VIEW_NAMES:
            _drop_view_or_matview(conn, name)
        # create in the declared order
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))
