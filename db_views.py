# db_views.py — Silver = passthrough + derived (from bronze), Gold = thin extract
# ----------------------------------------------------------------------------------
# - Coordinates are meters; no autoscale.
# - One primary bounce per swing:
#     primary  = first FLOOR in (ball_hit+5ms, min(next_hit, ball_hit+2.5s)+20ms]
#     fallback = first ANY bounce in the same window.
# - Serve faults: within a point, every serve *before* the starting serve is a fault.
#   If the point never starts (double fault), all serves are faults.
# - Terminal result (last shot of point): WINNER iff (ball_speed>0 AND chosen-bounce coords are in-court);
#   otherwise ERROR. Winner id is derived accordingly.
# - Robustness & extras:
#   • Opponent derived from the two most-active swing players (prevents stray ids).
#   • Far/near per hitter from raw ball_hit_y sign (0 at net; +to camera, −away).
#   • Last-shot-only booleans: is_wide_last_d, is_long_last_d, out_axis_last_d.
#   • Game winner & counters only on the last point *by serve boundary*.
#   • Serve location 1–8 (serve_loc_18_d), court placement A–D (placement_ad_d), play type (play_d).
# ----------------------------------------------------------------------------------

from sqlalchemy import text
from typing import List

__all__ = ["init_views", "run_views", "VIEW_SQL_STMTS", "VIEW_NAMES", "CREATE_STMTS"]
VIEW_SQL_STMTS: List[str] = []

# ==================================================================================
# Utilities
# ==================================================================================

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
        "dim_session", "dim_player",
        "fact_swing", "fact_bounce",
        "fact_player_position", "fact_ball_position",
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
        ("fact_swing", "serve"),
        ("fact_swing", "serve_type"),
        ("fact_swing", "swing_type"),
        ("fact_bounce", "bounce_id"),
        ("fact_bounce", "x"),
        ("fact_bounce", "y"),
        ("fact_bounce", "bounce_type"),
        ("fact_bounce", "bounce_ts"),
        ("fact_bounce", "bounce_s"),
        ("fact_ball_position", "ts_s"),
        ("fact_ball_position", "x"),
        ("fact_ball_position", "y"),
    ]
    missing_cols = [(t, c) for (t, c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t, c) in missing_cols])
        raise RuntimeError(f"Missing required columns before creating views: {msg}")

def _drop_any(conn, name: str):
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

# ==================================================================================
# View manifest
# ==================================================================================

VIEW_NAMES = [
    "vw_swing_silver",
    "vw_ball_position_silver",
    "vw_bounce_silver",
    "vw_point_silver",
    "vw_bounce_stream_debug",
    "vw_point_bounces_debug",
]

LEGACY_OBJECTS = [
    "vw_point_order_by_serve", "vw_point_log", "vw_point_log_gold",
    "vw_point_summary", "vw_point_shot_log", "vw_shot_order_gold",
    "point_log_tbl", "point_summary_tbl",
]

# ==================================================================================
# CREATE statements
# ==================================================================================

