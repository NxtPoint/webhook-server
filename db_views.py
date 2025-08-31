# db_views.py — Silver passthrough + ordering + enriched point rows (derived fields block)
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
# SILVER = strict pass-through from BRONZE (no edits; only *_d derived labels)
# GOLD   = reads only SILVER; all derivations are *_d and kept together
VIEW_NAMES = [
    # SILVER (pure)
    "vw_swing_silver",
    "vw_bounce_silver",
    "vw_ball_position_silver",
    "vw_player_position_silver",

    # Ordering and Gold
    "vw_point_order_by_serve",
    "vw_point_log",
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
        stmts = [
            f'DROP VIEW IF EXISTS "{name}" CASCADE;',
            f'DROP MATERIALIZED VIEW IF EXISTS "{name}" CASCADE;',
            f'DROP TABLE IF EXISTS "{name}" CASCADE;',
        ]
    for stmt in stmts:
        conn.execute(text(stmt))


CREATE_STMTS = {
    # ---------------- SILVER (PURE passthrough; add *_d only) ----------------
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
          fs.serve,
          fs.serve_type,
          fs.swing_type,
          fs.meta,
          ds.session_uid AS session_uid_d       -- DERIVED (labelled only)
        FROM fact_swing fs
        LEFT JOIN dim_session ds ON ds.session_id = fs.session_id;
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
          fpp.x, fpp.y
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
          s.bounce_type,
          ds.session_uid AS session_uid_d       -- DERIVED (labelled only)
        FROM src s
        LEFT JOIN xy_at_bounce xab ON xab.bounce_id = s.bounce_id
        LEFT JOIN dim_session ds    ON ds.session_id = s.session_id;
    """,

    # ---------------- ORDERING (serve-driven points; rally is fallback only if NO serves) ----------------
    "vw_point_order_by_serve": """
        CREATE OR REPLACE VIEW vw_point_order_by_serve AS
        WITH s AS (
          SELECT
            v.*,
            COALESCE(
              v.ball_hit_ts,
              v.start_ts,
              (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')
            ) AS ord_ts
          FROM vw_swing_silver v
        ),

        -- hitter position at (or just before) ball hit
        pos_at_hit AS (
          SELECT
            s.*,
            pp.x AS p_x_at_hit,
            pp.y AS p_y_at_hit
          FROM s
          LEFT JOIN LATERAL (
            SELECT x, y
            FROM vw_player_position_silver p
            WHERE p.session_id = s.session_id
              AND p.player_id  = s.player_id
              AND (
                    (p.ts   IS NOT NULL AND s.ball_hit_ts IS NOT NULL AND p.ts   <= s.ball_hit_ts)
                 OR ((p.ts IS NULL OR s.ball_hit_ts IS NULL) AND p.ts_s <= s.ball_hit_s)
                  )
            ORDER BY COALESCE(p.ts, (TIMESTAMP 'epoch' + p.ts_s * INTERVAL '1 second')) DESC
            LIMIT 1
          ) pp ON TRUE
        ),

        base AS (
          SELECT
            p.*,
            -- behind baseline if |y| >= 0.35 (tune if needed)
            CASE WHEN ABS(p.p_y_at_hit) >= 0.35 THEN TRUE ELSE FALSE END AS behind_baseline,
            -- serve-begin if SportAI serve flag OR overhead+behind-baseline
            CASE
              WHEN COALESCE(p.serve, FALSE) THEN TRUE
              WHEN p.swing_type ILIKE '%overhead%' AND ABS(p.p_y_at_hit) >= 0.35 THEN TRUE
              ELSE FALSE
            END AS is_serve_begin
          FROM pos_at_hit p
        ),

        seq AS (
          SELECT
            b.*,
            CASE
              WHEN SUM(CASE WHEN is_serve_begin THEN 1 ELSE 0 END) OVER (PARTITION BY b.session_id) = 0
                THEN 1
              ELSE 1 + SUM(CASE WHEN is_serve_begin THEN 1 ELSE 0 END)
                       OVER (PARTITION BY b.session_id ORDER BY b.ord_ts, b.swing_id
                             ROWS UNBOUNDED PRECEDING)
            END AS point_index
          FROM base b
        ),

        shots AS (
          SELECT
            seq.*,
            ROW_NUMBER() OVER (
              PARTITION BY seq.session_id, seq.point_index
              ORDER BY seq.ord_ts, seq.swing_id
            ) AS shot_number_in_point,
            MIN(seq.ord_ts) OVER (PARTITION BY seq.session_id, seq.point_index) AS point_ts0
          FROM seq
        ),

        -- fallback to rally segmentation only if there were zero serves in the session
        rally_fallback AS (
          SELECT
            sh.*,
            dr.rally_number,
            COALESCE(
              sh.point_index,
              DENSE_RANK() OVER (
                PARTITION BY sh.session_id
                ORDER BY dr.rally_number NULLS LAST, sh.point_ts0, sh.swing_id
              )
            ) AS point_number
          FROM shots sh
          LEFT JOIN dim_rally dr
            ON dr.session_id = sh.session_id
           AND sh.rally_id   = dr.rally_id
        )

        SELECT
          session_id,
          session_uid_d,
          swing_id,
          player_id,
          rally_id,
          start_s, end_s, ball_hit_s,
          start_ts, end_ts, ball_hit_ts,
          ball_hit_x, ball_hit_y,
          ball_speed,
          ball_player_distance,
          is_in_rally,
          serve, serve_type,
          swing_type,
          meta,
          p_x_at_hit, p_y_at_hit,          -- position at hit (used downstream)
          point_number,
          shot_number_in_point
        FROM rally_fallback;
    """,

    # ---------------- GOLD (enriched; derived fields grouped & suffixed *_d) ----------------
    "vw_point_log": """
        CREATE OR REPLACE VIEW vw_point_log AS
        WITH po AS (
          SELECT * FROM vw_point_order_by_serve
        ),

        /* first-shot row per point is the server swing */
        pt_first AS (
          SELECT DISTINCT ON (session_id, point_number)
            session_id, point_number,
            swing_id  AS first_swing_id,
            player_id AS server_id,
            ball_hit_ts AS first_hit_ts,
            ball_hit_s  AS first_hit_s
          FROM po
          ORDER BY session_id, point_number, shot_number_in_point
        ),

        /* last-shot row per point */
        pt_last AS (
          SELECT DISTINCT ON (session_id, point_number)
            session_id, point_number,
            swing_id  AS last_swing_id,
            player_id AS last_hitter_id,
            ball_hit_ts AS last_hit_ts,
            ball_hit_s  AS last_hit_s
          FROM po
          ORDER BY session_id, point_number, shot_number_in_point DESC
        ),

        /* bounce immediately after each swing */
        swing_bounce AS (
          SELECT
            s.swing_id, s.session_id,
            b.bounce_id,
            b.bounce_ts, b.bounce_s,
            b.x AS bounce_x, b.y AS bounce_y,
            b.bounce_type AS bounce_type_raw
          FROM po s
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

        /* receiver guess = first opponent present in the point */
        pp_receiver_guess AS (
          SELECT
            pf.session_id, pf.point_number,
            MIN(p.player_id) AS receiver_id_guess
          FROM pt_first pf
          LEFT JOIN po p
            ON p.session_id   = pf.session_id
           AND p.point_number = pf.point_number
           AND p.shot_number_in_point > 1
           AND p.player_id IS NOT NULL
           AND p.player_id <> pf.server_id
          GROUP BY pf.session_id, pf.point_number
        ),

        server_receiver AS (
          SELECT
            pf.session_id, pf.point_number,
            pf.server_id,
            COALESCE(
              rg.receiver_id_guess,
              (SELECT MIN(dp.player_id)
                 FROM dim_player dp
                WHERE dp.session_id = pf.session_id
                  AND dp.player_id <> pf.server_id)
            ) AS receiver_id
          FROM pt_first pf
          LEFT JOIN pp_receiver_guess rg
            ON rg.session_id  = pf.session_id
           AND rg.point_number = pf.point_number
        ),

        names AS (
          SELECT
            sr.*,
            dp1.full_name          AS server_name,
            dp1.sportai_player_uid AS server_uid,
            dp2.full_name          AS receiver_name,
            dp2.sportai_player_uid AS receiver_uid
          FROM server_receiver sr
          LEFT JOIN dim_player dp1
                 ON dp1.session_id = sr.session_id AND dp1.player_id = sr.server_id
          LEFT JOIN dim_player dp2
                 ON dp2.session_id = sr.session_id AND dp2.player_id = sr.receiver_id
        ),

        /* game numbering: increment only when server changes; first point is NOT a change */
        point_headers AS (
          SELECT
            n.*,
            LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number) AS prev_server_id,
            CASE
              WHEN LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number) IS NULL THEN 0
              WHEN LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number) IS DISTINCT FROM n.server_id THEN 1
              ELSE 0
            END AS new_game_flag
          FROM names n
        ),

        game_numbered AS (
          SELECT
            ph.*,
            1 + SUM(new_game_flag) OVER (PARTITION BY ph.session_id ORDER BY ph.point_number
                                         ROWS UNBOUNDED PRECEDING) AS game_number
          FROM point_headers ph
        ),

        game_seq AS (
          SELECT
            gn.*,
            ROW_NUMBER() OVER (PARTITION BY gn.session_id, gn.game_number ORDER BY gn.point_number) AS point_in_game
          FROM game_numbered gn
        ),

        /* server position at the first serve of the point */
        pf_pos AS (
          SELECT
            pf.session_id, pf.point_number,
            pp.x AS p_x_first,
            pp.y AS p_y_first
          FROM pt_first pf
          LEFT JOIN LATERAL (
            SELECT x, y
            FROM vw_player_position_silver p
            WHERE p.session_id = pf.session_id
              AND p.player_id  = pf.server_id
              AND (
                    (p.ts   IS NOT NULL AND pf.first_hit_ts IS NOT NULL AND p.ts   <= pf.first_hit_ts)
                 OR ((p.ts IS NULL OR pf.first_hit_ts IS NULL) AND p.ts_s <= pf.first_hit_s)
                  )
            ORDER BY COALESCE(p.ts, (TIMESTAMP 'epoch' + p.ts_s * INTERVAL '1 second')) DESC
            LIMIT 1
          ) pp ON TRUE
        ),

        /* first receiver swing (if any) */
        ret_from_swing AS (
          SELECT
            po.session_id,
            po.point_number,
            po.p_x_at_hit   AS p_x_first_return,
            ROW_NUMBER() OVER (
              PARTITION BY po.session_id, po.point_number
              ORDER BY po.shot_number_in_point
            ) AS rn
          FROM po
          JOIN names n
            ON n.session_id   = po.session_id
           AND n.point_number = po.point_number
          WHERE po.player_id = n.receiver_id
            AND po.shot_number_in_point > 1
        ),

        /* returner position at point start: prefer swing; else position at serve time */
        ret_pos AS (
          -- 1) swing-based if exists
          SELECT r.session_id, r.point_number, r.p_x_first_return AS r_x_first
          FROM ret_from_swing r
          WHERE r.rn = 1

          UNION ALL

          -- 2) fallback to receiver position at serve time
          SELECT n.session_id, n.point_number, ppr.x AS r_x_first
          FROM names n
          LEFT JOIN ret_from_swing r
            ON r.session_id  = n.session_id
           AND r.point_number = n.point_number
           AND r.rn = 1
          LEFT JOIN LATERAL (
            SELECT x
            FROM vw_player_position_silver p
            JOIN pt_first pf
              ON pf.session_id   = n.session_id
             AND pf.point_number = n.point_number
            WHERE p.session_id = n.session_id
              AND p.player_id  = n.receiver_id
              AND (
                    (p.ts   IS NOT NULL AND pf.first_hit_ts IS NOT NULL AND p.ts   <= pf.first_hit_ts)
                 OR ((p.ts IS NULL OR pf.first_hit_ts IS NULL) AND p.ts_s <= pf.first_hit_s)
                  )
            ORDER BY COALESCE(p.ts, (TIMESTAMP 'epoch' + p.ts_s * INTERVAL '1 second')) DESC
            LIMIT 1
          ) ppr ON TRUE
          WHERE r.session_id IS NULL
        ),

        /* bounce for the serve (first swing of point) */
        serve_bounce AS (
          SELECT
            pf.session_id, pf.point_number,
            sb.bounce_x, sb.bounce_y, sb.bounce_type_raw
          FROM pt_first pf
          LEFT JOIN swing_bounce sb
            ON sb.session_id = pf.session_id AND sb.swing_id = pf.first_swing_id
        ),

        /* serve placement bucket and raw x for side fallback */
        serve_bucket AS (
          SELECT
            sb.*,
            sb.bounce_x AS serve_first_bounce_x,
            CASE
              WHEN sb.bounce_x IS NULL OR sb.bounce_y IS NULL THEN NULL
              ELSE (CASE WHEN sb.bounce_y >= 0 THEN 1 ELSE 0 END) * 4
                 + (CASE
                      WHEN sb.bounce_x < -0.5 THEN 1
                      WHEN sb.bounce_x <  0.0 THEN 2
                      WHEN sb.bounce_x <  0.5 THEN 3
                      ELSE 4
                    END)
            END AS serve_bucket_1_8
          FROM serve_bounce sb
        ),

        /* last bounce for the point */
        last_bounce AS (
          SELECT
            pl.session_id, pl.point_number,
            sb.bounce_x AS last_bounce_x, sb.bounce_y AS last_bounce_y,
            sb.bounce_type_raw AS last_bounce_type
          FROM pt_last pl
          LEFT JOIN swing_bounce sb
            ON sb.session_id = pl.session_id AND sb.swing_id = pl.last_swing_id
        )

        SELECT
          -- identity & ordering
          po.session_uid_d,
          po.session_id,
          po.rally_id,
          po.point_number,
          po.shot_number_in_point AS shot_number,

          po.swing_id,
          po.player_id,

          /* ===== DERIVED FIELDS BLOCK (_d only) ===== */

          -- game & point counters
          gs.game_number                         AS game_number_d,
          gs.point_in_game                       AS point_in_game_d,

          -- final serving side using server/receiver cross-check with bounce fallback
          CASE
            -- prefer agreement between server x and receiver x (inverted)
            WHEN
              (CASE WHEN fp.p_x_first >=  0.15 THEN 'deuce'
                    WHEN fp.p_x_first <= -0.15 THEN 'ad' ELSE NULL END)
              =
              (CASE WHEN rp.r_x_first >=  0.15 THEN 'ad'
                    WHEN rp.r_x_first <= -0.15 THEN 'deuce' ELSE NULL END)
              AND
              (CASE WHEN fp.p_x_first >=  0.15 THEN 'deuce'
                    WHEN fp.p_x_first <= -0.15 THEN 'ad' ELSE NULL END) IS NOT NULL
            THEN (CASE WHEN fp.p_x_first >=  0.15 THEN 'deuce' ELSE 'ad' END)

            -- server ambiguous, receiver clear -> use receiver
            WHEN (CASE WHEN fp.p_x_first >=  0.15 THEN 'deuce'
                       WHEN fp.p_x_first <= -0.15 THEN 'ad' ELSE NULL END) IS NULL
                 AND
                 (CASE WHEN rp.r_x_first >=  0.15 THEN 'ad'
                       WHEN rp.r_x_first <= -0.15 THEN 'deuce' ELSE NULL END) IS NOT NULL
            THEN (CASE WHEN rp.r_x_first >=  0.15 THEN 'ad' ELSE 'deuce' END)

            -- receiver ambiguous, server clear -> use server
            WHEN (CASE WHEN rp.r_x_first >=  0.15 THEN 'ad'
                       WHEN rp.r_x_first <= -0.15 THEN 'deuce' ELSE NULL END) IS NULL
                 AND
                 (CASE WHEN fp.p_x_first >=  0.15 THEN 'deuce'
                       WHEN fp.p_x_first <= -0.15 THEN 'ad' ELSE NULL END) IS NOT NULL
            THEN (CASE WHEN fp.p_x_first >=  0.15 THEN 'deuce' ELSE 'ad' END)

            -- last resort: serve bounce x (cross-court)
            WHEN sv.serve_first_bounce_x IS NOT NULL
            THEN CASE WHEN sv.serve_first_bounce_x >= 0 THEN 'ad' ELSE 'deuce' END

            ELSE 'deuce'
          END                                     AS serving_side_d,

          -- serve per swing: SportAI flag OR overhead+behind-baseline at hit
          CASE
            WHEN COALESCE(po.serve, FALSE) THEN TRUE
            WHEN po.swing_type ILIKE '%overhead%' AND ABS(po.p_y_at_hit) >= 0.35 THEN TRUE
            ELSE FALSE
          END                                     AS serve_d,

          -- simple swing error proxy
          CASE WHEN COALESCE(po.ball_speed, 0) = 0 THEN TRUE ELSE FALSE END
                                                  AS is_error_d,

          lb.last_bounce_type                     AS point_end_bounce_type_d,

          -- serve placement bucket
          sv.serve_bucket_1_8                     AS serve_bucket_1_8_d,

          -- diagnostics (useful in BI to validate)
          fp.p_x_first                            AS server_pos_x_at_first_d,
          CASE WHEN ABS(fp.p_y_first) >= 0.35 THEN TRUE ELSE FALSE END
                                                  AS server_behind_baseline_at_first_d,
          po.p_x_at_hit                           AS player_pos_x_at_hit_d,
          po.p_y_at_hit                           AS player_pos_y_at_hit_d,

          -- counts for lets/fault/double-fault analysis
          COUNT(*) FILTER (
            WHERE
              (COALESCE(po.serve, FALSE) OR (po.swing_type ILIKE '%overhead%' AND ABS(po.p_y_at_hit) >= 0.35))
          ) OVER (PARTITION BY po.session_id, po.point_number)
                                                  AS serve_count_in_point_d,

          COUNT(*) FILTER (
            WHERE
              (COALESCE(po.serve, FALSE) OR (po.swing_type ILIKE '%overhead%' AND ABS(po.p_y_at_hit) >= 0.35))
              AND COALESCE(po.ball_speed, 0) = 0
          ) OVER (PARTITION BY po.session_id, po.point_number)
                                                  AS fault_serves_in_point_d,

          /* ---- passthrough (bronze -> silver; keep names intact) ---- */
          po.swing_type                           AS swing_type_raw,
          po.ball_speed,
          po.ball_player_distance,
          po.start_s,  po.end_s,  po.ball_hit_s,
          po.start_ts, po.end_ts, po.ball_hit_ts,

          /* bounce immediately after this swing (passthrough) */
          sb.bounce_id,
          sb.bounce_x                             AS ball_bounce_x,
          sb.bounce_y                             AS ball_bounce_y,
          sb.bounce_type_raw                      AS bounce_type_raw,

          /* names/ids for context (passthrough) */
          n.server_id,    n.server_name,    n.server_uid,
          n.receiver_id,  n.receiver_name,  n.receiver_uid

        FROM po
        LEFT JOIN swing_bounce sb ON sb.session_id = po.session_id AND sb.swing_id     = po.swing_id
        LEFT JOIN game_seq     gs ON gs.session_id = po.session_id AND gs.point_number = po.point_number
        LEFT JOIN last_bounce  lb ON lb.session_id = po.session_id AND lb.point_number = po.point_number
        LEFT JOIN serve_bucket sv ON sv.session_id = po.session_id AND sv.point_number = po.point_number
        LEFT JOIN names         n ON n.session_id  = po.session_id AND n.point_number  = po.point_number
        LEFT JOIN pf_pos        fp ON fp.session_id = po.session_id AND fp.point_number = po.point_number
        LEFT JOIN ret_pos       rp ON rp.session_id = po.session_id AND rp.point_number = po.point_number
        ORDER BY po.session_uid_d, po.point_number, po.shot_number_in_point, po.swing_id;
    """,
}


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
        "dim_session", "dim_player", "dim_rally",
        "fact_swing", "fact_bounce",
        "fact_player_position", "fact_ball_position",
    ]
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(f"Missing base tables before creating views: {', '.join(missing)}")

    checks = [
        ("dim_session", "session_uid"),
        ("dim_player", "full_name"),
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
        ("fact_swing", "ball_player_distance"),
        ("fact_swing", "is_in_rally"),
        ("fact_swing", "serve"),
        ("fact_swing", "serve_type"),
        ("fact_swing", "swing_type"),
        ("fact_swing", "meta"),

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
    missing_cols = [(t, c) for (t, c) in checks if not _column_exists(conn, t, c)]
    if missing_cols:
        msg = ", ".join([f"{t}.{c}" for (t, c) in missing_cols])
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
