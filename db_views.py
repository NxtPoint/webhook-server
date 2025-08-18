# db_views.py
from sqlalchemy import text

VIEW_NAMES = [
    # base convenience views
    "vw_swing",
    "vw_bounce",
    "vw_rally",
    "vw_ball_position",
    "vw_player_position",
    # analytics views (with surrogate keys for BI)
    "vw_session_summary",
    "vw_player_summary",
    "vw_rally_summary",
    "vw_shot_tempo",
    "vw_player_movement",
]

CREATE_STMTS = {
    # ----------------------- BASE VIEWS -----------------------
    "vw_swing": """
        CREATE VIEW vw_swing AS
        WITH membership AS (
          SELECT DISTINCT ts.session_id, 'front'::text AS side, x::text AS player_uid
          FROM team_session ts, jsonb_array_elements_text(ts.data->'team_front') AS x
          UNION
          SELECT DISTINCT ts.session_id, 'back'::text AS side, x::text AS player_uid
          FROM team_session ts, jsonb_array_elements_text(ts.data->'team_back') AS x
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
        LEFT JOIN dim_player  dp ON dp.player_id  = s.player_id
        LEFT JOIN dim_session ds ON ds.session_id  = s.session_id
        LEFT JOIN membership  m  ON m.session_id   = s.session_id
                                AND m.player_uid   = dp.sportai_player_uid;
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
        LEFT JOIN dim_player  dp ON dp.player_id  = b.hitter_player_id
        LEFT JOIN dim_session ds ON ds.session_id = b.session_id;
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
            dp.sportai_player_uid AS player_uid,
            p.ts_s, p.ts, p.x, p.y
        FROM fact_player_position p
        LEFT JOIN dim_session ds ON ds.session_id = p.session_id
        LEFT JOIN dim_player  dp ON dp.player_id  = p.player_id;
    """,

    # ----------------------- ANALYTICS VIEWS (with keys) -----------------------

    "vw_session_summary": """
        CREATE VIEW vw_session_summary AS
        WITH s AS (
          SELECT session_id, session_uid, COALESCE(fps, 0)::float AS fps
          FROM dim_session
        ),
        r AS (
          SELECT session_id,
                 COUNT(*) AS rallies,
                 AVG(GREATEST(0, end_s - start_s))::float AS avg_rally_duration_s,
                 PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY GREATEST(0, end_s - start_s))::float AS med_rally_duration_s
          FROM dim_rally
          GROUP BY 1
        ),
        sw_counts AS (
          SELECT fs.session_id,
                 COUNT(*)::int AS swings,
                 SUM(CASE WHEN COALESCE(fs.serve,false) THEN 1 ELSE 0 END)::int AS serve_swings
          FROM fact_swing fs
          GROUP BY 1
        ),
        sw_tempo_base AS (
          SELECT fs.session_id,
                 LEAD(fs.ball_hit_s) OVER (PARTITION BY fs.session_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s AS tempo_gap
          FROM fact_swing fs
          WHERE fs.ball_hit_s IS NOT NULL
        ),
        sw_tempo AS (
          SELECT session_id,
                 AVG(tempo_gap)::float AS avg_tempo_s,
                 PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY tempo_gap)::float AS p75_tempo_s,
                 PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY tempo_gap)::float AS p90_tempo_s
          FROM sw_tempo_base
          WHERE tempo_gap IS NOT NULL AND tempo_gap <> 0
          GROUP BY 1
        ),
        rs AS (
          SELECT fs.session_id, dr.rally_id, COUNT(*)::int AS shots_in_rally
          FROM fact_swing fs
          JOIN dim_rally dr
            ON dr.session_id = fs.session_id
           AND COALESCE(fs.ball_hit_s, fs.start_s) BETWEEN dr.start_s AND dr.end_s
          GROUP BY 1,2
        ),
        rs_agg AS (
          SELECT session_id,
                 AVG(shots_in_rally)::float AS avg_rally_shots,
                 PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY shots_in_rally)::float AS med_rally_shots
          FROM rs
          GROUP BY 1
        ),
        b AS (
          SELECT session_id,
                 COUNT(*)::int AS bounces,
                 SUM(CASE WHEN y <= -2.5 THEN 1 ELSE 0 END)::int AS deep_back,
                 SUM(CASE WHEN y > -2.5 AND y < 2.5 THEN 1 ELSE 0 END)::int AS mid_court,
                 SUM(CASE WHEN y >= 2.5 THEN 1 ELSE 0 END)::int AS near_net
          FROM fact_bounce
          GROUP BY 1
        )
        SELECT
          s.session_uid,
          r.rallies,
          sw_counts.swings,
          COALESCE(sw_counts.serve_swings,0) AS serve_swings,
          (COALESCE(sw_counts.serve_swings,0)::float / NULLIF(sw_counts.swings,0))::float AS pct_serve_swings,
          rs_agg.avg_rally_shots,
          rs_agg.med_rally_shots,
          r.avg_rally_duration_s,
          r.med_rally_duration_s,
          sw_tempo.avg_tempo_s,
          sw_tempo.p75_tempo_s,
          sw_tempo.p90_tempo_s,
          COALESCE(b.bounces,0) AS bounces,
          b.deep_back, b.mid_court, b.near_net
        FROM s
        LEFT JOIN r         ON r.session_id         = s.session_id
        LEFT JOIN sw_counts ON sw_counts.session_id = s.session_id
        LEFT JOIN sw_tempo  ON sw_tempo.session_id  = s.session_id
        LEFT JOIN rs_agg    ON rs_agg.session_id    = s.session_id
        LEFT JOIN b         ON b.session_id         = s.session_id;
    """,

    # Add a composite key for per-session players
    "vw_player_summary": """
        CREATE VIEW vw_player_summary AS
        WITH membership AS (
          SELECT DISTINCT ts.session_id, 'front'::text AS side, x::text AS player_uid
          FROM team_session ts, jsonb_array_elements_text(ts.data->'team_front') AS x
          UNION
          SELECT DISTINCT ts.session_id, 'back'::text AS side, x::text AS player_uid
          FROM team_session ts, jsonb_array_elements_text(ts.data->'team_back') AS x
        ),
        players AS (
          SELECT dp.session_id, dp.player_id, dp.sportai_player_uid, dp.full_name
          FROM dim_player dp
        ),
        sw_counts AS (
          SELECT fs.session_id, fs.player_id,
                 COUNT(*)::int AS swings,
                 SUM(CASE WHEN COALESCE(fs.serve,false) THEN 1 ELSE 0 END)::int AS serve_swings
          FROM fact_swing fs
          GROUP BY 1,2
        ),
        sw_tempo_base AS (
          SELECT fs.session_id, fs.player_id,
                 LEAD(fs.ball_hit_s) OVER (PARTITION BY fs.session_id, fs.player_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s AS tempo_gap
          FROM fact_swing fs
          WHERE fs.ball_hit_s IS NOT NULL
        ),
        sw_tempo AS (
          SELECT session_id, player_id,
                 AVG(tempo_gap)::float AS avg_tempo_s,
                 PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY tempo_gap)::float AS p75_tempo_s,
                 PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY tempo_gap)::float AS p90_tempo_s
          FROM sw_tempo_base
          WHERE tempo_gap IS NOT NULL AND tempo_gap <> 0
          GROUP BY 1,2
        )
        SELECT
          ds.session_uid,
          p.sportai_player_uid AS player_uid,
          (ds.session_uid || '|' || COALESCE(p.sportai_player_uid,'')) AS session_player_key,
          COALESCE(p.full_name, p.sportai_player_uid) AS player_name,
          m.side AS player_side,
          COALESCE(sc.swings,0) AS swings,
          COALESCE(sc.serve_swings,0) AS serve_swings,
          (COALESCE(sc.serve_swings,0)::float / NULLIF(sc.swings,0))::float AS pct_serve_swings,
          st.avg_tempo_s, st.p75_tempo_s, st.p90_tempo_s
        FROM players p
        JOIN dim_session ds ON ds.session_id = p.session_id
        LEFT JOIN sw_counts sc ON sc.session_id=p.session_id AND sc.player_id=p.player_id
        LEFT JOIN sw_tempo  st ON st.session_id=p.session_id AND st.player_id=p.player_id
        LEFT JOIN membership m  ON m.session_id=p.session_id AND m.player_uid = p.sportai_player_uid;
    """,

    "vw_rally_summary": """
        CREATE VIEW vw_rally_summary AS
        SELECT
          ds.session_uid,
          (ds.session_uid || '|' || r.rally_number::text) AS rally_key,
          r.rally_number,
          r.start_s,
          r.end_s,
          (SELECT COUNT(*) FROM fact_bounce b
            WHERE b.session_id=r.session_id AND b.rally_id=r.rally_id)    AS bounces,
          (SELECT COUNT(*) FROM fact_swing fs
            WHERE fs.session_id=r.session_id
              AND COALESCE(fs.ball_hit_s,fs.start_s) BETWEEN r.start_s AND r.end_s) AS shots
        FROM dim_rally r
        JOIN dim_session ds ON ds.session_id=r.session_id
        ORDER BY ds.session_uid, r.rally_number;
    """,

    # Shot-level with keys (row_id + composite session_player_key)
    "vw_shot_tempo": """
        CREATE VIEW vw_shot_tempo AS
        SELECT
          ds.session_uid,
          dp.sportai_player_uid AS player_uid,
          (ds.session_uid || '|' || COALESCE(dp.sportai_player_uid,'')) AS session_player_key,
          fs.ball_hit_s AS shot_s,
          -- stable row id within (session, player)
          ROW_NUMBER() OVER (PARTITION BY fs.session_id, fs.player_id ORDER BY fs.ball_hit_s) AS row_id,
          LEAD(fs.ball_hit_s) OVER (PARTITION BY fs.session_id, fs.player_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s AS tempo_gap
        FROM fact_swing fs
        JOIN dim_session ds ON ds.session_id=fs.session_id
        LEFT JOIN dim_player dp ON dp.player_id=fs.player_id
        WHERE fs.ball_hit_s IS NOT NULL;
    """,

    # Movement with key
    "vw_player_movement": """
        CREATE VIEW vw_player_movement AS
        WITH base AS (
          SELECT
            p.session_id, p.player_id, p.ts_s, p.x, p.y,
            LAG(p.x)  OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS prev_x,
            LAG(p.y)  OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS prev_y,
            LAG(p.ts_s) OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS prev_ts
          FROM fact_player_position p
        ),
        steps AS (
          SELECT
            session_id, player_id, ts_s,
            sqrt(POWER(x - prev_x, 2) + POWER(y - prev_y, 2)) AS step_dist,
            (ts_s - prev_ts)                                  AS dt
          FROM base
          WHERE prev_x IS NOT NULL AND prev_y IS NOT NULL AND prev_ts IS NOT NULL
        )
        SELECT
          ds.session_uid,
          dp.sportai_player_uid AS player_uid,
          (ds.session_uid || '|' || COALESCE(dp.sportai_player_uid,'')) AS session_player_key,
          SUM(step_dist)::float  AS total_distance,
          AVG(step_dist)::float  AS avg_step_distance,
          AVG(dt)::float         AS avg_dt
        FROM steps
        JOIN dim_player  dp ON dp.player_id = steps.player_id
        JOIN dim_session ds ON ds.session_id = steps.session_id
        GROUP BY ds.session_uid, dp.sportai_player_uid;
    """,
}

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
    return row[0] if row else None

def _drop_view_or_matview(conn, name):
    kind = _get_relkind(conn, name)
    if kind == 'v':
        conn.execute(text(f"DROP VIEW IF EXISTS {name} CASCADE;"))
    elif kind == 'm':
        conn.execute(text(f"DROP MATERIALIZED VIEW IF EXISTS {name} CASCADE;"))

def _preflight_or_raise(conn):
    required_tables = [
        "dim_session", "dim_player", "dim_rally",
        "fact_swing", "fact_bounce", "fact_ball_position", "fact_player_position",
        "team_session"
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

    checks = [
        ("dim_session", "session_uid"),
        ("dim_player", "sportai_player_uid"),
        ("fact_swing", "ball_hit_s"),
        ("fact_swing", "start_s"),
        ("fact_bounce", "y"),
        ("fact_player_position", "ts_s"),
        ("dim_rally", "start_s"),
        ("dim_rally", "end_s"),
    ]
    missing_cols = [(t, c) for (t, c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t, c) in missing_cols])
        raise RuntimeError(f"Missing required columns before creating views: {msg}")

def run_views(engine):
    with engine.begin() as conn:
        _preflight_or_raise(conn)
        for name in VIEW_NAMES:
            _drop_view_or_matview(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))
