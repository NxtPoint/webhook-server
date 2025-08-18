# db_views.py
from sqlalchemy import text

VIEW_NAMES = [
    # existing
    "vw_swing",
    "vw_bounce",
    "vw_rally",
    "vw_ball_position",
    "vw_player_position",
    # new metrics layer
    "vw_session_summary",
    "vw_player_movement",
    "vw_player_summary",
    "vw_rally_summary",
    "vw_shot_tempo",
]

CREATE_STMTS = {
    # ---------- YOUR ORIGINAL LIGHTWEIGHT VIEWS ----------
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
          CASE WHEN m.side IS NOT NULL
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
            dp.sportai_player_uid AS hitter_uid,
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
            dp.sportai_player_uid AS player_uid,
            dp.full_name AS player_name,
            p.ts_s, p.ts, p.x, p.y
        FROM fact_player_position p
        LEFT JOIN dim_session ds ON ds.session_id = p.session_id
        LEFT JOIN dim_player dp  ON dp.player_id  = p.player_id;
    """,

    # ---------- NEW METRIC VIEWS ----------
    "vw_session_summary": """
        CREATE VIEW vw_session_summary AS
        WITH shots AS (
          SELECT s.session_id,
                 COUNT(*) AS swings,
                 COUNT(*) FILTER (WHERE COALESCE(s.serve, false)) AS serves,
                 AVG(s.ball_speed) AS avg_ball_speed,
                 AVG(s.ball_player_distance) AS avg_ball_player_dist
          FROM fact_swing s
          GROUP BY s.session_id
        ),
        bounces AS (
          SELECT session_id,
                 COUNT(*) AS bounces
          FROM fact_bounce
          GROUP BY session_id
        ),
        rallies AS (
          SELECT r.session_id,
                 COUNT(*) AS rally_count,
                 AVG(NULLIF(r.end_s - r.start_s,0)) AS avg_rally_seconds,
                 MAX(NULLIF(r.end_s - r.start_s,0)) AS max_rally_seconds,
                 SUM(NULLIF(r.end_s - r.start_s,0)) AS total_rally_seconds
          FROM dim_rally r
          GROUP BY r.session_id
        ),
        players AS (
          SELECT session_id, COUNT(*) AS players
          FROM dim_player GROUP BY session_id
        ),
        ball_span AS (
          SELECT session_id,
                 (MAX(ts_s) - MIN(ts_s)) AS recording_seconds
          FROM fact_ball_position
          GROUP BY session_id
        )
        SELECT
          s.session_id,
          ds.session_uid,
          COALESCE(p.players,0) AS players,
          COALESCE(sh.swings,0) AS swings,
          COALESCE(sh.serves,0) AS serves,
          CASE WHEN COALESCE(sh.swings,0)>0 THEN sh.serves::decimal/sh.swings ELSE 0 END AS pct_serves,
          COALESCE(b.bounces,0) AS bounces,
          COALESCE(r.rally_count,0) AS rallies,
          COALESCE(r.avg_rally_seconds,0) AS avg_rally_seconds,
          COALESCE(r.max_rally_seconds,0) AS max_rally_seconds,
          COALESCE(r.total_rally_seconds,0) AS total_rally_seconds,
          COALESCE(bs.recording_seconds,0)  AS recording_seconds,
          COALESCE(sh.avg_ball_speed, NULL) AS avg_ball_speed,
          COALESCE(sh.avg_ball_player_dist, NULL) AS avg_ball_player_dist,
          CASE WHEN COALESCE(r.rally_count,0)>0
               THEN COALESCE(sh.swings,0)::decimal / r.rally_count
               ELSE NULL END AS shots_per_rally,
          CASE WHEN COALESCE(r.rally_count,0)>0
               THEN COALESCE(b.bounces,0)::decimal / r.rally_count
               ELSE NULL END AS bounces_per_rally
        FROM (SELECT DISTINCT session_id FROM dim_session) s
        LEFT JOIN dim_session ds ON ds.session_id = s.session_id
        LEFT JOIN shots   sh ON sh.session_id = s.session_id
        LEFT JOIN bounces b  ON b.session_id  = s.session_id
        LEFT JOIN rallies r  ON r.session_id  = s.session_id
        LEFT JOIN players p  ON p.session_id  = s.session_id
        LEFT JOIN ball_span bs ON bs.session_id = s.session_id;
    """,

    "vw_player_movement": """
        CREATE VIEW vw_player_movement AS
        WITH pos AS (
          SELECT
            p.session_id, p.player_id, dp.sportai_player_uid AS player_uid,
            p.ts_s,
            p.x, p.y,
            LAG(p.ts_s) OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS prev_ts_s,
            LAG(p.x)    OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS prev_x,
            LAG(p.y)    OVER (PARTITION BY p.session_id, p.player_id ORDER BY p.ts_s) AS prev_y
          FROM fact_player_position p
          JOIN dim_player dp ON dp.player_id = p.player_id
        ),
        steps AS (
          SELECT
            session_id, player_id, player_uid,
            CASE WHEN prev_x IS NULL OR prev_y IS NULL THEN 0
                 ELSE sqrt( (x - prev_x)^2 + (y - prev_y)^2 ) END AS step_distance,
            GREATEST(0, ts_s - COALESCE(prev_ts_s, ts_s)) AS step_seconds,
            x, y
          FROM pos
        )
        SELECT
          s.session_id,
          ds.session_uid,
          s.player_id,
          s.player_uid,
          SUM(step_distance) AS total_distance,
          SUM(step_seconds)  AS total_seconds,
          CASE WHEN SUM(step_seconds) > 0
               THEN SUM(step_distance) / SUM(step_seconds)
               ELSE NULL END AS avg_speed,
          AVG(x)  AS mean_x,
          AVG(y)  AS mean_y,
          STDDEV_POP(x) AS sd_x,
          STDDEV_POP(y) AS sd_y,
          COUNT(*) AS samples
        FROM steps s
        JOIN dim_session ds ON ds.session_id = s.session_id
        GROUP BY s.session_id, ds.session_uid, s.player_id, s.player_uid;
    """,

    "vw_player_summary": """
        CREATE VIEW vw_player_summary AS
        WITH swings AS (
          SELECT
            s.session_id, s.player_id,
            COUNT(*) AS swings,
            COUNT(*) FILTER (WHERE COALESCE(s.serve,false)) AS serves,
            AVG(s.ball_speed) AS avg_ball_speed,
            AVG(s.ball_player_distance) AS avg_ball_player_dist
          FROM fact_swing s
          GROUP BY s.session_id, s.player_id
        )
        SELECT
          d.session_id,
          ds.session_uid,
          d.player_id,
          d.sportai_player_uid AS player_uid,
          d.full_name          AS player_name,
          COALESCE(sw.swings,0) AS swings,
          COALESCE(sw.serves,0) AS serves,
          CASE WHEN COALESCE(sw.swings,0)>0 THEN sw.serves::decimal/sw.swings ELSE 0 END AS pct_serves,
          sw.avg_ball_speed,
          sw.avg_ball_player_dist,
          mv.total_distance,
          mv.total_seconds,
          mv.avg_speed,
          mv.mean_x, mv.mean_y, mv.sd_x, mv.sd_y,
          mv.samples AS position_samples
        FROM dim_player d
        JOIN dim_session ds   ON ds.session_id = d.session_id
        LEFT JOIN swings sw   ON sw.session_id = d.session_id AND sw.player_id = d.player_id
        LEFT JOIN vw_player_movement mv ON mv.session_id = d.session_id AND mv.player_id = d.player_id;
    """,

    "vw_rally_summary": """
        CREATE VIEW vw_rally_summary AS
        WITH shot_counts AS (
          SELECT r.session_id, r.rally_id,
                 COUNT(*) AS swings
          FROM dim_rally r
          LEFT JOIN fact_swing s ON s.session_id = r.session_id
                                AND s.ball_hit_s BETWEEN r.start_s AND r.end_s
          GROUP BY r.session_id, r.rally_id
        ),
        bounce_counts AS (
          SELECT r.session_id, r.rally_id,
                 COUNT(*) AS bounces
          FROM dim_rally r
          LEFT JOIN fact_bounce b ON b.session_id = r.session_id
                                 AND b.bounce_s BETWEEN r.start_s AND r.end_s
          GROUP BY r.session_id, r.rally_id
        )
        SELECT
          r.session_id,
          ds.session_uid,
          r.rally_id,
          r.rally_number,
          r.start_s, r.end_s,
          NULLIF(r.end_s - r.start_s,0) AS duration_s,
          COALESCE(sc.swings,0)  AS swings,
          COALESCE(bc.bounces,0) AS bounces,
          CASE WHEN NULLIF(r.end_s - r.start_s,0) IS NOT NULL AND (r.end_s - r.start_s) > 0
               THEN sc.swings::decimal / (r.end_s - r.start_s)
               ELSE NULL END AS shots_per_second
        FROM dim_rally r
        JOIN dim_session ds ON ds.session_id = r.session_id
        LEFT JOIN shot_counts   sc ON sc.session_id = r.session_id AND sc.rally_id = r.rally_id
        LEFT JOIN bounce_counts bc ON bc.session_id = r.session_id AND bc.rally_id = r.rally_id;
    """,

    "vw_shot_tempo": """
        CREATE VIEW vw_shot_tempo AS
        WITH s AS (
          SELECT
            s.session_id,
            ds.session_uid,
            s.player_id,
            dp.sportai_player_uid AS player_uid,
            s.ball_hit_s,
            LAG(s.ball_hit_s) OVER (PARTITION BY s.session_id ORDER BY s.ball_hit_s) AS prev_global,
            LAG(s.ball_hit_s) OVER (PARTITION BY s.session_id, s.player_id ORDER BY s.ball_hit_s) AS prev_player
          FROM fact_swing s
          JOIN dim_session ds ON ds.session_id = s.session_id
          LEFT JOIN dim_player dp ON dp.player_id = s.player_id
          WHERE s.ball_hit_s IS NOT NULL
        )
        SELECT
          session_id, session_uid, player_id, player_uid,
          AVG( CASE WHEN prev_global IS NOT NULL THEN (ball_hit_s - prev_global) END ) AS avg_intershot_s,
          AVG( CASE WHEN prev_player IS NOT NULL THEN (ball_hit_s - prev_player) END ) AS avg_player_cadence_s,
          COUNT(*) AS shots_count
        FROM s
        GROUP BY session_id, session_uid, player_id, player_uid;
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

def run_views(engine):
    with engine.begin() as conn:
        _preflight_or_raise(conn)
        for name in VIEW_NAMES:
            _drop_view_or_matview(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))
