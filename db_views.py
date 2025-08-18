# db_views.py
from sqlalchemy import text

VIEW_NAMES = [
    # base views (kept from your file)
    "vw_swing",
    "vw_bounce",
    "vw_rally",
    "vw_ball_position",
    "vw_player_position",
    # analytics views for Power BI
    "Session_Summary",
    "Player_Summary",
    "Rally_Summary",
    "Shot_Tempo",
    "Player_Movement",
]

CREATE_STMTS = {
    # ---------- BASE VIEWS (unchanged logic, tidied) ----------
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
        LEFT JOIN dim_player dp ON dp.player_id = b.hitter_player_id
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
        LEFT JOIN dim_player dp ON dp.player_id = p.player_id;
    """,

    # ---------- ANALYTICS VIEWS FOR POWER BI ----------
    "Session_Summary": """
        CREATE VIEW Session_Summary AS
        WITH s AS (
          SELECT session_id, session_uid, COALESCE(fps,0)::float AS fps
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
        sw_tempo AS (
          -- compute per-shot deltas in a subquery, then aggregate to avoid window-in-aggregate error
          SELECT x.session_id,
                 AVG(x.delta_s)::float AS avg_tempo_s,
                 PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY x.delta_s)::float AS p75_tempo_s,
                 PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY x.delta_s)::float AS p90_tempo_s
          FROM (
            SELECT fs.session_id,
                   (LEAD(COALESCE(fs.ball_hit_s, fs.start_s))
                    OVER (PARTITION BY fs.session_id ORDER BY COALESCE(fs.ball_hit_s, fs.start_s))
                   - COALESCE(fs.ball_hit_s, fs.start_s)) AS delta_s
            FROM fact_swing fs
          ) x
          WHERE x.delta_s IS NOT NULL AND x.delta_s > 0
          GROUP BY x.session_id
        ),
        rs AS (
          -- shots per rally
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
          FROM rs GROUP BY 1
        ),
        b AS (
          SELECT session_id,
                 COUNT(*)::int AS bounces,
                 -- depth buckets on Y (tweak thresholds to your coordinate system)
                 SUM(CASE WHEN y <= -2.5 THEN 1 ELSE 0 END)::int AS deep_back,
                 SUM(CASE WHEN y > -2.5 AND y <  2.5 THEN 1 ELSE 0 END)::int AS mid_court,
                 SUM(CASE WHEN y >=  2.5 THEN 1 ELSE 0 END)::int AS near_net
          FROM fact_bounce
          GROUP BY 1
        )
        SELECT
          s.session_uid,
          COALESCE(r.rallies,0)                        AS rallies,
          COALESCE(sw_counts.swings,0)                 AS swings,
          COALESCE(sw_counts.serve_swings,0)           AS serve_swings,
          (COALESCE(sw_counts.serve_swings,0)::float / NULLIF(sw_counts.swings,0))::float AS pct_serve_swings,
          rs_agg.avg_rally_shots,
          rs_agg.med_rally_shots,
          r.avg_rally_duration_s,
          r.med_rally_duration_s,
          sw_tempo.avg_tempo_s,
          sw_tempo.p75_tempo_s,
          sw_tempo.p90_tempo_s,
          COALESCE(b.bounces,0)                        AS bounces,
          b.deep_back, b.mid_court, b.near_net
        FROM s
        LEFT JOIN r        ON r.session_id        = s.session_id
        LEFT JOIN sw_counts ON sw_counts.session_id = s.session_id
        LEFT JOIN sw_tempo ON sw_tempo.session_id  = s.session_id
        LEFT JOIN rs_agg   ON rs_agg.session_id    = s.session_id
        LEFT JOIN b        ON b.session_id         = s.session_id;
    """,

    "Player_Summary": """
        CREATE VIEW Player_Summary AS
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
        ),
        sw AS (
          SELECT fs.session_id, fs.player_id,
                 COUNT(*)::int AS swings,
                 SUM(CASE WHEN COALESCE(fs.serve,false) THEN 1 ELSE 0 END)::int AS serve_swings
          FROM fact_swing fs
          GROUP BY 1,2
        ),
        pp AS (
          SELECT f.session_id, f.player_id, COUNT(*)::int AS position_points,
                 MIN(f.x) AS min_x, MAX(f.x) AS max_x,
                 MIN(f.y) AS min_y, MAX(f.y) AS max_y
          FROM fact_player_position f
          GROUP BY 1,2
        )
        SELECT
          ds.session_uid,
          dp.sportai_player_uid AS player_uid,
          (ds.session_uid || '|' || dp.sportai_player_uid) AS session_player_key,
          dp.full_name,
          COALESCE(m.side, NULL) AS side,
          COALESCE(sw.swings,0) AS swings,
          COALESCE(sw.serve_swings,0) AS serve_swings,
          COALESCE(pp.position_points,0) AS position_points,
          pp.min_x, pp.max_x, pp.min_y, pp.max_y,
          dp.covered_distance,
          dp.fastest_sprint,
          dp.fastest_sprint_timestamp_s,
          dp.activity_score
        FROM dim_player dp
        JOIN dim_session ds ON ds.session_id = dp.session_id
        LEFT JOIN membership m ON m.session_id = dp.session_id AND m.player_uid = dp.sportai_player_uid
        LEFT JOIN sw ON sw.session_id = dp.session_id AND sw.player_id = dp.player_id
        LEFT JOIN pp ON pp.session_id = dp.session_id AND pp.player_id = dp.player_id
        WHERE dp.sportai_player_uid IS NOT NULL;
    """,

    "Rally_Summary": """
        CREATE VIEW Rally_Summary AS
        WITH shots AS (
          SELECT dr.session_id, dr.rally_id,
                 COUNT(*)::int AS shots_in_rally
          FROM dim_rally dr
          LEFT JOIN fact_swing fs
            ON fs.session_id = dr.session_id
           AND COALESCE(fs.ball_hit_s, fs.start_s) BETWEEN dr.start_s AND dr.end_s
          GROUP BY 1,2
        ),
        b AS (
          SELECT dr.session_id, dr.rally_id,
                 COUNT(b.bounce_id)::int AS bounces_in_rally
          FROM dim_rally dr
          LEFT JOIN fact_bounce b
            ON b.session_id = dr.session_id
           AND b.rally_id   = dr.rally_id
          GROUP BY 1,2
        )
        SELECT
          ds.session_uid,
          dr.rally_number,
          GREATEST(0, dr.end_s - dr.start_s)::float AS rally_duration_s,
          COALESCE(shots.shots_in_rally,0)  AS shots_in_rally,
          COALESCE(b.bounces_in_rally,0)    AS bounces_in_rally
        FROM dim_rally dr
        JOIN dim_session ds ON ds.session_id = dr.session_id
        LEFT JOIN shots ON shots.session_id = dr.session_id AND shots.rally_id = dr.rally_id
        LEFT JOIN b     ON b.session_id     = dr.session_id AND b.rally_id     = dr.rally_id
        ORDER BY ds.session_uid, dr.rally_number;
    """,

    "Shot_Tempo": """
        CREATE VIEW Shot_Tempo AS
        WITH base AS (
          SELECT
            fs.session_id,
            dp.sportai_player_uid AS player_uid,
            COALESCE(fs.ball_hit_s, fs.start_s) AS t
          FROM fact_swing fs
          LEFT JOIN dim_player dp ON dp.player_id = fs.player_id
          WHERE dp.sportai_player_uid IS NOT NULL
        ),
        deltas AS (
          SELECT
            session_id,
            player_uid,
            (LEAD(t) OVER (PARTITION BY session_id, player_uid ORDER BY t) - t) AS delta_s
          FROM base
        )
        SELECT
          ds.session_uid,
          d.player_uid,
          (ds.session_uid || '|' || d.player_uid) AS session_player_key,
          COUNT(*) FILTER (WHERE d.delta_s IS NOT NULL AND d.delta_s > 0)::int AS intervals,
          AVG(d.delta_s) FILTER (WHERE d.delta_s IS NOT NULL AND d.delta_s > 0)::float AS avg_tempo_s,
          PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY d.delta_s)::float
            FILTER (WHERE d.delta_s IS NOT NULL AND d.delta_s > 0) AS p75_tempo_s,
          PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY d.delta_s)::float
            FILTER (WHERE d.delta_s IS NOT NULL AND d.delta_s > 0) AS p90_tempo_s
        FROM deltas d
        JOIN dim_session ds ON ds.session_id = d.session_id
        WHERE d.delta_s IS NOT NULL AND d.delta_s > 0
        GROUP BY ds.session_uid, d.player_uid;
    """,

    "Player_Movement": """
        CREATE VIEW Player_Movement AS
        SELECT
          ds.session_uid,
          dp.sportai_player_uid AS player_uid,
          (ds.session_uid || '|' || dp.sportai_player_uid) AS session_player_key,
          COUNT(f.id)::int AS position_points,
          MIN(f.x) AS min_x,
          MAX(f.x) AS max_x,
          MIN(f.y) AS min_y,
          MAX(f.y) AS max_y
        FROM fact_player_position f
        JOIN dim_player  dp ON dp.player_id  = f.player_id
        JOIN dim_session ds ON ds.session_id = f.session_id
        WHERE dp.sportai_player_uid IS NOT NULL
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
        ("dim_player", "sportai_player_uid"),
        ("fact_swing", "start_s"),
        ("fact_swing", "ball_hit_s"),
        ("fact_bounce", "bounce_s"),
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
        for name in VIEW_NAMES:
            _drop_view_or_matview(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))
