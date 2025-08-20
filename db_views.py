# db_views.py
from sqlalchemy import text

VIEW_NAMES = [
    # ---------- BASE VIEWS ----------
    "vw_swing",
    "vw_bounce",
    "vw_rally",
    "vw_ball_position",
    "vw_player_position",

    # ---------- ANALYTICS SUMMARY VIEWS (existing) ----------
    "Session_Summary",
    "Player_Summary",
    "Rally_Summary",
    "Shot_Tempo",
    "Player_Movement",

    # ---------- NEW HELPER VIEWS (additive) ----------
    # Order matters: shot_order before serve_return
    "vw_shot_order",
    "vw_point",
    "vw_serve_return",
    "vw_shot_tempo",
]

CREATE_STMTS = {
    # ---------- BASE VIEWS ----------
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

    # ---------- ANALYTICS SUMMARY VIEWS (existing) ----------
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
        LEFT JOIN r         ON r.session_id         = s.session_id
        LEFT JOIN sw_counts ON sw_counts.session_id = s.session_id
        LEFT JOIN sw_tempo  ON sw_tempo.session_id  = s.session_id
        LEFT JOIN rs_agg    ON rs_agg.session_id    = s.session_id
        LEFT JOIN b         ON b.session_id         = s.session_id;
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
        ),
        deltas_pos AS (
          SELECT session_id, player_uid, delta_s
          FROM deltas
          WHERE delta_s IS NOT NULL AND delta_s > 0
        )
        SELECT
          ds.session_uid,
          d.player_uid,
          (ds.session_uid || '|' || d.player_uid) AS session_player_key,
          COUNT(*)::int AS intervals,
          AVG(d.delta_s)::float AS avg_tempo_s,
          PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY d.delta_s)::float AS p75_tempo_s,
          PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY d.delta_s)::float AS p90_tempo_s
        FROM deltas_pos d
        JOIN dim_session ds ON ds.session_id = d.session_id
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

    # ---------- NEW HELPER VIEWS (additive) ----------

    "vw_shot_order": """
        CREATE VIEW vw_shot_order AS
        SELECT
          fs.swing_id,
          fs.session_id,
          ds.session_uid,
          fs.player_id,
          dp.sportai_player_uid AS player_uid,
          fs.rally_id,
          dr.rally_number,
          COALESCE(fs.ball_hit_s, fs.start_s) AS t,
          ROW_NUMBER() OVER (
            PARTITION BY fs.session_id, fs.rally_id
            ORDER BY COALESCE(fs.ball_hit_s, fs.start_s), fs.swing_id
          ) AS shot_index,
          ROW_NUMBER() OVER (
            PARTITION BY fs.session_id, fs.rally_id, fs.player_id
            ORDER BY COALESCE(fs.ball_hit_s, fs.start_s), fs.swing_id
          ) AS player_shot_num
        FROM fact_swing fs
        JOIN dim_session ds ON ds.session_id = fs.session_id
        LEFT JOIN dim_player dp ON dp.player_id = fs.player_id
        LEFT JOIN dim_rally  dr ON dr.session_id = fs.session_id AND dr.rally_id = fs.rally_id
        WHERE fs.rally_id IS NOT NULL;
    """,

    "vw_point": """
        CREATE VIEW vw_point AS
        WITH first_shot AS (
          SELECT DISTINCT ON (fs.session_id, fs.rally_id)
            fs.session_id,
            fs.rally_id,
            fs.swing_id AS first_swing_id,
            fs.player_id AS first_hitter_id,
            (CASE WHEN COALESCE(fs.serve,false) THEN fs.player_id END) AS server_id,
            COALESCE(fs.ball_hit_s, fs.start_s) AS t0
          FROM fact_swing fs
          WHERE fs.rally_id IS NOT NULL
          ORDER BY fs.session_id, fs.rally_id, COALESCE(fs.ball_hit_s, fs.start_s), fs.swing_id
        ),
        last_shot AS (
          SELECT DISTINCT ON (fs.session_id, fs.rally_id)
            fs.session_id,
            fs.rally_id,
            fs.swing_id AS last_swing_id,
            COALESCE(fs.ball_hit_s, fs.end_s) AS t1
          FROM fact_swing fs
          WHERE fs.rally_id IS NOT NULL
          ORDER BY fs.session_id, fs.rally_id, COALESCE(fs.ball_hit_s, fs.end_s) DESC, fs.swing_id DESC
        ),
        agg AS (
          SELECT fs.session_id, fs.rally_id, COUNT(*)::int AS total_swings
          FROM fact_swing fs
          WHERE fs.rally_id IS NOT NULL
          GROUP BY 1,2
        )
        SELECT
          ds.session_uid,
          a.session_id,
          a.rally_id,
          dr.rally_number,
          a.total_swings,
          f.first_swing_id,
          f.first_hitter_id,
          COALESCE(f.server_id, f.first_hitter_id) AS server_id,
          l.last_swing_id,
          GREATEST(0, l.t1 - f.t0)::float AS rally_duration_s
        FROM agg a
        JOIN first_shot f ON f.session_id = a.session_id AND f.rally_id = a.rally_id
        JOIN last_shot  l ON l.session_id = a.session_id AND l.rally_id = a.rally_id
        JOIN dim_session ds ON ds.session_id = a.session_id
        LEFT JOIN dim_rally dr ON dr.session_id = a.session_id AND dr.rally_id = a.rally_id;
    """,

    "vw_serve_return": """
        CREATE VIEW vw_serve_return AS
        WITH so AS (
          SELECT * FROM vw_shot_order
        ),
        serve_row AS (
          SELECT DISTINCT ON (fs.session_id, fs.rally_id)
            fs.session_id,
            ds.session_uid,
            fs.rally_id,
            fs.player_id AS server_player_id,
            fs.swing_id  AS serve_swing_id,
            so.shot_index
          FROM fact_swing fs
          JOIN so ON so.swing_id = fs.swing_id
          JOIN dim_session ds ON ds.session_id = fs.session_id
          WHERE fs.rally_id IS NOT NULL
          ORDER BY fs.session_id, fs.rally_id,
            CASE WHEN COALESCE(fs.serve,false) THEN 0 ELSE 1 END,
            so.shot_index
        ),
        return_row AS (
          SELECT
            s.session_id,
            s.rally_id,
            MAX(CASE WHEN s.shot_index = 2 THEN s.swing_id END) AS return_swing_id
          FROM so s
          GROUP BY s.session_id, s.rally_id
        ),
        serve_plus1_row AS (
          SELECT
            s.session_id,
            s.rally_id,
            MIN(s.shot_index) AS serve_plus1_index
          FROM so s
          JOIN serve_row sr
            ON s.session_id = sr.session_id
           AND s.rally_id   = sr.rally_id
           AND s.player_id  = sr.server_player_id
           AND s.shot_index > sr.shot_index
          GROUP BY s.session_id, s.rally_id
        ),
        serve_plus1 AS (
          SELECT so.session_id, so.rally_id, so.swing_id AS serve_plus1_swing_id
          FROM so
          JOIN serve_plus1_row sp1
            ON so.session_id = sp1.session_id
           AND so.rally_id   = sp1.rally_id
           AND so.shot_index = sp1.serve_plus1_index
        )
        SELECT
          sr.session_id,
          sr.session_uid,
          sr.rally_id,
          dr.rally_number,
          sr.server_player_id,
          sr.serve_swing_id,
          rr.return_swing_id,
          sp.serve_plus1_swing_id
        FROM serve_row sr
        LEFT JOIN return_row rr
          ON rr.session_id = sr.session_id AND rr.rally_id = sr.rally_id
        LEFT JOIN serve_plus1 sp
          ON sp.session_id = sr.session_id AND sp.rally_id = sr.rally_id
        LEFT JOIN dim_rally dr
          ON dr.session_id = sr.session_id AND dr.rally_id = sr.rally_id;
    """,

    "vw_shot_tempo": """
        CREATE VIEW vw_shot_tempo AS
        WITH base AS (
          SELECT
            fs.session_id,
            ds.session_uid,
            fs.player_id,
            dp.sportai_player_uid AS player_uid,
            fs.swing_id,
            COALESCE(fs.ball_hit_s, fs.start_s) AS t
          FROM fact_swing fs
          JOIN dim_session ds ON ds.session_id = fs.session_id
          LEFT JOIN dim_player dp ON dp.player_id = fs.player_id
          WHERE dp.sportai_player_uid IS NOT NULL
        ),
        deltas AS (
          SELECT
            session_id,
            session_uid,
            player_id,
            player_uid,
            swing_id,
            (t - LAG(t) OVER (
              PARTITION BY session_id, player_id
              ORDER BY t, swing_id
            )) AS delta_s,
            t
          FROM base
        )
        SELECT
          session_id,
          session_uid,
          player_id,
          player_uid,
          swing_id,
          t,
          delta_s
        FROM deltas
        WHERE delta_s IS NOT NULL AND delta_s >= 0;
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
        # fallback: attempt both view types just in case
        conn.execute(text(f"DROP VIEW IF EXISTS {name} CASCADE;"))
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
        # drop first to avoid dependency issues
        for name in VIEW_NAMES:
            _drop_view_or_matview(conn, name)
        # create in the declared order (shot_order precedes serve_return)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))
