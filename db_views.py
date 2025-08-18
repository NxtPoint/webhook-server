# db_views.py
from sqlalchemy import text

VIEW_NAMES = [
    "vw_session_summary",
    "vw_player_summary",
    "vw_rally_summary",
    "vw_shot_tempo",
    "vw_player_movement",
]

CREATE_STMTS = {
    # ---------------- SESSION SUMMARY ----------------
    "vw_session_summary": r"""
    CREATE VIEW vw_session_summary AS
    WITH s AS (
      SELECT session_id, session_uid, COALESCE(fps, 0)::float AS fps
      FROM dim_session
    ),
    r AS (
      SELECT session_id,
             COUNT(*)                         AS rallies,
             AVG(GREATEST(0, end_s - start_s))::float AS avg_rally_duration_s,
             PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY GREATEST(0, end_s - start_s))::float AS med_rally_duration_s
      FROM dim_rally
      GROUP BY 1
    ),
    sw AS (
      SELECT fs.session_id,
             COUNT(*)::int                    AS swings,
             SUM(CASE WHEN COALESCE(fs.serve,false) THEN 1 ELSE 0 END)::int AS serve_swings,
             AVG(NULLIF(LEAD(fs.ball_hit_s) OVER(PARTITION BY fs.session_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s,0))::float AS avg_tempo_s,
             PERCENTILE_CONT(0.75) WITHIN GROUP
                (ORDER BY NULLIF(LEAD(fs.ball_hit_s) OVER(PARTITION BY fs.session_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s,0))::float AS p75_tempo_s,
             PERCENTILE_CONT(0.90) WITHIN GROUP
                (ORDER BY NULLIF(LEAD(fs.ball_hit_s) OVER(PARTITION BY fs.session_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s,0))::float AS p90_tempo_s
      FROM fact_swing fs
      GROUP BY 1
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
             -- depth buckets on Y; tweak thresholds to your court units
             SUM(CASE WHEN y <= -2.5 THEN 1 ELSE 0 END)::int AS deep_back,
             SUM(CASE WHEN y > -2.5 AND y < 2.5 THEN 1 ELSE 0 END)::int AS mid_court,
             SUM(CASE WHEN y >= 2.5 THEN 1 ELSE 0 END)::int AS near_net
      FROM fact_bounce GROUP BY 1
    )
    SELECT
      s.session_uid,
      r.rallies,
      sw.swings,
      COALESCE(sw.serve_swings,0)                    AS serve_swings,
      (COALESCE(sw.serve_swings,0)::float / NULLIF(sw.swings,0))::float AS pct_serve_swings,
      rs_agg.avg_rally_shots,
      rs_agg.med_rally_shots,
      r.avg_rally_duration_s,
      r.med_rally_duration_s,
      sw.avg_tempo_s,
      sw.p75_tempo_s,
      sw.p90_tempo_s,
      COALESCE(b.bounces,0)                          AS bounces,
      b.deep_back, b.mid_court, b.near_net
    FROM s
    LEFT JOIN r       ON r.session_id       = s.session_id
    LEFT JOIN sw      ON sw.session_id      = s.session_id
    LEFT JOIN rs_agg  ON rs_agg.session_id  = s.session_id
    LEFT JOIN b       ON b.session_id       = s.session_id;
    """,

    # ---------------- PLAYER SUMMARY ----------------
    "vw_player_summary": r"""
    CREATE VIEW vw_player_summary AS
    WITH membership AS (
      SELECT DISTINCT
        ts.session_id,
        'front'::text AS side,
        x::text       AS player_uid
      FROM team_session ts, jsonb_array_elements_text(ts.data->'team_front') AS x
      UNION
      SELECT DISTINCT
        ts.session_id,
        'back'::text  AS side,
        x::text       AS player_uid
      FROM team_session ts, jsonb_array_elements_text(ts.data->'team_back') AS x
    ),
    swings AS (
      SELECT
        fs.session_id, dp.player_id, dp.sportai_player_uid AS player_uid,
        COUNT(*)::int                                       AS swings,
        SUM(CASE WHEN COALESCE(fs.serve,false) THEN 1 ELSE 0 END)::int AS serve_swings,
        AVG(fs.ball_hit_x)::float                           AS avg_contact_x,
        AVG(fs.ball_hit_y)::float                           AS avg_contact_y,
        AVG(NULLIF(LEAD(fs.ball_hit_s) OVER(PARTITION BY fs.session_id, dp.player_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s,0))::float AS avg_tempo_s,
        SUM(CASE WHEN fs.ball_hit_y > 0 THEN 1 ELSE 0 END)::int AS contacts_in_front
      FROM fact_swing fs
      LEFT JOIN dim_player dp ON dp.player_id = fs.player_id
      GROUP BY 1,2,3
    ),
    serve_dir AS (
      -- naive L/Body/R by contact X (tune thresholds!)
      SELECT
        fs.session_id, dp.player_id,
        SUM(CASE WHEN fs.serve AND fs.ball_hit_x <= -2.5 THEN 1 ELSE 0 END)::int AS srv_left,
        SUM(CASE WHEN fs.serve AND fs.ball_hit_x >  -2.5 AND fs.ball_hit_x < 2.5 THEN 1 ELSE 0 END)::int AS srv_body,
        SUM(CASE WHEN fs.serve AND fs.ball_hit_x >=  2.5 THEN 1 ELSE 0 END)::int AS srv_right
      FROM fact_swing fs
      LEFT JOIN dim_player dp ON dp.player_id = fs.player_id
      GROUP BY 1,2
    ),
    movement AS (
      -- distance from consecutive player_position points
      SELECT
        p.session_id, p.player_id,
        SUM( sqrt( power(p.x - LAG(p.x) OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s), 2)
                 + power(p.y - LAG(p.y) OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s), 2) ) )::float AS total_distance_units,
        (MAX(p.ts_s) - MIN(p.ts_s))::float AS active_time_s
      FROM fact_player_position p
      GROUP BY 1,2
    )
    SELECT
      ds.session_uid,
      dp.sportai_player_uid  AS player_uid,
      COALESCE(dp.full_name, dp.sportai_player_uid) AS player_name,
      m.side                 AS player_side,                          -- front/back if available
      sw.swings, sw.serve_swings,
      (sw.serve_swings::float / NULLIF(sw.swings,0))::float AS pct_serve_swings,
      sw.avg_contact_x, sw.avg_contact_y,
      (sw.contacts_in_front::float / NULLIF(sw.swings,0))::float AS pct_contacts_in_front,
      sd.srv_left, sd.srv_body, sd.srv_right,
      CASE WHEN (sd.srv_left+sd.srv_body+sd.srv_right) > 0
           THEN sd.srv_left::float  / (sd.srv_left+sd.srv_body+sd.srv_right) END AS pct_srv_left,
      CASE WHEN (sd.srv_left+sd.srv_body+sd.srv_right) > 0
           THEN sd.srv_body::float  / (sd.srv_left+sd.srv_body+sd.srv_right) END AS pct_srv_body,
      CASE WHEN (sd.srv_left+sd.srv_body+sd.srv_right) > 0
           THEN sd.srv_right::float / (sd.srv_left+sd.srv_body+sd.srv_right) END AS pct_srv_right,
      sw.avg_tempo_s,
      mv.total_distance_units,
      CASE WHEN mv.active_time_s > 0 THEN mv.total_distance_units / mv.active_time_s END AS avg_speed_units_per_s
    FROM dim_player dp
    JOIN dim_session ds ON ds.session_id = dp.session_id
    LEFT JOIN swings   sw ON sw.session_id = dp.session_id AND sw.player_id = dp.player_id
    LEFT JOIN serve_dir sd ON sd.session_id = dp.session_id AND sd.player_id = dp.player_id
    LEFT JOIN movement mv ON mv.session_id = dp.session_id AND mv.player_id = dp.player_id
    LEFT JOIN membership m ON m.session_id = dp.session_id AND m.player_uid = dp.sportai_player_uid;
    """,

    # ---------------- RALLY SUMMARY ----------------
    "vw_rally_summary": r"""
    CREATE VIEW vw_rally_summary AS
    WITH shots AS (
      SELECT
        dr.session_id, dr.rally_id,
        COUNT(*)::int AS shot_count,
        AVG(NULLIF(LEAD(fs.ball_hit_s) OVER (PARTITION BY dr.session_id, dr.rally_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s, 0))::float AS avg_tempo_s
      FROM dim_rally dr
      LEFT JOIN fact_swing fs
        ON fs.session_id = dr.session_id
       AND COALESCE(fs.ball_hit_s, fs.start_s) BETWEEN dr.start_s AND dr.end_s
      GROUP BY 1,2
    ),
    b AS (
      SELECT session_id, rally_id, COUNT(*)::int AS bounces
      FROM fact_bounce
      GROUP BY 1,2
    )
    SELECT
      ds.session_uid,
      dr.rally_number,
      GREATEST(0, dr.end_s - dr.start_s)::float AS duration_s,
      s.shot_count,
      COALESCE(b.bounces,0) AS bounces,
      s.avg_tempo_s
    FROM dim_rally dr
    JOIN dim_session ds ON ds.session_id = dr.session_id
    LEFT JOIN shots s    ON s.session_id = dr.session_id AND s.rally_id = dr.rally_id
    LEFT JOIN b     b    ON b.session_id = dr.session_id AND b.rally_id = dr.rally_id
    ORDER BY ds.session_uid, dr.rally_number;
    """,

    # ---------------- SHOT TEMPO (raw rows) ----------------
    "vw_shot_tempo": r"""
    CREATE VIEW vw_shot_tempo AS
    SELECT
      ds.session_uid,
      dp.sportai_player_uid AS player_uid,
      fs.ball_hit_s AS this_contact_s,
      LEAD(fs.ball_hit_s) OVER (PARTITION BY fs.session_id, fs.player_id ORDER BY fs.ball_hit_s) AS next_contact_s,
      NULLIF(LEAD(fs.ball_hit_s) OVER (PARTITION BY fs.session_id, fs.player_id ORDER BY fs.ball_hit_s) - fs.ball_hit_s, 0)::float AS tempo_s
    FROM fact_swing fs
    JOIN dim_session ds ON ds.session_id = fs.session_id
    LEFT JOIN dim_player dp ON dp.player_id = fs.player_id;
    """,

    # ---------------- PLAYER MOVEMENT (raw & agg) ----------------
    "vw_player_movement": r"""
    CREATE VIEW vw_player_movement AS
    WITH moves AS (
      SELECT
        ds.session_uid,
        dp.sportai_player_uid AS player_uid,
        p.ts_s,
        LAG(p.ts_s) OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS prev_ts_s,
        p.x, p.y,
        LAG(p.x) OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS px,
        LAG(p.y) OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS py
      FROM fact_player_position p
      JOIN dim_session ds ON ds.session_id = p.session_id
      JOIN dim_player  dp ON dp.player_id  = p.player_id
    ),
    step AS (
      SELECT
        session_uid, player_uid, ts_s,
        sqrt( power(x - px, 2) + power(y - py, 2) )::float AS step_dist
      FROM moves
      WHERE px IS NOT NULL AND py IS NOT NULL
    )
    SELECT
      session_uid, player_uid,
      SUM(step_dist)::float AS total_distance_units,
      COUNT(*)::int        AS steps
    FROM step
    GROUP BY 1,2;
    """,
}

# ----------- helpers (unchanged) -----------
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
    """), {"t": t}).first() is not None

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
        "dim_session","dim_player","dim_rally",
        "fact_swing","fact_bounce","fact_ball_position","fact_player_position"
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

def run_views(engine):
    with engine.begin() as conn:
        _preflight_or_raise(conn)
        for name in VIEW_NAMES:
            _drop_view_or_matview(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))
