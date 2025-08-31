# db_views.py — Silver passthrough (verbatim bronze) + minimal, explicit derived fields
from sqlalchemy import text
from typing import List

VIEW_SQL_STMTS: List[str] = []  # populated from VIEW_NAMES/CREATE_STMTS

# -------- Bronze helper: make raw_ingest table if missing (safe to re-run) --------
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

# -------- Views to build (order matters) --------
VIEW_NAMES = [
    # SILVER (pure)
    "vw_swing_silver",
    "vw_bounce_silver",
    "vw_ball_position_silver",
    "vw_player_position_silver",

    # ORDERING + DERIVED (still exposed as the single Silver output you want)
    "vw_point_silver",
]

# Legacy objects we want to drop if they still exist
LEGACY_OBJECTS = [
    "vw_swing", "vw_bounce", "vw_ball_position", "vw_player_position",
    "vw_point_order_by_serve_old", "vw_point_log_gold",
    "vw_point_shot_log_gold", "vw_shot_order_gold",
    "vw_point_summary", "point_log_tbl", "point_summary_tbl",
    "vw_point_shot_log",
]

def _drop_any(conn, name: str):
    """
    Drop any object named `name` in public schema, picking the right DROP
    for its actual type to avoid WrongObjectType errors.
    """
    kind = conn.execute(text("""
        SELECT CASE
                 WHEN EXISTS (
                    SELECT 1 FROM information_schema.views
                    WHERE table_schema='public' AND table_name=:n
                 ) THEN 'view'
                 WHEN EXISTS (
                    SELECT 1 FROM pg_matviews
                    WHERE schemaname='public' AND matviewname=:n
                 ) THEN 'mview'
                 WHEN EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema='public' AND table_name=:n
                 ) THEN 'table'
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
        # (fix: quotes now matched)
        stmts = [
            f'DROP VIEW IF EXISTS "{name}" CASCADE;',
            f'DROP MATERIALIZED VIEW IF EXISTS "{name}" CASCADE;',
            f'DROP TABLE IF EXISTS "{name}" CASCADE;',
        ]
    for stmt in stmts:
        conn.execute(text(stmt))

# ======================================================================
# ==========  REPLACEABLE SECTION A:  PURE SILVER PASSTHROUGHS  =========
# ======================================================================

CREATE_STMTS = {
    "vw_swing_silver": """
        CREATE OR REPLACE VIEW vw_swing_silver AS
        SELECT
          fs.session_id,
          fs.swing_id,
          fs.player_id,
          fs.rally_id,
          fs.start_s, fs.end_s, fs.ball_hit_s,
          fs.start_ts, fs.end_ts, fs.ball_hit_ts,
          fs.ball_hit_x, fs.ball_hit_y,
          fs.ball_speed,
          fs.ball_player_distance,
          fs.is_in_rally,
          fs.serve,                -- keep bronze flag
          fs.serve_type,
          fs.swing_type,
          fs.meta,
          ds.session_uid                            AS session_uid,        -- passthrough label
          dp.sportai_player_uid                     AS player_uid          -- passthrough label
        FROM fact_swing fs
        LEFT JOIN dim_session ds ON ds.session_id = fs.session_id
        LEFT JOIN dim_player  dp ON dp.session_id = fs.session_id AND dp.player_id = fs.player_id;
    """,

    "vw_ball_position_silver": """
        CREATE OR REPLACE VIEW vw_ball_position_silver AS
        SELECT
          fbp.session_id,
          fbp.ts_s,
          fbp.ts,
          fbp.x, fbp.y
        FROM fact_ball_position fbp;
    """,

    "vw_player_position_silver": """
        CREATE OR REPLACE VIEW vw_player_position_silver AS
        SELECT
          fpp.session_id,
          fpp.player_id,
          fpp.ts_s,
          fpp.ts,
          fpp.x,
          fpp.y
        FROM fact_player_position fpp;
    """,

    "vw_bounce_silver": """
        CREATE OR REPLACE VIEW vw_bounce_silver AS
        WITH src AS (
          SELECT
            b.session_id,
            b.bounce_id,
            b.hitter_player_id,
            b.rally_id,
            b.bounce_s,
            b.bounce_ts,
            b.x AS x_raw,
            b.y AS y_raw,
            b.bounce_type
          FROM fact_bounce b
        ),
        xy_at_bounce AS (
          SELECT
            s.bounce_id,
            bp.x AS x_bp,
            bp.y AS y_bp
          FROM src s
          LEFT JOIN LATERAL (
            SELECT fbp.x, fbp.y
            FROM fact_ball_position fbp
            WHERE fbp.session_id = s.session_id
              AND (
                    (fbp.ts   IS NOT NULL AND s.bounce_ts IS NOT NULL AND fbp.ts   = s.bounce_ts)
                 OR (fbp.ts_s IS NOT NULL AND s.bounce_s  IS NOT NULL AND fbp.ts_s = s.bounce_s)
                  )
            ORDER BY 1
            LIMIT 1
          ) bp ON TRUE
        )
        SELECT
          s.session_id,
          s.bounce_id,
          s.hitter_player_id,
          s.rally_id,
          s.bounce_s,
          s.bounce_ts,
          COALESCE(s.x_raw, xab.x_bp) AS x,
          COALESCE(s.y_raw, xab.y_bp) AS y,
          s.bounce_type
        FROM src s
        LEFT JOIN xy_at_bounce xab ON xab.bounce_id = s.bounce_id;
    """,
}

# ======================================================================
# =====  REPLACEABLE SECTION B:  ORDERING + DERIVED (SILVER OUTPUT)  ===
# ======================================================================

CREATE_STMTS.update({
    "vw_point_silver": """
        CREATE OR REPLACE VIEW vw_point_silver AS
        /* ---------- 1) Base swings + player position at hit ---------- */
        WITH s AS (
          SELECT
            v.*,
            COALESCE(v.ball_hit_ts, v.start_ts, (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')) AS ord_ts
          FROM vw_swing_silver v
        ),
        s_pos AS (
          SELECT
            s.*,
            pp.x AS player_x_at_hit,
            pp.y AS player_y_at_hit
          FROM s
          LEFT JOIN LATERAL (
            SELECT pps.x, pps.y
            FROM vw_player_position_silver pps
            WHERE pps.session_id = s.session_id
              AND pps.player_id  = s.player_id
              AND (
                    (pps.ts   IS NOT NULL AND s.ball_hit_ts IS NOT NULL)
                 OR (pps.ts_s IS NOT NULL AND s.ball_hit_s  IS NOT NULL)
              )
            ORDER BY CASE
                       WHEN pps.ts IS NOT NULL AND s.ball_hit_ts IS NOT NULL
                         THEN ABS(EXTRACT(EPOCH FROM (pps.ts - s.ball_hit_ts)))
                       ELSE ABS(COALESCE(pps.ts_s, 0) - COALESCE(s.ball_hit_s, 0))
                     END
            LIMIT 1
          ) pp ON TRUE
        ),

        /* ---------- 2) Robust court center & service/back thresholds ---------- */
        session_center AS (
          SELECT
            p.session_id,
            PERCENTILE_CONT(0.02) WITHIN GROUP (ORDER BY p.x) AS x_lo,
            PERCENTILE_CONT(0.98) WITHIN GROUP (ORDER BY p.x) AS x_hi,
            (PERCENTILE_CONT(0.02) WITHIN GROUP (ORDER BY p.x)
             + PERCENTILE_CONT(0.98) WITHIN GROUP (ORDER BY p.x)) / 2.0 AS center_x,
            PERCENTILE_CONT(0.02) WITHIN GROUP (ORDER BY p.y) AS y_lo,
            PERCENTILE_CONT(0.98) WITHIN GROUP (ORDER BY p.y) AS y_hi
          FROM vw_player_position_silver p
          GROUP BY p.session_id
        ),
        s_flags AS (
          SELECT
            sp.*,
            sc.center_x,
            sc.y_lo, sc.y_hi,
            /* "Behind service line": far-back zone ≈ 80% of half-court */
            CASE
              WHEN sc.y_hi IS NULL OR sc.y_lo IS NULL OR sp.player_y_at_hit IS NULL THEN NULL
              ELSE CASE
                WHEN ABS(sp.player_y_at_hit - (sc.y_lo + sc.y_hi)/2.0) >= 0.80 * ((sc.y_hi - sc.y_lo)/2.0)
                THEN TRUE ELSE FALSE END
            END AS behind_service_zone
          FROM s_pos sp
          LEFT JOIN session_center sc ON sc.session_id = sp.session_id
        ),

        /* ---------- 3) Point boundaries (first fh_overhead/serve from back) ---------- */
        base AS (
          SELECT
            f.*,
            CASE
              WHEN COALESCE(f.serve, FALSE) THEN 1
              WHEN f.swing_type ILIKE '%overhead%' AND COALESCE(f.behind_service_zone, FALSE) THEN 1
              ELSE 0
            END AS is_serve_begin
          FROM s_flags f
        ),
        seq AS (
          SELECT
            b.*,
            CASE
              WHEN SUM(is_serve_begin) OVER (PARTITION BY b.session_id) = 0 THEN 1
              ELSE 1 + SUM(is_serve_begin) OVER (PARTITION BY b.session_id ORDER BY b.ord_ts, b.swing_id ROWS UNBOUNDED PRECEDING)
            END AS point_number_d
          FROM base b
        ),
        shots AS (
          SELECT
            seq.*,
            ROW_NUMBER() OVER (PARTITION BY seq.session_id, seq.point_number_d ORDER BY seq.ord_ts, seq.swing_id) AS shot_number_d,
            MIN(seq.ord_ts) OVER (PARTITION BY seq.session_id, seq.point_number_d) AS point_ts0
          FROM seq
        ),

        /* ---------- 4) Server at first swing in point (and side) ---------- */
        pt_first AS (
          SELECT DISTINCT ON (session_id, point_number_d)
            session_id, point_number_d,
            swing_id      AS first_swing_id,
            player_id     AS server_player_id,
            player_x_at_hit AS server_pos_x_first,
            player_y_at_hit AS server_pos_y_first,
            center_x
          FROM shots
          ORDER BY session_id, point_number_d, shot_number_d
        ),
        serving_side AS (
          SELECT
            pf.*,
            CASE
              WHEN pf.server_pos_x_first IS NULL OR pf.center_x IS NULL THEN NULL
              WHEN (pf.server_pos_x_first - pf.center_x) >= 0 THEN 'deuce' ELSE 'ad'
            END AS serving_side_d
          FROM pt_first pf
        ),

        /* ---------- 5) Bounce immediately after each swing (for serve bucket + rally quad) ---------- */
        s_bounce AS (
          SELECT
            s.swing_id, s.session_id,
            b.bounce_id,
            b.bounce_ts, b.bounce_s,
            b.x AS ball_bounce_x,
            b.y AS ball_bounce_y,
            b.bounce_type
          FROM shots s
          LEFT JOIN LATERAL (
            SELECT b.*
            FROM vw_bounce_silver b
            WHERE b.session_id = s.session_id
              AND (
                    (b.bounce_ts IS NOT NULL AND s.ball_hit_ts IS NOT NULL AND b.bounce_ts >= s.ball_hit_ts)
                 OR ((b.bounce_ts IS NULL OR s.ball_hit_ts IS NULL)
                     AND b.bounce_s IS NOT NULL AND s.ball_hit_s IS NOT NULL
                     AND b.bounce_s >= s.ball_hit_s)
                  )
            ORDER BY COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second'))
            LIMIT 1
          ) b ON TRUE
        ),

        pt_last AS (
          SELECT DISTINCT ON (session_id, point_number_d)
            session_id, point_number_d,
            swing_id AS last_swing_id,
            player_id AS last_hitter_id
          FROM shots
          ORDER BY session_id, point_number_d, shot_number_d DESC
        ),

        /* ---------- 6) Serve bucket (1–8) from first-serve bounce ---------- */
        serve_bounce AS (
          SELECT
            pf.session_id, pf.point_number_d,
            sb.ball_bounce_x, sb.ball_bounce_y
          FROM pt_first pf
          LEFT JOIN s_bounce sb ON sb.session_id = pf.session_id AND sb.swing_id = pf.first_swing_id
        ),
        serve_bucket AS (
          SELECT
            sb.*,
            CASE
              WHEN sb.ball_bounce_x IS NULL OR sb.ball_bounce_y IS NULL THEN NULL
              ELSE (CASE WHEN sb.ball_bounce_y >= 0 THEN 1 ELSE 0 END) * 4
                   + (CASE
                        WHEN sb.ball_bounce_x < -0.5 THEN 1
                        WHEN sb.ball_bounce_x <  0.0 THEN 2
                        WHEN sb.ball_bounce_x <  0.5 THEN 3
                        ELSE 4
                      END)
            END AS serve_bucket_1_8_d
          FROM serve_bounce sb
        ),

        /* ---------- 7) Point winner (simple, documented heuristic) ---------- */
        last_bounce AS (
          SELECT
            pl.session_id, pl.point_number_d,
            sb.bounce_type,
            sb.ball_bounce_x, sb.ball_bounce_y
          FROM pt_last pl
          LEFT JOIN s_bounce sb ON sb.session_id = pl.session_id AND sb.swing_id = pl.last_swing_id
        ),
        point_winner AS (
          SELECT
            lb.session_id, lb.point_number_d,
            CASE
              -- if last shot is out/net/wide/long -> last hitter loses -> opponent wins
              WHEN lb.bounce_type ILIKE '%net%' OR lb.bounce_type ILIKE '%out%' OR lb.bounce_type ILIKE '%wide%' OR lb.bounce_type ILIKE '%long%'
                THEN (SELECT dp.sportai_player_uid
                      FROM vw_swing_silver sw
                      JOIN dim_player dp ON dp.session_id = sw.session_id AND dp.player_id = sw.player_id
                      WHERE sw.session_id = pl.session_id AND sw.swing_id = pl.last_swing_id)   -- last hitter
                     -- flip to opponent:
                     || '/*opponent*/'
              ELSE
                -- treat in-court / floor as winner = last hitter
                (SELECT dp.sportai_player_uid
                 FROM vw_swing_silver sw
                 JOIN dim_player dp ON dp.session_id = sw.session_id AND dp.player_id = sw.player_id
                 WHERE sw.session_id = pl.session_id AND sw.swing_id = pl.last_swing_id)
            END AS point_winner_uid_d
          FROM pt_last pl
          LEFT JOIN last_bounce lb ON lb.session_id = pl.session_id AND lb.point_number_d = pl.point_number_d
        ),

        /* ---------- 8) Score per game (server first) ---------- */
        names AS (
          SELECT
            pf.session_id, pf.point_number_d,
            pf.server_player_id,
            dp1.sportai_player_uid AS server_uid,
            (SELECT MIN(dp2.sportai_player_uid) FROM dim_player dp2 WHERE dp2.session_id=pf.session_id AND dp2.player_id<>pf.server_player_id) AS receiver_uid
          FROM pt_first pf
          LEFT JOIN dim_player dp1 ON dp1.session_id = pf.session_id AND dp1.player_id=pf.server_player_id
        ),
        game_change AS (
          SELECT
            n.*,
            LAG(n.server_uid) OVER (PARTITION BY n.session_id ORDER BY n.point_number_d) AS prev_server_uid,
            CASE WHEN LAG(n.server_uid) OVER (PARTITION BY n.session_id ORDER BY n.point_number_d)
                      IS DISTINCT FROM n.server_uid THEN 1 ELSE 0 END AS new_game_flag
          FROM names n
        ),
        game_seq AS (
          SELECT
            g.*,
            1 + SUM(new_game_flag) OVER (PARTITION BY g.session_id ORDER BY g.point_number_d ROWS UNBOUNDED PRECEDING) AS game_number_d
          FROM game_change g
        ),
        winners AS (
          SELECT
            gs.session_id, gs.point_number_d, gs.server_uid, gs.game_number_d,
            COALESCE(pw.point_winner_uid_d, NULL) AS win_uid
          FROM game_seq gs
          LEFT JOIN point_winner pw ON pw.session_id=gs.session_id AND pw.point_number_d=gs.point_number_d
        ),
        tally AS (
          SELECT
            w.*,
            SUM(CASE WHEN w.win_uid = w.server_uid THEN 1 ELSE 0 END)
              OVER (PARTITION BY w.session_id, w.game_number_d ORDER BY w.point_number_d ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS server_pts_before,
            SUM(CASE WHEN w.win_uid <> w.server_uid THEN 1 ELSE 0 END)
              OVER (PARTITION BY w.session_id, w.game_number_d ORDER BY w.point_number_d ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS recv_pts_before
          FROM winners w
        ),
        score_text AS (
          SELECT
            t.*,
            /* helper to tennis strings */
            CASE
              WHEN COALESCE(server_pts_before,0) >= 4 OR COALESCE(recv_pts_before,0) >= 4 THEN
                CASE
                  WHEN server_pts_before = recv_pts_before THEN '40-40'
                  WHEN server_pts_before = recv_pts_before + 1 THEN 'Ad-40'
                  WHEN recv_pts_before   = server_pts_before + 1 THEN '40-Ad'
                  ELSE '40-40'
                END
              ELSE
                CONCAT(
                  CASE COALESCE(server_pts_before,0)
                    WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END,
                  '-', 
                  CASE COALESCE(recv_pts_before,0)
                    WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END
                )
            END AS score_d
          FROM tally t
        ),

        /* ---------- 9) Assemble final Silver rowset (verbatim bronze + derived only) ---------- */
        final_rows AS (
          SELECT
            sh.session_id,
            sw.session_uid,
            sw.player_uid,

            -- bronze passthrough positions/timing/swing (UNEDITED)
            sb.ball_bounce_x, sb.ball_bounce_y,
            sh.ball_hit_x,   sh.ball_hit_y,
            sh.player_x_at_hit, sh.player_y_at_hit,
            sh.ball_speed,
            sh.swing_type,
            COALESCE(sh.ball_hit_s, sh.start_s, 0)                 AS t_s,
            TO_CHAR( (TIMESTAMP 'epoch' + COALESCE(sh.ball_hit_s, sh.start_s, 0) * INTERVAL '1 second'), 'HH24:MI:SS') AS t_hms,
            (COALESCE(sh.end_s,0) - COALESCE(sh.start_s,0))        AS duration_s,

            -- derived minimal set
            CASE WHEN COALESCE(sh.ball_speed, 0) = 0 THEN TRUE ELSE FALSE END AS is_error_d,
            /* serve_d: first swing in point and either explicit serve or fh_overhead from back */
            CASE WHEN sh.shot_number_d = 1
                      AND (COALESCE(sh.serve, FALSE) OR sh.swing_type ILIKE '%overhead%')
                      AND ss.serving_side_d IS NOT NULL
                 THEN TRUE ELSE FALSE END                           AS serve_d,

            sv.serve_bucket_1_8_d,

            /* rally location quadrant A-D from last bounce of point:
               A = deuce/front, B = ad/front, C = ad/back, D = deuce/back */
            CASE
              WHEN lb.ball_bounce_x IS NULL OR lb.ball_bounce_y IS NULL OR sc.center_x IS NULL THEN NULL
              ELSE CASE
                WHEN (lb.ball_bounce_y >= 0) AND (lb.ball_bounce_x >= sc.center_x) THEN 'A'
                WHEN (lb.ball_bounce_y >= 0) AND (lb.ball_bounce_x <  sc.center_x) THEN 'B'
                WHEN (lb.ball_bounce_y  < 0) AND (lb.ball_bounce_x <  sc.center_x) THEN 'C'
                ELSE 'D' END
            END                                                     AS rally_location_quadrant_d,

            /* shot depth (short/mid/long) by fractional Y distance */
            CASE
              WHEN sc.y_hi IS NULL OR sc.y_lo IS NULL OR sb.ball_bounce_y IS NULL THEN NULL
              ELSE CASE
                WHEN ABS(sb.ball_bounce_y - (sc.y_lo + sc.y_hi)/2.0) <= 0.30 * ((sc.y_hi - sc.y_lo)/2.0) THEN 'short'
                WHEN ABS(sb.ball_bounce_y - (sc.y_lo + sc.y_hi)/2.0) <= 0.70 * ((sc.y_hi - sc.y_lo)/2.0) THEN 'mid'
                ELSE 'long' END
            END                                                     AS shot_depth_d,

            gs.game_number_d,
            sh.point_number_d,
            sh.shot_number_d                                       AS shot_number_d,

            pw.point_winner_uid_d,
            st.score_d,

            NULL::text                                              AS error_type_d -- placeholder, per your note

          FROM shots sh
          LEFT JOIN vw_swing_silver  sw ON sw.session_id = sh.session_id AND sw.swing_id = sh.swing_id
          LEFT JOIN s_bounce         sb ON sb.session_id = sh.session_id AND sb.swing_id = sh.swing_id
          LEFT JOIN serving_side     ss ON ss.session_id = sh.session_id AND ss.point_number_d = sh.point_number_d
          LEFT JOIN serve_bucket     sv ON sv.session_id = sh.session_id AND sv.point_number_d = sh.point_number_d
          LEFT JOIN last_bounce      lb ON lb.session_id = sh.session_id AND lb.point_number_d = sh.point_number_d
          LEFT JOIN session_center   sc ON sc.session_id = sh.session_id
          LEFT JOIN score_text       st ON st.session_id = sh.session_id AND st.point_number_d = sh.point_number_d
          LEFT JOIN game_seq         gs ON gs.session_id = sh.session_id AND gs.point_number_d = sh.point_number_d
          LEFT JOIN point_winner     pw ON pw.session_id = sh.session_id AND pw.point_number_d = sh.point_number_d
        )

        SELECT * FROM final_rows
        ORDER BY session_id, point_number_d, shot_number_d, t_s;
    """
})

# ---------- helpers ----------
def _table_exists(conn, t: str) -> bool:
    return conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name=:t
        LIMIT 1
    """), {"t": t}).first() is not None

def _column_exists(conn, t: str, c: str) -> bool:
    return conn.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t AND column_name=:c
        LIMIT 1
    """), {"t": t, "c": c}).first() is not None

def _preflight_or_raise(conn):
    required_tables = [
        "dim_session", "dim_player",
        "dim_rally",
        "fact_swing", "fact_bounce",
        "fact_player_position", "fact_ball_position",
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

    checks = [
        ("dim_session", "session_uid"),
        ("dim_player", "sportai_player_uid"),
        ("dim_rally", "rally_id"),
        ("dim_rally", "rally_number"),

        ("fact_swing", "swing_id"),
        ("fact_swing", "session_id"),
        ("fact_swing", "player_id"),
        ("fact_swing", "rally_id"),
        ("fact_swing", "start_s"),
        ("fact_swing", "end_s"),
        ("fact_swing", "ball_hit_s"),
        ("fact_swing", "start_ts"),
        ("fact_swing", "end_ts"),
        ("fact_swing", "ball_hit_ts"),
        ("fact_swing", "ball_hit_x"),
        ("fact_swing", "ball_hit_y"),
        ("fact_swing", "ball_speed"),
        ("fact_swing", "is_in_rally"),
        ("fact_swing", "serve"),
        ("fact_swing", "swing_type"),

        ("fact_bounce", "bounce_id"),
        ("fact_bounce", "session_id"),
        ("fact_bounce", "hitter_player_id"),
        ("fact_bounce", "rally_id"),
        ("fact_bounce", "bounce_s"),
        ("fact_bounce", "bounce_ts"),
        ("fact_bounce", "x"),
        ("fact_bounce", "y"),
        ("fact_bounce", "bounce_type"),

        ("fact_player_position", "session_id"),
        ("fact_player_position", "player_id"),
        ("fact_player_position", "ts_s"),
        ("fact_player_position", "ts"),
        ("fact_player_position", "x"),
        ("fact_player_position", "y"),

        ("fact_ball_position", "session_id"),
        ("fact_ball_position", "ts_s"),
        ("fact_ball_position", "ts"),
        ("fact_ball_position", "x"),
        ("fact_ball_position", "y"),
    ]
    missing_cols = [(t,c) for (t,c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t,c) in missing_cols])
        raise RuntimeError(f"Missing required columns before creating views: {msg}")

# ---------- apply all views ----------
def _apply_views(engine):
    """Drops legacy objects, then (re)creates all views listed in VIEW_NAMES."""
    global VIEW_SQL_STMTS
    VIEW_SQL_STMTS = [CREATE_STMTS[name] for name in VIEW_NAMES]

    with engine.begin() as conn:
        _ensure_raw_ingest(conn)
        _preflight_or_raise(conn)

        # 1) Proactively drop legacy blockers
        for obj in LEGACY_OBJECTS:
            _drop_any(conn, obj)

        # 2) DROP in reverse dependency order then CREATE in forward order
        for name in reversed(VIEW_NAMES):
            _drop_any(conn, name)
        for name in VIEW_NAMES:
            conn.execute(text(CREATE_STMTS[name]))

# Back-compat exports
init_views = _apply_views
run_views  = _apply_views

__all__ = ["init_views", "run_views", "VIEW_SQL_STMTS", "VIEW_NAMES", "CREATE_STMTS"]
