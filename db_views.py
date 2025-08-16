# db_views.py
from sqlalchemy import text

def create_views(engine):
    """
    Drops and recreates analytic views so column lists can change safely.
    Compatible with legacy columns (is_serve, swing_start_ts, ball_player_dist, ball_x/ball_y).
    """
    view_sql = [
        # -- Bounce view
        """
        DROP VIEW IF EXISTS vw_bounce;
        CREATE VIEW vw_bounce AS
        SELECT
            b.session_id,
            b.rally_id,
            r.rally_number,
            b.bounce_ts,
            b.timestamp_s,
            b.bounce_x,
            b.bounce_y,
            b.bounce_type,
            b.hitter_player_id
        FROM fact_bounce b
        LEFT JOIN dim_rally r USING (rally_id);
        """,

        # -- Swing view (coalesce legacy/new cols)
        """
        DROP VIEW IF EXISTS vw_swing;
        CREATE VIEW vw_swing AS
        SELECT
            s.session_id,
            s.rally_id,
            r.rally_number,
            s.player_id,
            s.swing_type,
            COALESCE(s.serve, s.is_serve)                        AS serve,
            s.volley,
            s.is_in_rally,
            s.confidence,
            s.confidence_swing_type,
            s.confidence_volley,
            s.ball_hit_ts,
            s.ball_hit_s,
            s.ball_speed,
            COALESCE(s.ball_player_distance, s.ball_player_dist) AS ball_player_distance,
            COALESCE(s.ball_hit_x, s.ball_x)                     AS ball_hit_x,
            COALESCE(s.ball_hit_y, s.ball_y)                     AS ball_hit_y,
            COALESCE(s.start_ts, s.swing_start_ts)               AS start_ts,
            COALESCE(s.end_ts, s.swing_end_ts)                   AS end_ts,
            s.start_s,
            s.end_s
        FROM fact_swing s
        LEFT JOIN dim_rally r USING (rally_id);
        """,

        # -- Rally view
        """
        DROP VIEW IF EXISTS vw_rally;
        CREATE VIEW vw_rally AS
        SELECT
            r.session_id,
            r.rally_id,
            r.rally_number,
            r.start_ts,
            r.end_ts,
            r.length_shots,
            r.point_winner_player_id
        FROM dim_rally r;
        """,

        # -- Player positions
        """
        DROP VIEW IF EXISTS vw_player_position;
        CREATE VIEW vw_player_position AS
        SELECT
            p.session_id,
            p.player_id,
            dp.sportai_player_uid,
            p.ts,
            p.timestamp_s,
            p.img_x,
            p.img_y,
            p.court_x,
            p.court_y
        FROM fact_player_position p
        LEFT JOIN dim_player dp USING (player_id);
        """,

        # -- Ball positions
        """
        DROP VIEW IF EXISTS vw_ball_position;
        CREATE VIEW vw_ball_position AS
        SELECT
            session_id,
            ts,
            timestamp_s,
            x_image AS x,
            y_image AS y
        FROM fact_ball_position;
        """
    ]

    with engine.begin() as conn:
        for stmt in view_sql:
            conn.execute(text(stmt))
