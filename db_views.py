# db_views.py — Silver = passthrough + derived (from bronze), Gold = thin extract
# ----------------------------------------------------------------------------------
# - Coordinates are meters; no autoscale.
# - One primary bounce per swing:
#     primary = first FLOOR in (ball_hit+5ms, min(next_hit, ball_hit+2.5s)+20ms]
#     fallback = first ANY bounce in the same window.
# - Serve faults: within a point, every serve *before* the starting serve is a fault.
#   If the point never starts (double fault), all serves are faults.
# - Terminal result (last shot of point): WINNER iff (ball_speed>0 AND chosen-bounce coords are in-court);
#   otherwise ERROR. Winner id is derived accordingly.
# - Extra debug: bounce_in_doubles_d (floor-only), bounce_in_court_any_d (floor or any), terminal_basis_d
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
    missing_cols = [(t, c) for (t, c) in checks if not _column_exists(conn, t)]
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
            8.23::numeric      AS court_w_m,   -- SINGLES width
            23.77::numeric     AS court_l_m,
            8.23::numeric/2    AS half_w_m,    -- SINGLES half-width
            23.77::numeric/2   AS mid_y_m,
            0.50::numeric      AS serve_eps_m
        ),

        players_pair AS (
          SELECT session_id,
                 MIN(player_id) AS pmin,
                 MAX(player_id) AS pmax
          FROM dim_player
          GROUP BY session_id
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

        -- S2. Serve detection (fh_overhead in serve band) + serving side
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
            dp.sportai_player_uid AS server_uid,
            sf.ord_ts,
            sf.serving_side_d
          FROM serve_flags sf
          LEFT JOIN dim_player dp
            ON dp.session_id = sf.session_id AND dp.player_id = sf.player_id
          WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),

        -- S3. Point/game numbering
        serve_events_numbered AS (
          SELECT
            se.*,
            LAG(se.serving_side_d) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_side,
            LAG(se.server_uid)     OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_server_uid
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
                  WHEN sen.prev_server_uid IS NULL THEN 1
                  WHEN sen.server_uid     IS DISTINCT FROM sen.prev_server_uid THEN 1
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
            /* SINGLES: SportAI Y is already absolute 0..23.77 → no midline offset */
            b.y AS bounce_y_norm_m,
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
            sp.server_uid,
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

        -- S9. Why-null
        bounce_explain AS (
          SELECT
            se.session_id, se.swing_id,
            CASE WHEN sbp.bounce_id IS NOT NULL THEN NULL ELSE 'no_bounce_in_window' END AS why_null
          FROM swings_enriched se
          LEFT JOIN swing_bounce_primary sbp
            ON sbp.session_id=se.session_id AND sbp.swing_id=se.swing_id
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
            WHEN sbp.first_rally_shot_ix IS NULL THEN TRUE                    -- point never started → all serves fault
            WHEN sbp.start_serve_shot_ix IS NULL THEN TRUE                     -- safety
            WHEN sbp.shot_ix < sbp.start_serve_shot_ix THEN TRUE               -- pre-start serves
            WHEN sbp.shot_ix = sbp.start_serve_shot_ix THEN FALSE              -- starting serve
            ELSE NULL
          END AS is_serve_fault_d,

          -- terminal basis (last shot only): helps debug winner/error
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
                THEN (CASE WHEN sbp.player_id = pp.pmin THEN pp.pmax ELSE pp.pmin END)
                ELSE sbp.player_id
              END
            ELSE NULL
          END AS point_winner_player_id_d,

          be.why_null
        FROM swing_bounce_primary sbp
        JOIN vw_swing_silver vss USING (session_id, swing_id)
        JOIN players_pair pp       ON pp.session_id = sbp.session_id
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
          COALESCE((
            SELECT COUNT(*) FROM (
              SELECT bounce_id
              FROM vw_point_silver v2
              WHERE v2.session_id = vps.session_id AND v2.bounce_id IS NOT NULL
              GROUP BY bounce_id
              HAVING COUNT(*) > 1
            ) d
          ),0)                                                           AS dup_bounce_ids
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