CREATE_STMTS = {
    # ---------------------------- swings passthrough ----------------------------
    "vw_swing_silver": """
        CREATE OR REPLACE VIEW vw_swing_silver AS
        SELECT
          fs.session_id,
          fs.swing_id,
          fs.player_id,
          fs.rally_id,
          -- timing
          fs.start_s, fs.end_s, fs.ball_hit_s,
          fs.start_ts, fs.end_ts, fs.ball_hit_ts,
          -- ball at contact
          fs.ball_hit_x, fs.ball_hit_y,
          fs.ball_speed,
          -- labels/raw
          fs.serve, fs.serve_type AS serve_type, fs.swing_type, fs.is_in_rally,
          fs.ball_player_distance,
          fs.meta,
          ds.session_uid AS session_uid_d
        FROM fact_swing fs
        LEFT JOIN dim_session ds USING (session_id);
    """,

    # --------------------- ball position passthrough ---------------------
    "vw_ball_position_silver": """
        CREATE OR REPLACE VIEW vw_ball_position_silver AS
        SELECT session_id, ts_s, ts, x, y
        FROM fact_ball_position;
    """,

    # -------------------------- bounce passthrough -----------------------
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

    # ----------------------------- point silver -----------------------------
    "vw_point_silver": """
        CREATE OR REPLACE VIEW vw_point_silver AS
        WITH
        const AS (
          SELECT
            8.23::numeric     AS court_w_m,
            23.77::numeric     AS court_l_m,
            8.23::numeric/2   AS half_w_m,
            23.77::numeric/2   AS mid_y_m,
            6.40::numeric      AS service_box_depth_m,
            0.50::numeric      AS serve_eps_m
        ),

        /* Two real players in the match = the two with most swings. */
        swing_players AS (
          SELECT fs.session_id, fs.player_id, COUNT(*) AS n_sw
          FROM fact_swing fs
          GROUP BY fs.session_id, fs.player_id
        ),
        swing_players_ranked AS (
          SELECT sp.*,
                 ROW_NUMBER() OVER (PARTITION BY sp.session_id
                                    ORDER BY sp.n_sw DESC, sp.player_id) AS rn
          FROM swing_players sp
        ),
        players_pair AS (
          SELECT
            r.session_id,
            MAX(CASE WHEN r.rn=1 THEN r.player_id END) AS p1,
            MAX(CASE WHEN r.rn=2 THEN r.player_id END) AS p2
          FROM swing_players_ranked r
          WHERE r.rn <= 2
          GROUP BY r.session_id
        ),

        -- S1. Base swings (original ordering)
        swings AS (
          SELECT
            v.*,
            COALESCE(
              v.ball_hit_ts,
              v.start_ts,
              (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')
            ) AS ord_ts
          FROM vw_swing_silver v
        ),

        -- S1b. Infer per-player far/near using raw sign (0 at net)
        player_orientation AS (
          SELECT
            s.session_id,
            s.player_id,
            AVG(s.ball_hit_y) AS avg_hit_y,
            (AVG(s.ball_hit_y) < 0) AS is_far_side_d
          FROM swings s
          WHERE s.ball_hit_y IS NOT NULL
          GROUP BY s.session_id, s.player_id
        ),

        -- S2. Serve detection (fh_overhead in serve band) + serving side (from server's end)
        serve_flags AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
            s.ball_hit_x AS x_ref, s.ball_hit_y AS y_ref,
            (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
            CASE
              WHEN s.ball_hit_y IS NULL THEN NULL
              ELSE (s.ball_hit_y <= (SELECT serve_eps_m FROM const)
                OR  s.ball_hit_y >= (SELECT court_l_m FROM const) - (SELECT serve_eps_m FROM const))
            END AS inside_serve_band,
            CASE
              WHEN s.ball_hit_y IS NULL OR s.ball_hit_x IS NULL THEN NULL
              WHEN s.ball_hit_y < (SELECT mid_y_m FROM const)
                THEN CASE WHEN s.ball_hit_x < (SELECT half_w_m FROM const) THEN 'deuce' ELSE 'ad' END
              ELSE CASE WHEN s.ball_hit_x > (SELECT half_w_m FROM const) THEN 'deuce' ELSE 'ad' END
            END AS serving_side_d
          FROM swings s
        ),

        serve_events AS (
          SELECT
            sf.session_id,
            sf.swing_id           AS srv_swing_id,
            sf.player_id          AS server_id,
            sf.ord_ts,
            sf.serving_side_d
          FROM serve_flags sf
          WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),

        -- S3. Point/game numbering
        serve_events_numbered AS (
          SELECT
            se.*,
            LAG(se.serving_side_d) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_side,
            LAG(se.server_id)      OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_server
          FROM serve_events se
        ),
        serve_points AS (
          SELECT
            sen.*,
            SUM(CASE
                  WHEN sen.prev_side IS NULL THEN 1
                  WHEN sen.serving_side_d IS DISTINCT FROM sen.prev_side THEN 1
                  ELSE 0
                END)
              OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts, sen.srv_swing_id
                    ROWS UNBOUNDED PRECEDING) AS point_number_d,
            SUM(CASE
                  WHEN sen.prev_server IS NULL THEN 1
                  WHEN sen.server_id IS DISTINCT FROM sen.prev_server THEN 1
                  ELSE 0
                END)
              OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts, sen.srv_swing_id
                    ROWS UNBOUNDED PRECEDING) AS game_number_d
          FROM serve_events_numbered sen
        ),
        serve_points_ix AS (
          SELECT
            sp.*,
            sp.point_number_d
              - MIN(sp.point_number_d) OVER (PARTITION BY sp.session_id, sp.game_number_d)
              + 1 AS point_in_game_d
          FROM serve_points sp
        ),

        /* Last point in each game by serve-boundary */
        game_last_point AS (
          SELECT session_id, game_number_d, MAX(point_in_game_d) AS last_point_in_game_d
          FROM serve_points_ix
          GROUP BY session_id, game_number_d
        ),

        -- S4. Normalize bounces + unified TS
        bounces_norm AS (
          SELECT
            b.session_id,
            b.bounce_id,
            b.bounce_ts,
            b.bounce_s,
            b.bounce_type,
            b.x AS bounce_x_center_m,
            b.y AS bounce_y_center_m,
            ((SELECT mid_y_m FROM const) + b.y) AS bounce_y_norm_m,
            COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')) AS bounce_ts_pref
          FROM vw_bounce_silver b
        ),

        -- S5. Attach swings to most recent serve
        swings_in_point AS (
          SELECT
            s.*,
            sp.point_number_d,
            sp.game_number_d,
            sp.point_in_game_d,
            sp.server_id,
            sp.serving_side_d
          FROM swings s
          LEFT JOIN LATERAL (
            SELECT sp.* FROM serve_points_ix sp
            WHERE sp.session_id = s.session_id AND sp.ord_ts <= s.ord_ts
            ORDER BY sp.ord_ts DESC
            LIMIT 1
          ) sp ON TRUE
        ),

        -- S5b. Mark serves
        swings_with_serve AS (
          SELECT
            sip.*,
            EXISTS (
              SELECT 1 FROM serve_flags sf
              WHERE sf.session_id = sip.session_id
                AND sf.swing_id   = sip.swing_id
                AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
            ) AS serve_d
          FROM swings_in_point sip
        ),

        -- S6. Per-point shot indices (unambiguous)
        swings_numbered AS (
          SELECT
            sps.*,
            ROW_NUMBER() OVER (
              PARTITION BY sps.session_id, sps.point_number_d
              ORDER BY sps.ord_ts, sps.swing_id
            ) AS shot_ix,
            COUNT(*) OVER (PARTITION BY sps.session_id, sps.point_number_d) AS last_shot_ix,
            LEAD(sps.ball_hit_ts) OVER (PARTITION BY sps.session_id ORDER BY sps.ord_ts, sps.swing_id) AS next_ball_hit_ts,
            LEAD(sps.ball_hit_s)  OVER (PARTITION BY sps.session_id ORDER BY sps.ord_ts, sps.swing_id) AS next_ball_hit_s
          FROM swings_with_serve sps
        ),

        -- First non-serve shot index per point (NULL if point never starts)
        point_first_rally AS (
          SELECT
            session_id, point_number_d,
            MIN(shot_ix) FILTER (WHERE NOT serve_d) AS first_rally_shot_ix
          FROM swings_numbered
          GROUP BY session_id, point_number_d
        ),

        -- Starting serve = last serve immediately before first non-serve
        point_starting_serve AS (
          SELECT
            sn.session_id, sn.point_number_d,
            MAX(sn.shot_ix) AS start_serve_shot_ix
          FROM swings_numbered sn
          JOIN point_first_rally pfr
            ON pfr.session_id = sn.session_id
           AND pfr.point_number_d = sn.point_number_d
          WHERE sn.serve_d
            AND pfr.first_rally_shot_ix IS NOT NULL
            AND sn.shot_ix < pfr.first_rally_shot_ix
          GROUP BY sn.session_id, sn.point_number_d
        ),

        -- Enrich with point start markers
        swings_enriched AS (
          SELECT
            sn.*,
            pfr.first_rally_shot_ix,
            pss.start_serve_shot_ix
          FROM swings_numbered sn
          LEFT JOIN point_first_rally pfr
            ON pfr.session_id = sn.session_id AND pfr.point_number_d = sn.point_number_d
          LEFT JOIN point_starting_serve pss
            ON pss.session_id = sn.session_id AND pss.point_number_d = sn.point_number_d
        ),

        -- S7. Window with guards
        swing_windows AS (
          SELECT
            se.*,
            COALESCE(se.ball_hit_ts, (TIMESTAMP 'epoch' + se.ball_hit_s * INTERVAL '1 second')) AS start_ts_pref
          FROM swings_enriched se
        ),
        swing_windows_cap AS (
          SELECT
            sw.*,
            LEAST(
              sw.start_ts_pref + INTERVAL '2.5 seconds',
              COALESCE(sw.next_ball_hit_ts, sw.start_ts_pref + INTERVAL '2.5 seconds')
            ) AS end_ts_pref_raw,
            LEAST(
              sw.start_ts_pref + INTERVAL '2.5 seconds',
              COALESCE(sw.next_ball_hit_ts, sw.start_ts_pref + INTERVAL '2.5 seconds')
            ) + INTERVAL '20 milliseconds' AS end_ts_pref,
            sw.start_ts_pref + INTERVAL '5 milliseconds' AS start_ts_guard
          FROM swing_windows sw
        ),

        -- S8A. First FLOOR in window
        swing_bounce_floor AS (
          SELECT
            swc.swing_id, swc.session_id, swc.point_number_d, swc.shot_ix,
            b.bounce_id, b.bounce_ts, b.bounce_s,
            b.bounce_x_center_m, b.bounce_y_center_m, b.bounce_y_norm_m,
            b.bounce_type AS bounce_type_raw
          FROM swing_windows_cap swc
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM bounces_norm b
            WHERE b.session_id = swc.session_id
              AND b.bounce_type = 'floor'
              AND b.bounce_ts_pref >  swc.start_ts_guard
              AND b.bounce_ts_pref <= swc.end_ts_pref
            ORDER BY b.bounce_ts_pref, b.bounce_id
            LIMIT 1
          ) b ON TRUE
        ),

        -- S8B. First ANY in window
        swing_bounce_any AS (
          SELECT
            swc.swing_id, swc.session_id, swc.point_number_d, swc.shot_ix,
            b.bounce_id   AS any_bounce_id,
            b.bounce_ts   AS any_bounce_ts,
            b.bounce_s    AS any_bounce_s,
            b.bounce_x_center_m AS any_bounce_x_center_m,
            b.bounce_y_center_m AS any_bounce_y_center_m,
            b.bounce_y_norm_m   AS any_bounce_y_norm_m,
            b.bounce_type       AS any_bounce_type
          FROM swing_windows_cap swc
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM bounces_norm b
            WHERE b.session_id = swc.session_id
              AND b.bounce_ts_pref >  swc.start_ts_guard
              AND b.bounce_ts_pref <= swc.end_ts_pref
            ORDER BY b.bounce_ts_pref, b.bounce_id
            LIMIT 1
          ) b ON TRUE
        ),

        -- S8C. Primary bounce
        swing_bounce_primary AS (
          SELECT
            se.session_id, se.swing_id, se.point_number_d, se.shot_ix, se.last_shot_ix,
            COALESCE(f.bounce_id,         a.any_bounce_id)          AS bounce_id,
            COALESCE(f.bounce_ts,         a.any_bounce_ts)          AS bounce_ts,
            COALESCE(f.bounce_s,          a.any_bounce_s)           AS bounce_s,
            COALESCE(f.bounce_x_center_m, a.any_bounce_x_center_m)  AS bounce_x_center_m,
            COALESCE(f.bounce_y_center_m, a.any_bounce_y_center_m)  AS bounce_y_center_m,
            COALESCE(f.bounce_y_norm_m,   a.any_bounce_y_norm_m)    AS bounce_y_norm_m,
            COALESCE(f.bounce_type_raw,   a.any_bounce_type)        AS bounce_type_raw,
            CASE WHEN f.bounce_id IS NOT NULL THEN 'floor'::text
                 WHEN a.any_bounce_id IS NOT NULL THEN 'any'::text
                 ELSE NULL::text
            END AS primary_source_d,
            se.serve_d,
            se.first_rally_shot_ix,
            se.start_serve_shot_ix,
            se.player_id,
            se.server_id,
            se.game_number_d,
            se.point_in_game_d,
            se.serving_side_d,
            se.start_s, se.end_s, se.ball_hit_s,
            se.start_ts, se.end_ts, se.ball_hit_ts,
            se.ball_hit_x, se.ball_hit_y,
            se.ball_speed,
            se.swing_type AS swing_type_raw
          FROM swings_enriched se
          LEFT JOIN swing_bounce_floor f
            ON f.session_id=se.session_id AND f.swing_id=se.swing_id
          LEFT JOIN swing_bounce_any a
            ON a.session_id=se.session_id AND a.swing_id=se.swing_id
        ),

        -- S8D. Per-point outcome (last-shot rows only) for scoring
        point_outcome AS (
          SELECT
            sbp.session_id, sbp.point_number_d, sbp.game_number_d, sbp.point_in_game_d,
            sbp.server_id,
            sbp.player_id AS hitter_id,
            sbp.shot_ix, sbp.last_shot_ix,
            sbp.ball_speed, sbp.bounce_id,
            sbp.bounce_x_center_m, sbp.bounce_y_norm_m,
            CASE
              WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN TRUE
              WHEN sbp.bounce_id IS NULL THEN TRUE
              WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
                    AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN FALSE
              ELSE TRUE
            END AS is_error_last,
            CASE
              WHEN (
                CASE
                  WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN TRUE
                  WHEN sbp.bounce_id IS NULL THEN TRUE
                  WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
                        AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN FALSE
                  ELSE TRUE
                END
              ) IS TRUE
              THEN NULL
              ELSE sbp.player_id
            END AS point_winner_if_in_d
          FROM swing_bounce_primary sbp
          WHERE sbp.shot_ix = sbp.last_shot_ix
        ),
        point_outcome_winner AS (
          SELECT
            po.*,
            CASE
              WHEN po.point_winner_if_in_d IS NOT NULL THEN po.point_winner_if_in_d
              ELSE CASE WHEN po.hitter_id = pp.p1 THEN pp.p2 ELSE pp.p1 END
            END AS point_winner_player_id_d
          FROM point_outcome po
          JOIN players_pair pp ON pp.session_id = po.session_id
        ),

        -- S9. Why-null
        bounce_explain AS (
          SELECT
            se.session_id, se.swing_id,
            CASE WHEN sbp.bounce_id IS NOT NULL THEN NULL ELSE 'no_bounce_in_window' END AS why_null
          FROM swings_enriched se
          LEFT JOIN swing_bounce_primary sbp
            ON sbp.session_id=se.session_id AND sbp.swing_id=se.swing_id
        ),

        -- S10. Per-game scoring accumulation (server-first)
        points_accum AS (
          SELECT
            pow.*,
            SUM(CASE WHEN pow.point_winner_player_id_d = pow.server_id THEN 1 ELSE 0 END)
              OVER (PARTITION BY pow.session_id, pow.game_number_d
                    ORDER BY pow.point_in_game_d
                    ROWS UNBOUNDED PRECEDING) AS server_pts_cum,
            SUM(CASE WHEN pow.point_winner_player_id_d <> pow.server_id THEN 1 ELSE 0 END)
              OVER (PARTITION BY pow.session_id, pow.game_number_d
                    ORDER BY pow.point_in_game_d
                    ROWS UNBOUNDED PRECEDING) AS recv_pts_cum
          FROM point_outcome_winner pow
        ),

        -- Scoring text; also enforce boundary-of-serve for game winner
        points_scored AS (
          SELECT
            pa.*,
            glp.last_point_in_game_d,
            (pa.point_in_game_d = glp.last_point_in_game_d) AS is_last_point_by_serve,
            CASE
              WHEN pa.server_pts_cum >= 4 OR pa.recv_pts_cum >= 4 THEN
                   CASE WHEN ABS(pa.server_pts_cum - pa.recv_pts_cum) >= 2 THEN TRUE ELSE FALSE END
              ELSE FALSE
            END AS is_game_end_scoring,
            CASE
              WHEN pa.server_pts_cum >= 3 AND pa.recv_pts_cum >= 3 THEN
                CASE
                  WHEN pa.server_pts_cum = pa.recv_pts_cum     THEN '40-40'
                  WHEN pa.server_pts_cum = pa.recv_pts_cum + 1 THEN 'Ad-40'
                  WHEN pa.recv_pts_cum  = pa.server_pts_cum + 1 THEN '40-Ad'
                  ELSE '40-40'
                END
              ELSE
                (CASE pa.server_pts_cum WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END)
                || '-' ||
                (CASE pa.recv_pts_cum   WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END)
            END AS point_score_text_d
          FROM points_accum pa
          JOIN game_last_point glp
            ON glp.session_id = pa.session_id AND glp.game_number_d = pa.game_number_d
        ),

        points_scored_winner AS (
          SELECT
            ps.*,
            (ps.is_game_end_scoring AND ps.is_last_point_by_serve) AS is_game_end_d,
            CASE
              WHEN (ps.is_game_end_scoring AND ps.is_last_point_by_serve) THEN
                CASE
                  WHEN ps.server_pts_cum > ps.recv_pts_cum THEN ps.server_id
                  ELSE CASE WHEN ps.server_id = pp.p1 THEN pp.p2 ELSE pp.p1 END
                END
              ELSE NULL
            END AS game_winner_player_id_d
          FROM points_scored ps
          JOIN players_pair pp ON pp.session_id = ps.session_id
        ),

        games_running AS (
          SELECT
            psw.*,
            SUM(CASE WHEN psw.is_game_end_d AND psw.game_winner_player_id_d = psw.server_id THEN 1 ELSE 0 END)
              OVER (PARTITION BY psw.session_id
                    ORDER BY psw.game_number_d, psw.point_in_game_d
                    ROWS UNBOUNDED PRECEDING) AS games_server_after_d,
            SUM(CASE WHEN psw.is_game_end_d AND psw.game_winner_player_id_d <> psw.server_id THEN 1 ELSE 0 END)
              OVER (PARTITION BY psw.session_id
                    ORDER BY psw.game_number_d, psw.point_in_game_d
                    ROWS UNBOUNDED PRECEDING) AS games_receiver_after_d
          FROM points_scored_winner psw
        )

        -- FINAL
        SELECT
          sbp.session_id,
          vss.session_uid_d,
          sbp.swing_id,
          sbp.player_id,
          sbp.start_s, sbp.end_s, sbp.ball_hit_s,
          sbp.start_ts, sbp.end_ts, sbp.ball_hit_ts,
          sbp.ball_hit_x, sbp.ball_hit_y,
          sbp.ball_speed,
          sbp.swing_type_raw,

          sbp.bounce_id,
          sbp.bounce_ts             AS bounce_ts_d,
          sbp.bounce_type_raw,
          sbp.bounce_s              AS bounce_s_d,
          sbp.bounce_x_center_m     AS bounce_x_center_m,
          sbp.bounce_y_center_m     AS bounce_y_center_m,
          sbp.bounce_y_norm_m       AS bounce_y_norm_m,
          sbp.primary_source_d,

          sbp.serve_d,
          sbp.first_rally_shot_ix,
          sbp.start_serve_shot_ix,

          -- point / game context
          sbp.point_number_d,
          sbp.game_number_d,
          sbp.point_in_game_d,
          sbp.serving_side_d,
          sbp.server_id,

          -- last-in-point: by shot index
          (sbp.shot_ix = sbp.last_shot_ix) AS is_last_in_point_d,

          -- in/out using FLOOR-only (original) and ANY (new)
          CASE
            WHEN sbp.bounce_id IS NULL OR sbp.bounce_type_raw <> 'floor' THEN NULL
            ELSE (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
               AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const))
          END AS bounce_in_doubles_d,

          CASE
            WHEN sbp.bounce_id IS NULL THEN NULL
            ELSE (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
               AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const))
          END AS bounce_in_court_any_d,

          -- serve faults before the starting serve (starting serve is NOT a fault)
          CASE
            WHEN sbp.serve_d IS NOT TRUE THEN NULL
            WHEN sbp.first_rally_shot_ix IS NULL THEN TRUE
            WHEN sbp.start_serve_shot_ix IS NULL THEN TRUE
            WHEN sbp.shot_ix < sbp.start_serve_shot_ix THEN TRUE
            WHEN sbp.shot_ix = sbp.start_serve_shot_ix THEN FALSE
            ELSE NULL
          END AS is_serve_fault_d,

          -- terminal basis (last shot only)
          CASE
            WHEN sbp.shot_ix <> sbp.last_shot_ix THEN NULL
            ELSE CASE
              WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN 'no_speed'
              WHEN sbp.bounce_id IS NULL THEN 'no_bounce'
              WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
                    AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN 'in'
              ELSE 'out'
            END
          END AS terminal_basis_d,

          -- terminal ERROR (last shot only) using ANY-bounce in/out
          CASE
            WHEN sbp.shot_ix <> sbp.last_shot_ix THEN NULL
            ELSE CASE
              WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN TRUE
              WHEN sbp.bounce_id IS NULL THEN TRUE
              WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
                    AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN FALSE
              ELSE TRUE
            END
          END AS is_error_d,

          -- point winner on last shot only (error ⇒ opponent, else hitter)
          CASE
            WHEN sbp.shot_ix = sbp.last_shot_ix THEN
              CASE
                WHEN (
                  CASE
                    WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN TRUE
                    WHEN sbp.bounce_id IS NULL THEN TRUE
                    WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
                          AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN FALSE
                    ELSE TRUE
                  END
                ) IS TRUE
                THEN (CASE WHEN sbp.player_id = pp.p1 THEN pp.p2 ELSE pp.p1 END)
                ELSE sbp.player_id
              END
            ELSE NULL
          END AS point_winner_player_id_d,

          -- orientation for LONG logic
          pdir.is_far_side_d AS player_is_far_side_d,

          -- last-shot only classification for WIDE/LONG/BOTH
          CASE
            WHEN sbp.shot_ix <> sbp.last_shot_ix OR sbp.bounce_id IS NULL THEN NULL
            ELSE (sbp.bounce_x_center_m < 0 OR sbp.bounce_x_center_m > (SELECT court_w_m FROM const))
          END AS is_wide_last_d,

          CASE
            WHEN sbp.shot_ix <> sbp.last_shot_ix OR sbp.bounce_id IS NULL THEN NULL
            ELSE CASE
              WHEN pdir.is_far_side_d THEN (sbp.bounce_y_norm_m < 0)                    -- far hitter long
              ELSE (sbp.bounce_y_norm_m > (SELECT court_l_m FROM const))                -- near hitter long
            END
          END AS is_long_last_d,

          CASE
            WHEN sbp.shot_ix <> sbp.last_shot_ix OR sbp.bounce_id IS NULL THEN NULL
            ELSE CASE
              WHEN (sbp.bounce_x_center_m < 0 OR sbp.bounce_x_center_m > (SELECT court_w_m FROM const))
                   AND (CASE WHEN pdir.is_far_side_d THEN sbp.bounce_y_norm_m < 0 ELSE sbp.bounce_y_norm_m > (SELECT court_l_m FROM const) END)
                THEN 'both'
              WHEN (sbp.bounce_x_center_m < 0 OR sbp.bounce_x_center_m > (SELECT court_w_m FROM const))
                THEN 'wide'
              WHEN (CASE WHEN pdir.is_far_side_d THEN sbp.bounce_y_norm_m < 0 ELSE sbp.bounce_y_norm_m > (SELECT court_l_m FROM const) END)
                THEN 'long'
              ELSE NULL
            END
          END AS out_axis_last_d,

          -- ===== Serve location 1–8 (valid starting serves only) =====
          CASE
            WHEN sbp.serve_d IS TRUE
             AND sbp.start_serve_shot_ix IS NOT NULL
             AND sbp.shot_ix = sbp.start_serve_shot_ix
            THEN (
              WITH coords AS (
                SELECT
                  COALESCE(sbp.bounce_x_center_m,
                           (SELECT court_w_m FROM const) - sbp.ball_hit_x) AS sx,
                  COALESCE(sbp.bounce_y_norm_m,
                           (SELECT mid_y_m FROM const) - sbp.ball_hit_y)     AS sy
              ),
              flags AS (
                SELECT
                  sx, sy,
                  (sy < (SELECT mid_y_m FROM const)) AS is_far_end,
                  CASE
                    WHEN sy < (SELECT mid_y_m FROM const)
                      THEN (sx < (SELECT half_w_m FROM const))      -- far: deuce = left half
                    ELSE (sx > (SELECT half_w_m FROM const))         -- near: deuce = right half
                  END AS is_deuce_box,
                  CASE
                    WHEN sx < (SELECT half_w_m FROM const) THEN sx
                    ELSE (SELECT court_w_m FROM const) - sx
                  END AS x_from_sideline,
                  CASE
                    WHEN sy < (SELECT mid_y_m FROM const)
                      THEN (sy > (SELECT mid_y_m FROM const) - (SELECT service_box_depth_m FROM const)/2.0)
                    ELSE (sy < (SELECT mid_y_m FROM const) + (SELECT service_box_depth_m FROM const)/2.0)
                  END AS is_short
                FROM coords
              )
              SELECT
                CASE
                  WHEN is_deuce_box THEN
                    CASE
                      WHEN x_from_sideline < (SELECT half_w_m FROM const)/2.0 AND is_short THEN 1
                      WHEN x_from_sideline < (SELECT half_w_m FROM const)/2.0 AND NOT is_short THEN 2
                      WHEN x_from_sideline >= (SELECT half_w_m FROM const)/2.0 AND NOT is_short THEN 3
                      ELSE 4
                    END
                  ELSE
                    4 + CASE
                          WHEN x_from_sideline < (SELECT half_w_m FROM const)/2.0 AND is_short THEN 1
                          WHEN x_from_sideline < (SELECT half_w_m FROM const)/2.0 AND NOT is_short THEN 2
                          WHEN x_from_sideline >= (SELECT half_w_m FROM const)/2.0 AND NOT is_short THEN 3
                          ELSE 4
                        END
                END
              FROM flags
            )
            ELSE NULL
          END AS serve_loc_18_d,

          -- ===== Court placement A–D (any swing; bounce floor preferred) =====
          (
            WITH pt AS (
              SELECT COALESCE(sbp.bounce_y_norm_m,
                              (SELECT mid_y_m FROM const) + sbp.ball_hit_y) AS py
            ),
            band AS (
              SELECT
                py,
                (py < (SELECT mid_y_m FROM const)) AS far_end,
                CASE
                  WHEN py < (SELECT mid_y_m FROM const)
                    THEN (py / (SELECT mid_y_m FROM const))                                -- far: 0..mid_y
                  ELSE ((SELECT court_l_m FROM const) - py) / (SELECT mid_y_m FROM const)  -- near: reverse
                END AS t
              FROM pt
            )
            SELECT CASE
              WHEN t < 0.25 THEN 'A'
              WHEN t < 0.50 THEN 'B'
              WHEN t < 0.75 THEN 'C'
              ELSE 'D'
            END
            FROM band
          ) AS placement_ad_d,

          -- ===== Play type =====
          CASE
            WHEN sbp.serve_d THEN 'serve'
            WHEN sbp.shot_ix = sbp.first_rally_shot_ix THEN 'return'
            WHEN ABS(sbp.ball_hit_y) <= (SELECT service_box_depth_m FROM const) THEN 'net'
            ELSE 'baseline'
          END AS play_d,

          -- Scoring (only on last-shot rows)
          gr.point_score_text_d,
          gr.is_game_end_d,
          gr.game_winner_player_id_d,
          gr.games_server_after_d,
          gr.games_receiver_after_d,
          CASE
            WHEN gr.point_score_text_d IS NULL THEN NULL
            ELSE (gr.games_server_after_d::text || '-' || gr.games_receiver_after_d::text)
          END AS game_score_text_after_d,

          be.why_null
        FROM swing_bounce_primary sbp
        JOIN vw_swing_silver vss USING (session_id, swing_id)
        JOIN players_pair pp       ON pp.session_id = sbp.session_id
        LEFT JOIN player_orientation pdir
               ON pdir.session_id = sbp.session_id AND pdir.player_id = sbp.player_id
        LEFT JOIN games_running gr
               ON gr.session_id = sbp.session_id
              AND gr.point_number_d = sbp.point_number_d
        LEFT JOIN bounce_explain be
               ON be.session_id = sbp.session_id AND be.swing_id = sbp.swing_id
        ORDER BY sbp.session_id, sbp.point_number_d, sbp.shot_ix, sbp.swing_id;
    """,

    # ------------------------ DEBUG: per-swing window ------------------------
    "vw_bounce_stream_debug": """
        CREATE OR REPLACE VIEW vw_bounce_stream_debug AS
        WITH s AS (
          SELECT
            v.session_id, v.swing_id,
            COALESCE(v.ball_hit_ts, (TIMESTAMP 'epoch' + v.ball_hit_s * INTERVAL '1 second')) AS start_ts_pref,
            LEAD(COALESCE(v.ball_hit_ts, (TIMESTAMP 'epoch' + v.ball_hit_s * INTERVAL '1 second')))
              OVER (PARTITION BY v.session_id ORDER BY
                    COALESCE(v.ball_hit_ts, (TIMESTAMP 'epoch' + v.ball_hit_s * INTERVAL '1 second')), v.swing_id) AS next_hit_pref
          FROM vw_swing_silver v
        ),
        base AS (
          SELECT
            vps.session_id,
            vps.session_uid_d,
            vps.swing_id,
            vps.point_number_d,
            vps.game_number_d,
            vps.point_in_game_d,
            vps.serve_d,
            vps.ball_hit_ts,
            vps.ball_hit_s,
            vps.start_ts,
            vps.start_s,
            s.start_ts_pref,
            LEAST(
              s.start_ts_pref + INTERVAL '2.5 seconds',
              COALESCE(s.next_hit_pref, s.start_ts_pref + INTERVAL '2.5 seconds')
            ) AS end_ts_pref_raw,
            LEAST(
              s.start_ts_pref + INTERVAL '2.5 seconds',
              COALESCE(s.next_hit_pref, s.start_ts_pref + INTERVAL '2.5 seconds')
            ) + INTERVAL '20 milliseconds' AS end_ts_pref,
            s.start_ts_pref + INTERVAL '5 milliseconds' AS start_ts_guard,
            vps.bounce_id           AS chosen_bounce_id,
            vps.bounce_type_raw     AS chosen_type,
            vps.bounce_ts_d         AS chosen_bounce_ts
          FROM vw_point_silver vps
          JOIN s
            ON s.session_id = vps.session_id AND s.swing_id = vps.swing_id
        ),
        any_in_window AS (
          SELECT
            b.session_id, b.swing_id,
            EXISTS (
              SELECT 1
              FROM vw_bounce_silver bs
              WHERE bs.session_id = b.session_id
                AND COALESCE(bs.bounce_ts, (TIMESTAMP 'epoch' + bs.bounce_s * INTERVAL '1 second'))
                    >  b.start_ts_guard
                AND COALESCE(bs.bounce_ts, (TIMESTAMP 'epoch' + bs.bounce_s * INTERVAL '1 second'))
                    <= b.end_ts_pref
            ) AS had_any
          FROM base b
        ),
        floor_in_window AS (
          SELECT
            b.session_id, b.swing_id,
            EXISTS (
              SELECT 1
              FROM vw_bounce_silver bs
              WHERE bs.session_id = b.session_id
                AND bs.bounce_type = 'floor'
                AND COALESCE(bs.bounce_ts, (TIMESTAMP 'epoch' + bs.bounce_s * INTERVAL '1 second'))
                    >  b.start_ts_guard
                AND COALESCE(bs.bounce_ts, (TIMESTAMP 'epoch' + bs.bounce_s * INTERVAL '1 second'))
                    <= b.end_ts_pref
            ) AS had_floor
          FROM base b
        )
        SELECT
          b.session_id,
          b.session_uid_d,
          b.swing_id,
          b.point_number_d,
          b.game_number_d,
          b.point_in_game_d,
          b.serve_d,
          b.start_ts_pref,
          b.start_ts_guard,
          b.end_ts_pref_raw,
          b.end_ts_pref,
          (b.chosen_bounce_ts - b.end_ts_pref_raw) AS dt_chosen_to_end_raw,
          f.had_floor,
          a.had_any,
          b.chosen_bounce_id,
          b.chosen_type,
          CASE
            WHEN b.chosen_bounce_id IS NULL THEN 'none'
            WHEN b.chosen_type = 'floor' THEN 'floor'
            WHEN f.had_floor THEN 'any_fallback'
            ELSE 'any_only'
          END AS primary_source_explain
        FROM base b
        LEFT JOIN floor_in_window f
          ON f.session_id = b.session_id AND f.swing_id = b.swing_id
        LEFT JOIN any_in_window a
          ON a.session_id = b.session_id AND a.swing_id = b.swing_id;
    """,

    # ------------------------ DEBUG: per-session summary ----------------------
    "vw_point_bounces_debug": """
        CREATE OR REPLACE VIEW vw_point_bounces_debug AS
        SELECT
          vps.session_id,
          vps.session_uid_d,
          COUNT(*)                                                        AS swings_total,
          COUNT(*) FILTER (WHERE vps.bounce_id IS NOT NULL)               AS swings_with_any_bounce,
          COUNT(*) FILTER (WHERE vps.bounce_type_raw = 'floor')           AS swings_with_floor_primary,
          COUNT(*) FILTER (WHERE vps.bounce_type_raw <> 'floor' AND vps.bounce_id IS NOT NULL)
                                                                          AS swings_with_racquet_primary,
          COUNT(*) FILTER (WHERE vps.bounce_id IS NULL)                   AS swings_with_no_bounce,
          COUNT(DISTINCT vps.bounce_id) FILTER (WHERE vps.bounce_id IS NOT NULL)
                                                                          AS distinct_bounce_ids,
          COALESCE((SELECT COUNT(*) FROM (
              SELECT bounce_id
              FROM vw_point_silver v2
              WHERE v2.session_id = vps.session_id AND v2.bounce_id IS NOT NULL
              GROUP BY bounce_id
              HAVING COUNT(*) > 1
            ) d),0)                                                       AS dup_bounce_ids
        FROM vw_point_silver vps
        GROUP BY vps.session_id, vps.session_uid_d;
    """,
}

# ==================================================================================
# Apply
# ==================================================================================

def _apply_views(engine):
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

# Back-compat
init_views = _apply_views
run_views  = _apply_views
