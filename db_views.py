# db_views.py — Silver = passthrough + derived (from bronze), Gold = thin extract
# ----------------------------------------------------------------------------------
# CHANGES (this version):
# - Robust swing→bounce matching (first FLOOR bounce before the next hit)
# - Reasonable serve-geometry check (is_serve_in_d) using court constants
# - Point winner (point_winner_id_d) via last shot in point with error/fault logic
# - Per-point game score after the point (score_str_d): 0/15/30/40/AD/game
# ----------------------------------------------------------------------------------
# NOTES (unchanged):
# - SportAI sends coordinates in METERS. We do not autoscale; treat x/y as meters.
# - Serve bucket (1–8) is computed from the FIRST FLOOR bounce of the serve.
# - Rally depth/location for non-serve shots is computed from the FIRST FLOOR bounce.
# - We also expose the FIRST ANY bounce per swing (id/type/x/y).
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
        "dim_session", "dim_player", "dim_rally",
        "fact_swing", "fact_bounce", "fact_player_position", "fact_ball_position",
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
        ("fact_swing", "swing_type"),
        ("fact_bounce", "bounce_id"),
        ("fact_bounce", "x"),
        ("fact_bounce", "y"),
        ("fact_bounce", "bounce_type"),
        ("fact_bounce", "bounce_ts"),
        ("fact_bounce", "bounce_s"),
        ("fact_player_position", "player_id"),
        ("fact_player_position", "ts_s"),
        ("fact_player_position", "x"),
        ("fact_player_position", "y"),
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
    stmts = []
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
    "vw_point_gold",
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
          fs.serve, fs.serve_type, fs.swing_type, fs.is_in_rally,
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
            10.97::numeric       AS court_w_m,          -- doubles width
            23.77::numeric       AS court_l_m,
            10.97::numeric/2     AS half_w_m,
            23.77::numeric / 2   AS mid_y_m,
            6.40::numeric        AS svc_box_depth_m,     -- net to service line
            0.50::numeric        AS serve_eps_m,
            2.5::numeric         AS short_m,
            5.5::numeric         AS mid_m
        ),

        -- D2. Base swings (ordered)
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

        -- D3. Hitter UID
        hitter_uid AS (
          SELECT s.session_id, s.swing_id, dp.sportai_player_uid AS player_uid
          FROM swings s
          LEFT JOIN dim_player dp
            ON dp.session_id = s.session_id AND dp.player_id = s.player_id
        ),

        -- D4. Serve flags (ball coords only)
        serve_flags AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
            s.ball_hit_x AS x_ref,
            s.ball_hit_y AS y_ref,
            (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
            CASE
              WHEN s.ball_hit_y IS NULL THEN NULL
              ELSE (s.ball_hit_y <= (SELECT serve_eps_m FROM const)
                 OR  s.ball_hit_y >= (SELECT court_l_m FROM const) - (SELECT serve_eps_m FROM const))
            END AS inside_serve_band
          FROM swings s
        ),

        -- D5. Serve events + serving side (ball x/y). Keep x_ref,y_ref for geometry later.
        serve_events AS (
          SELECT
            sf.session_id,
            sf.swing_id           AS srv_swing_id,
            sf.player_id          AS server_id,
            dp.sportai_player_uid AS server_uid,
            sf.ord_ts,
            sf.x_ref, sf.y_ref,
            CASE
              WHEN sf.y_ref IS NULL OR sf.x_ref IS NULL THEN NULL
              WHEN sf.y_ref < (SELECT mid_y_m FROM const)
                THEN CASE WHEN sf.x_ref < (SELECT half_w_m FROM const) THEN 'deuce' ELSE 'ad' END
              ELSE CASE WHEN sf.x_ref > (SELECT half_w_m FROM const) THEN 'deuce' ELSE 'ad' END
            END AS serving_side_d
          FROM serve_flags sf
          LEFT JOIN dim_player dp
            ON dp.session_id = sf.session_id AND dp.player_id = sf.player_id
          WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),

        -- D6. Number serves into points & games
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
                END
            ) OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts, sen.srv_swing_id
                    ROWS UNBOUNDED PRECEDING) AS point_number_d,
            SUM(CASE
                  WHEN sen.prev_server_uid IS NULL THEN 1
                  WHEN sen.server_uid     IS DISTINCT FROM sen.prev_server_uid THEN 1
                  ELSE 0
                END
            ) OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts, sen.srv_swing_id
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

        -- D7. Normalize ALL bounces (ANY type). We treat fact_bounce.x/y as meters.
        bounces_norm AS (
          SELECT
            b.session_id,
            b.bounce_id,
            b.bounce_ts,
            b.bounce_s,
            b.bounce_type,
            b.x AS x_m_center,
            b.y AS y_m_center,
            ((SELECT mid_y_m FROM const) + b.y) AS y_m_norm
          FROM vw_bounce_silver b
        ),

        -- D8. Attach swings to latest serve at/preceding it (carry serving info incl. y_ref)
        swings_in_point AS (
          SELECT
            s.*,
            sp.point_number_d,
            sp.game_number_d,
            sp.point_in_game_d,
            sp.server_id,
            sp.server_uid,
            sp.serving_side_d,
            sp.y_ref AS srv_y_ref
          FROM swings s
          LEFT JOIN LATERAL (
            SELECT sp.* FROM serve_points_ix sp
            WHERE sp.session_id = s.session_id AND sp.ord_ts <= s.ord_ts
            ORDER BY sp.ord_ts DESC LIMIT 1
          ) sp ON TRUE
        ),

        -- D9. Shot number within point + next swing timing
        swings_numbered AS (
          SELECT
            sip.*,
            ROW_NUMBER() OVER (
              PARTITION BY sip.session_id, sip.point_number_d
              ORDER BY sip.ord_ts, sip.swing_id
            ) AS shot_number_d,
            LEAD(sip.ball_hit_ts) OVER (PARTITION BY sip.session_id ORDER BY sip.ord_ts, sip.swing_id) AS next_ball_hit_ts,
            LEAD(sip.ball_hit_s)  OVER (PARTITION BY sip.session_id ORDER BY sip.ord_ts, sip.swing_id) AS next_ball_hit_s
          FROM swings_in_point sip
        ),

        -- D10A. First ANY bounce between this swing and the next swing
        swing_bounce_any AS (
          SELECT
            sn.swing_id, sn.session_id, sn.point_number_d, sn.shot_number_d,
            b.bounce_id AS any_bounce_id, b.bounce_ts AS any_bounce_ts, b.bounce_s AS any_bounce_s,
            b.x_m_center AS any_bounce_x_m,
            b.y_m_center AS any_bounce_y_center_m,
            b.y_m_norm   AS any_bounce_y_norm_m,
            b.bounce_type AS any_bounce_type
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM bounces_norm b
            WHERE b.session_id = sn.session_id
              AND (
                    (b.bounce_ts IS NOT NULL AND sn.ball_hit_ts IS NOT NULL AND b.bounce_ts > sn.ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR sn.ball_hit_ts IS NULL)
                      AND b.bounce_s IS NOT NULL AND sn.ball_hit_s IS NOT NULL
                      AND b.bounce_s > sn.ball_hit_s)
                  )
              AND (
                    sn.next_ball_hit_ts IS NULL
                 OR (b.bounce_ts IS NOT NULL AND sn.next_ball_hit_ts IS NOT NULL AND b.bounce_ts <= sn.next_ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR sn.next_ball_hit_ts IS NULL)
                      AND sn.next_ball_hit_s IS NOT NULL AND b.bounce_s <= sn.next_ball_hit_s)
                  )
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')), b.bounce_id
            LIMIT 1
          ) b ON TRUE
        ),

        -- D10B. First FLOOR bounce between this swing and the next swing
        swing_bounce_floor AS (
          SELECT
            sn.swing_id, sn.session_id, sn.point_number_d, sn.shot_number_d,
            b.bounce_id, b.bounce_ts, b.bounce_s,
            b.x_m_center AS bounce_x_m,
            b.y_m_center AS bounce_y_center_m,
            b.y_m_norm   AS bounce_y_norm_m,
            b.bounce_type AS bounce_type_raw
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM bounces_norm b
            WHERE b.session_id = sn.session_id
              AND b.bounce_type = 'floor'
              AND (
                    (b.bounce_ts IS NOT NULL AND sn.ball_hit_ts IS NOT NULL AND b.bounce_ts > sn.ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR sn.ball_hit_ts IS NULL)
                      AND b.bounce_s IS NOT NULL AND sn.ball_hit_s IS NOT NULL
                      AND b.bounce_s > sn.ball_hit_s)
                  )
              AND (
                    sn.next_ball_hit_ts IS NULL
                 OR (b.bounce_ts IS NOT NULL AND sn.next_ball_hit_ts IS NOT NULL AND b.bounce_ts <= sn.next_ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR sn.next_ball_hit_ts IS NULL)
                      AND sn.next_ball_hit_s IS NOT NULL AND b.bounce_s <= sn.next_ball_hit_s)
                  )
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')), b.bounce_id
            LIMIT 1
          ) b ON TRUE
        ),

        -- D11. Serve bucket from FIRST serve's FLOOR bounce
        first_serve_per_point AS (
          SELECT sp.session_id, sp.point_number_d, MIN(sp.ord_ts) AS first_srv_ts
          FROM serve_points_ix sp
          GROUP BY sp.session_id, sp.point_number_d
        ),
        first_srv_ids AS (
          SELECT sp.session_id, sp.point_number_d, sp.srv_swing_id, sp.serving_side_d
          FROM serve_points_ix sp
          JOIN first_serve_per_point f
            ON f.session_id = sp.session_id
           AND f.point_number_d = sp.point_number_d
           AND f.first_srv_ts   = sp.ord_ts
        ),
        serve_bucket AS (
          SELECT
            f.session_id, f.point_number_d, f.serving_side_d,
            sbf.bounce_x_m,
            sbf.bounce_y_norm_m,
            CASE
              WHEN sbf.bounce_x_m IS NULL OR sbf.bounce_y_norm_m IS NULL THEN NULL
              ELSE
                (CASE WHEN f.serving_side_d = 'ad' THEN 4 ELSE 0 END) +
                (CASE
                   WHEN sbf.bounce_x_m < -((SELECT court_w_m FROM const) / 4.0) THEN 1
                   WHEN sbf.bounce_x_m <  0                                     THEN 2
                   WHEN sbf.bounce_x_m <  ((SELECT court_w_m FROM const) / 4.0) THEN 3
                   ELSE 4
                 END)
            END AS serve_bucket_1_8_d
          FROM first_srv_ids f
          LEFT JOIN swing_bounce_floor sbf
            ON sbf.session_id = f.session_id
           AND sbf.swing_id   = f.srv_swing_id
        ),

        -- D12. Player position at hit (nearest ts_s to ball_hit_s)
        player_at_hit AS (
          SELECT
            sn.session_id, sn.swing_id,
            pp.x AS player_x_at_hit,
            pp.y AS player_y_at_hit
          FROM swings_numbered sn
          LEFT JOIN LATERAL (
            SELECT p.*
            FROM fact_player_position p
            WHERE p.session_id = sn.session_id
              AND p.player_id   = sn.player_id
              AND p.ts_s IS NOT NULL
            ORDER BY ABS(p.ts_s - sn.ball_hit_s)
            LIMIT 1
          ) pp ON TRUE
        ),

        -- D13. Last swing per point + geometry checks for in/out and serve-in
        players_2 AS (
          SELECT session_id,
                 MIN(player_id) AS p1_id,
                 MAX(player_id) AS p2_id
          FROM dim_player
          GROUP BY session_id
        ),
        last_swing_in_point AS (
          SELECT DISTINCT ON (sn.session_id, sn.point_number_d)
            sn.session_id, sn.point_number_d, sn.swing_id, sn.player_id,
            sn.serving_side_d, sn.srv_y_ref, sn.server_id,
            sb.bounce_x_m, sb.bounce_y_norm_m, sb.bounce_type_raw,
            -- is this last shot a serve?
            (EXISTS (
              SELECT 1 FROM serve_flags sf
              WHERE sf.session_id = sn.session_id AND sf.swing_id = sn.swing_id
                AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
            )) AS is_serve_last
          FROM swings_numbered sn
          LEFT JOIN swing_bounce_floor sb
            ON sb.session_id = sn.session_id AND sb.swing_id = sn.swing_id
          ORDER BY sn.session_id, sn.point_number_d, sn.shot_number_d DESC, sn.swing_id DESC
        ),
        last_with_class AS (
          SELECT
            ls.*,
            -- ball in singles/doubles rectangle?
            (ls.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) AND
            (ls.bounce_x_m BETWEEN - (SELECT half_w_m FROM const) AND (SELECT half_w_m FROM const))
              AS last_bounce_in_court,

            -- serve-in geometry (approx): correct half-court in Y and correct service half in X
            CASE
              WHEN NOT ls.is_serve_last THEN NULL
              WHEN ls.bounce_y_norm_m IS NULL OR ls.bounce_x_m IS NULL OR ls.srv_y_ref IS NULL THEN NULL
              ELSE
                (
                  (
                    (ls.srv_y_ref <  (SELECT mid_y_m FROM const) AND
                     ls.bounce_y_norm_m BETWEEN (SELECT mid_y_m FROM const)
                                            AND (SELECT mid_y_m FROM const) + (SELECT svc_box_depth_m FROM const))
                  ) OR (
                    (ls.srv_y_ref >= (SELECT mid_y_m FROM const) AND
                     ls.bounce_y_norm_m BETWEEN (SELECT mid_y_m FROM const) - (SELECT svc_box_depth_m FROM const)
                                            AND (SELECT mid_y_m FROM const))
                  )
                )
                AND
                (
                  (
                    ls.srv_y_ref < (SELECT mid_y_m FROM const) AND
                    (
                      (ls.serving_side_d = 'deuce' AND ls.bounce_x_m <  (SELECT half_w_m FROM const)) OR
                      (ls.serving_side_d = 'ad'    AND ls.bounce_x_m >= (SELECT half_w_m FROM const))
                    )
                  ) OR (
                    ls.srv_y_ref >= (SELECT mid_y_m FROM const) AND
                    (
                      (ls.serving_side_d = 'deuce' AND ls.bounce_x_m >  (SELECT half_w_m FROM const)) OR
                      (ls.serving_side_d = 'ad'    AND ls.bounce_x_m <= (SELECT half_w_m FROM const))
                    )
                  )
                )
            END AS is_serve_in_d
          FROM last_swing_in_point ls
        ),
        point_winner AS (
          SELECT
            lwc.session_id, lwc.point_number_d,
            CASE
              WHEN lwc.is_serve_last AND COALESCE(lwc.is_serve_in_d, FALSE) = FALSE
                THEN CASE WHEN lwc.player_id = p2.p1_id THEN p2.p2_id ELSE p2.p1_id END  -- double fault -> receiver
              WHEN COALESCE(lwc.last_bounce_in_court, FALSE)
                THEN lwc.player_id                                            -- in & unreturned -> hitter wins
              ELSE CASE WHEN lwc.player_id = p2.p1_id THEN p2.p2_id ELSE p2.p1_id END  -- out/net -> opponent
            END AS point_winner_id_d,
            lwc.is_serve_in_d
          FROM last_with_class lwc
          JOIN players_2 p2 USING (session_id)
        ),

        -- D14. Score after each point (server perspective)
        point_scores AS (
          SELECT
            spx.session_id, spx.game_number_d, spx.point_number_d, spx.point_in_game_d,
            spx.server_id,
            pw.point_winner_id_d,
            SUM(CASE WHEN pw.point_winner_id_d = spx.server_id THEN 1 ELSE 0 END)
              OVER (PARTITION BY spx.session_id, spx.game_number_d
                    ORDER BY spx.point_in_game_d
                    ROWS UNBOUNDED PRECEDING) AS server_pts,
            (SUM(1) OVER (PARTITION BY spx.session_id, spx.game_number_d
                          ORDER BY spx.point_in_game_d
                          ROWS UNBOUNDED PRECEDING)
             -
             SUM(CASE WHEN pw.point_winner_id_d = spx.server_id THEN 1 ELSE 0 END)
               OVER (PARTITION BY spx.session_id, spx.game_number_d
                     ORDER BY spx.point_in_game_d
                     ROWS UNBOUNDED PRECEDING)
            ) AS returner_pts
          FROM serve_points_ix spx
          LEFT JOIN point_winner pw
            ON pw.session_id = spx.session_id AND pw.point_number_d = spx.point_number_d
        ),
        point_scores_fmt AS (
          SELECT
            ps.*,
            CASE
              WHEN GREATEST(server_pts, returner_pts) >= 4 AND ABS(server_pts - returner_pts) >= 2
                THEN CASE WHEN server_pts > returner_pts THEN 'game-server' ELSE 'game-returner' END
              WHEN server_pts >= 4 OR returner_pts >= 4 THEN
                CASE
                  WHEN server_pts = returner_pts THEN '40-40'
                  WHEN server_pts  > returner_pts THEN 'AD-40'
                  ELSE '40-AD'
                END
              ELSE
                (CASE server_pts
                   WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END)
                || '-' ||
                (CASE returner_pts
                   WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END)
            END AS score_after_point_str
          FROM point_scores ps
        )

        -- FINAL SELECT
        SELECT
          sn.session_id,
          sn.session_uid_d,
          sn.swing_id,
          sn.player_id,
          hu.player_uid,
          sn.rally_id,

          sn.start_s, sn.end_s, sn.ball_hit_s,
          sn.start_ts, sn.end_ts, sn.ball_hit_ts,
          sn.ball_hit_x, sn.ball_hit_y,
          sn.ball_speed,
          sn.swing_type AS swing_type_raw,

          -- FIRST FLOOR bounce (used for scoring)
          sb.bounce_id,
          sb.bounce_type_raw,
          sb.bounce_s                 AS bounce_s_d,
          sb.bounce_x_m               AS bounce_x_m,
          sb.bounce_y_center_m        AS bounce_y_center_m,
          sb.bounce_y_norm_m          AS bounce_y_norm_m,

          -- legacy aliases for back-compat
          sb.bounce_x_m               AS bounce_x,
          sb.bounce_y_norm_m          AS bounce_y,

          -- FIRST ANY bounce (diagnostic/visibility)
          sba.any_bounce_id,
          sba.any_bounce_type,
          sba.any_bounce_s,
          sba.any_bounce_x_m,
          sba.any_bounce_y_center_m,
          sba.any_bounce_y_norm_m,

          COALESCE(pah.player_x_at_hit, sn.ball_hit_x) AS player_x_at_hit,
          COALESCE(pah.player_y_at_hit, sn.ball_hit_y) AS player_y_at_hit,

          sn.shot_number_d,
          sn.point_number_d,
          sn.game_number_d,
          sn.point_in_game_d,

          -- serve flag per your rule
          (EXISTS (
            SELECT 1 FROM serve_flags sf
            WHERE sf.session_id = sn.session_id
              AND sf.swing_id   = sn.swing_id
              AND sf.is_fh_overhead
              AND COALESCE(sf.inside_serve_band, FALSE)
          )) AS serve_d,

          sn.serving_side_d,

          -- serve counts
          SUM(CASE WHEN (EXISTS (
                SELECT 1 FROM serve_flags sf
                WHERE sf.session_id = sn.session_id
                  AND sf.swing_id   = sn.swing_id
                  AND sf.is_fh_overhead
                  AND COALESCE(sf.inside_serve_band, FALSE)
          )) THEN 1 ELSE 0 END)
            OVER (PARTITION BY sn.session_id, sn.point_number_d) AS serve_count_in_point_d,
          GREATEST(
            SUM(CASE WHEN (EXISTS (
                  SELECT 1 FROM serve_flags sf
                  WHERE sf.session_id = sn.session_id
                    AND sf.swing_id   = sn.swing_id
                    AND sf.is_fh_overhead
                    AND COALESCE(sf.inside_serve_band, FALSE)
            )) THEN 1 ELSE 0 END)
              OVER (PARTITION BY sn.session_id, sn.point_number_d) - 1,
            0
          ) AS fault_serves_in_point_d,

          -- depth / rally location (NON-SERVE only, from FLOOR bounce)
          CASE
            WHEN (EXISTS (
                SELECT 1 FROM serve_flags sf
                WHERE sf.session_id = sn.session_id AND sf.swing_id = sn.swing_id
                  AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
            )) THEN NULL
            WHEN sb.bounce_y_norm_m IS NULL THEN NULL
            ELSE CASE
              WHEN LEAST(sb.bounce_y_norm_m, (SELECT court_l_m FROM const) - sb.bounce_y_norm_m) < (SELECT short_m FROM const) THEN 'short'
              WHEN LEAST(sb.bounce_y_norm_m, (SELECT court_l_m FROM const) - sb.bounce_y_norm_m) < (SELECT mid_m   FROM const) THEN 'mid'
              ELSE 'long'
            END
          END AS shot_depth_d,

          CASE
            WHEN (EXISTS (
                SELECT 1 FROM serve_flags sf
                WHERE sf.session_id = sn.session_id AND sf.swing_id = sn.swing_id
                  AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
            )) THEN NULL
            WHEN sb.bounce_x_m IS NULL THEN NULL
            WHEN sb.bounce_x_m < -((SELECT court_w_m FROM const) / 4.0) THEN 'A'
            WHEN sb.bounce_x_m <  0                                       THEN 'B'
            WHEN sb.bounce_x_m <  ((SELECT court_w_m FROM const) / 4.0)   THEN 'C'
            ELSE 'D'
          END AS rally_location_d,

          CASE
            WHEN (EXISTS (
                SELECT 1 FROM serve_flags sf
                WHERE sf.session_id = sn.session_id AND sf.swing_id = sn.swing_id
                  AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
            ))
            THEN sv.serve_bucket_1_8_d
            ELSE NULL
          END AS serve_bucket_1_8_d,

          -- ball speed error flag
          (COALESCE(sn.ball_speed, 0) = 0) AS is_error_d,

          -- NEW: winner + score
          pw.point_winner_id_d,
          psf.score_after_point_str       AS score_str_d,

          -- helpful: whether a serve was judged in
          pw.is_serve_in_d

        FROM swings_numbered sn
        LEFT JOIN hitter_uid hu
          ON hu.session_id = sn.session_id AND hu.swing_id = sn.swing_id
        LEFT JOIN swing_bounce_floor sb
          ON sb.session_id = sn.session_id AND sb.swing_id = sn.swing_id
        LEFT JOIN swing_bounce_any sba
          ON sba.session_id = sn.session_id AND sba.swing_id = sn.swing_id
        LEFT JOIN serve_bucket sv
          ON sv.session_id = sn.session_id AND sv.point_number_d = sn.point_number_d
        LEFT JOIN player_at_hit pah
          ON pah.session_id = sn.session_id AND pah.swing_id = sn.swing_id
        LEFT JOIN point_winner pw
          ON pw.session_id = sn.session_id AND pw.point_number_d = sn.point_number_d
        LEFT JOIN point_scores_fmt psf
          ON psf.session_id = sn.session_id AND psf.point_number_d = sn.point_number_d
        ORDER BY sn.session_id, sn.point_number_d, sn.shot_number_d, sn.swing_id;
    """,

    # ------------------------------- point gold --------------------------------
    "vw_point_gold": """
        CREATE OR REPLACE VIEW vw_point_gold AS
        SELECT * FROM vw_point_silver;
    """,

    # ------------------------------ DEBUG: streams ------------------------------
    "vw_bounce_stream_debug": """
        CREATE OR REPLACE VIEW vw_bounce_stream_debug AS
        WITH const AS (
          SELECT 23.77::numeric AS court_l_m, 23.77::numeric/2 AS mid_y_m, 10.97::numeric AS court_w_m
        )
        SELECT
          b.session_id,
          ds.session_uid,
          b.bounce_id,
          b.bounce_type,
          b.bounce_ts,
          b.bounce_s,
          b.x AS x_center,                -- as sent (meters)
          b.y AS y_center,                -- as sent (meters)
          (b.x) AS x_m_center,            -- explicit meter alias
          (b.y) AS y_m_center,            -- explicit meter alias
          ((SELECT mid_y_m FROM const) + b.y) AS y_m_norm,   -- 0..23.77 normalization
          ((SELECT mid_y_m FROM const) + b.y) AS y_norm,     -- back-compat alias
          CASE WHEN b.bounce_type='floor' THEN 1 ELSE 0 END AS is_floor,
          CASE WHEN b.x IS NULL OR b.y IS NULL THEN 1 ELSE 0 END AS is_xy_null
        FROM fact_bounce b
        LEFT JOIN dim_session ds USING (session_id)
        ORDER BY b.session_id, COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')), b.bounce_id;
    """,

    # --------------------- DEBUG: all bounces per swing window ------------------
    "vw_point_bounces_debug": """
        CREATE OR REPLACE VIEW vw_point_bounces_debug AS
        WITH
        const AS (
          SELECT 23.77::numeric AS court_l_m, 23.77::numeric/2 AS mid_y_m
        ),
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
        -- numbering points off serves:
        serve_flags AS (
          SELECT
            s.session_id, s.swing_id, s.player_id, s.ord_ts,
            s.ball_hit_x AS x_ref, s.ball_hit_y AS y_ref,
            (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
            CASE
              WHEN s.ball_hit_y IS NULL THEN NULL
              ELSE (s.ball_hit_y <= 0.50 OR s.ball_hit_y >= 23.77 - 0.50)
            END AS inside_serve_band
          FROM swings s
        ),
        serve_events AS (
          SELECT sf.session_id, sf.swing_id AS srv_swing_id, sf.player_id AS server_id, sf.ord_ts
          FROM serve_flags sf
          WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
        ),
        serves_numbered AS (
          SELECT
            se.*,
            LAG(se.srv_swing_id) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_srv
          FROM serve_events se
        ),
        serve_points AS (
          SELECT
            sn.*,
            SUM(CASE WHEN sn.prev_srv IS NULL OR sn.srv_swing_id <> sn.prev_srv THEN 1 ELSE 0 END)
              OVER (PARTITION BY sn.session_id ORDER BY sn.ord_ts, sn.srv_swing_id) AS point_number_d
          FROM serves_numbered sn
        ),
        swings_in_point AS (
          SELECT s.*, sp.point_number_d
          FROM swings s
          LEFT JOIN LATERAL (
            SELECT sp.* FROM serve_points sp
            WHERE sp.session_id = s.session_id AND sp.ord_ts <= s.ord_ts
            ORDER BY sp.ord_ts DESC LIMIT 1
          ) sp ON TRUE
        ),
        swings_numbered AS (
          SELECT
            sip.*,
            ROW_NUMBER() OVER (PARTITION BY sip.session_id, sip.point_number_d ORDER BY sip.ord_ts, sip.swing_id) AS shot_number_d,
            LEAD(sip.ball_hit_ts) OVER (PARTITION BY sip.session_id ORDER BY sip.ord_ts, sip.swing_id) AS next_ball_hit_ts,
            LEAD(sip.ball_hit_s)  OVER (PARTITION BY sip.session_id ORDER BY sip.ord_ts, sip.swing_id) AS next_ball_hit_s
          FROM swings_in_point sip
        ),
        bounces_in_window AS (
          SELECT
            sn.session_id, sn.point_number_d, sn.swing_id, sn.shot_number_d,
            b.bounce_id, b.bounce_type, b.bounce_ts, b.bounce_s,
            b.x             AS bounce_x_center_m,
            b.y             AS bounce_y_center_m,
            ((SELECT mid_y_m FROM const) + b.y) AS bounce_y_norm_m,
            ROW_NUMBER() OVER (
              PARTITION BY sn.session_id, sn.swing_id
              ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')), b.bounce_id
            ) AS bounce_rank_in_shot
          FROM swings_numbered sn
          JOIN LATERAL (
            SELECT b.*
            FROM vw_bounce_silver b
            WHERE b.session_id = sn.session_id
              AND (
                    (b.bounce_ts IS NOT NULL AND sn.ball_hit_ts IS NOT NULL AND b.bounce_ts > sn.ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR sn.ball_hit_ts IS NULL)
                      AND b.bounce_s IS NOT NULL AND sn.ball_hit_s IS NOT NULL
                      AND b.bounce_s > sn.ball_hit_s)
                  )
              AND (
                    sn.next_ball_hit_ts IS NULL
                 OR COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')) <= sn.next_ball_hit_ts
                  )
          ) b ON TRUE
        )
        SELECT * FROM bounces_in_window
        ORDER BY session_id, point_number_d, shot_number_d, bounce_rank_in_shot, bounce_id;
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
