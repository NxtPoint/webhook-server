# db_views.py — Bronze ➜ Silver (no edits to Bronze columns), Gold mirrors Silver,
# with a fully isolated DERIVED FIELDS BLOCK.
#
# Rules:
# - Silver always selects all Bronze columns unchanged.
# - Any additional / derived / joined fields must be suffixed with (d) and quoted, e.g. "serve(d)".
# - Gold = SELECT * FROM Silver (for now).
#
# Copy/paste the DERIVED FIELDS BLOCK (below in both places) wholesale when we tweak logic.

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
    # SILVER (Bronze pass-through + extra "(d)" columns only)
    "vw_swing_silver",
    "vw_ball_position_silver",
    "vw_player_position_silver",
    "vw_bounce_silver",

    # Derived ordering + enriched rows (still Silver; adds only "(d)" columns)
    "vw_point_order_silver",
    "vw_point_log_silver",

    # GOLD wrappers (mirror Silver for now)
    "vw_swing_gold",
    "vw_ball_position_gold",
    "vw_player_position_gold",
    "vw_bounce_gold",
    "vw_point_order_gold",
    "vw_point_log_gold",
]

# Legacy objects we want to drop if they still exist, to avoid mismatched types
LEGACY_OBJECTS = [
    "vw_point_shot_log_gold",
    "vw_shot_order_gold",
    "vw_point_summary",
    "point_log_tbl",
    "point_summary_tbl",
    "vw_point_shot_log",
    "vw_swing",
    "vw_ball_position",
    "vw_player_position",
    "vw_bounce",
    "vw_point_order_by_serve",
    "vw_point_log",
]

def _drop_any(conn, name: str):
    """
    Drop any object named `name` in public schema, picking the right DROP
    for its actual type to avoid WrongObjectType errors.
    """
    kind = conn.execute(text("""
        SELECT CASE
                 WHEN EXISTS (
                    SELECT 1
                    FROM information_schema.views
                    WHERE table_schema='public' AND table_name=:n
                 ) THEN 'view'
                 WHEN EXISTS (
                    SELECT 1
                    FROM pg_matviews
                    WHERE schemaname='public' AND matviewname=:n
                 ) THEN 'mview'
                 WHEN EXISTS (
                    SELECT 1
                    FROM information_schema.tables
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
        # best-effort cleanup if we can’t detect the type
        stmts = [
            f'DROP VIEW IF EXISTS "{name}" CASCADE;',
            f'DROP MATERIALIZED VIEW IF EXISTS "{name}" CASCADE;',
            f'DROP TABLE IF EXISTS "{name}" CASCADE;',
        ]

    for stmt in stmts:
        conn.execute(text(stmt))

# =============================================================================
# ===========================  SILVER: BRONZE + (d)  ==========================
# =============================================================================
CREATE_STMTS = {

    # fact_swing passthrough (+ session/player labels as "(d)")
    "vw_swing_silver": """
        CREATE OR REPLACE VIEW vw_swing_silver AS
        SELECT
          fs.*,                                    -- all Bronze columns unchanged
          ds.session_uid               AS "session_uid(d)",
          dp.full_name                AS "player_name(d)",
          dp.sportai_player_uid       AS "player_uid(d)"
        FROM fact_swing fs
        LEFT JOIN dim_session ds ON ds.session_id = fs.session_id
        LEFT JOIN dim_player  dp ON dp.session_id = fs.session_id AND dp.player_id = fs.player_id;
    """,

    # fact_ball_position passthrough (+ session label)
    "vw_ball_position_silver": """
        CREATE OR REPLACE VIEW vw_ball_position_silver AS
        SELECT
          fbp.*,                                   -- Bronze columns unchanged
          ds.session_uid               AS "session_uid(d)"
        FROM fact_ball_position fbp
        LEFT JOIN dim_session ds ON ds.session_id = fbp.session_id;
    """,

    # fact_player_position passthrough (+ session + player labels)
    "vw_player_position_silver": """
        CREATE OR REPLACE VIEW vw_player_position_silver AS
        SELECT
          fpp.*,                                   -- Bronze columns unchanged
          ds.session_uid               AS "session_uid(d)",
          dp.full_name                AS "player_name(d)",
          dp.sportai_player_uid       AS "player_uid(d)"
        FROM fact_player_position fpp
        LEFT JOIN dim_session ds ON ds.session_id = fpp.session_id
        LEFT JOIN dim_player  dp ON dp.session_id = fpp.session_id AND dp.player_id = fpp.player_id;
    """,

    # fact_bounce passthrough (+ session + hitter labels + coalesced XY as "(d)")
    "vw_bounce_silver": """
        CREATE OR REPLACE VIEW vw_bounce_silver AS
        WITH src AS (
          SELECT
            b.*,                                     -- Bronze columns unchanged
            ds.session_uid             AS "session_uid(d)",
            dp.full_name              AS "hitter_name(d)",
            dp.sportai_player_uid     AS "hitter_player_uid(d)"
          FROM fact_bounce b
          LEFT JOIN dim_session ds ON ds.session_id = b.session_id
          LEFT JOIN dim_player  dp ON dp.session_id = b.session_id AND dp.player_id = b.hitter_player_id
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
          s.*,
          -- keep original x/y intact; add coalesced versions as "(d)"
          COALESCE(s.x, xab.x_bp) AS "x_coalesced(d)",
          COALESCE(s.y, xab.y_bp) AS "y_coalesced(d)"
        FROM src s
        LEFT JOIN xy_at_bounce xab ON xab.bounce_id = s.bounce_id;
    """,

    # =============================================================================
    # =====================  DERIVED ORDERING (Silver; adds "(d)")  ===============
    # =============================================================================
    "vw_point_order_silver": """
        /* One row per swing with point/shot numbering (derived) but keeps all Bronze swing fields intact. */
        CREATE OR REPLACE VIEW vw_point_order_silver AS
        WITH s AS (
          SELECT
            v.*,  -- includes all Bronze columns + labels "(d)"
            /* robust ordering key */
            COALESCE(
              v.ball_hit_ts,
              v.start_ts,
              (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')
            ) AS "ord_ts(d)",

            /* treat overhead and explicit serve as serve-begin candidates */
            (v.swing_type ILIKE '%overhead%') AS "is_overhead(d)",
            CASE WHEN COALESCE(v.serve, FALSE) OR (v.swing_type ILIKE '%overhead%')
                 THEN 1 ELSE 0 END AS "is_serve_begin(d)"
          FROM vw_swing_silver v
        ),
        seq AS (
          SELECT
            s.*,
            CASE
              WHEN SUM("is_serve_begin(d)") OVER (PARTITION BY s.session_id) = 0
                THEN 1
              ELSE 1 + SUM("is_serve_begin(d)") OVER (PARTITION BY s.session_id ORDER BY "ord_ts(d)", s.swing_id
                                                     ROWS UNBOUNDED PRECEDING)
            END AS "point_index(d)"
          FROM s
        ),
        shots AS (
          SELECT
            seq.*,
            ROW_NUMBER() OVER (PARTITION BY seq.session_id, "point_index(d)" ORDER BY "ord_ts(d)", seq.swing_id)
              AS "shot_number_in_point(d)",
            MIN("ord_ts(d)") OVER (PARTITION BY seq.session_id, "point_index(d)") AS "point_ts0(d)"
          FROM seq
        ),
        rally_fallback AS (
          SELECT
            sh.*,
            dr.rally_number,
            COALESCE("point_index(d)",
                     DENSE_RANK() OVER (PARTITION BY sh.session_id
                                        ORDER BY dr.rally_number NULLS LAST, "point_ts0(d)", sh.swing_id)
            ) AS "point_number(d)"
          FROM shots sh
          LEFT JOIN dim_rally dr
            ON dr.session_id = sh.session_id AND sh.rally_id = dr.rally_id
        )
        SELECT * FROM rally_fallback;
    """,

    # =============================================================================
    # ==================  ENRICHED POINT ROWS (Silver; adds "(d)")  ===============
    # =============================================================================
    "vw_point_log_silver": """
        /* One row per swing with tennis-aware point & shot numbers + derived context.
           We KEEP all swing Bronze fields; add only "(d)" columns for deriveds.
        */
        CREATE OR REPLACE VIEW vw_point_log_silver AS
        WITH po AS (
          SELECT * FROM vw_point_order_silver
        ),
        pt_first AS (
          SELECT DISTINCT ON (session_id, "point_number(d)")
            session_id, "point_number(d)", swing_id AS first_swing_id,
            player_id AS server_id, player_name AS "server_name(d)", "player_uid(d)" AS "server_uid(d)",
            ball_hit_ts AS "first_hit_ts(d)", ball_hit_s AS "first_hit_s(d)"
          FROM po
          ORDER BY session_id, "point_number(d)", "shot_number_in_point(d)"
        ),
        pt_last AS (
          SELECT DISTINCT ON (session_id, "point_number(d)")
            session_id, "point_number(d)", swing_id AS last_swing_id,
            player_id AS last_hitter_id, player_name AS "last_hitter_name(d)",
            ball_hit_ts AS "last_hit_ts(d)", ball_hit_s AS "last_hit_s(d)"
          FROM po
          ORDER BY session_id, "point_number(d)", "shot_number_in_point(d)" DESC
        ),
        /* per-swing bounce just after hit */
        s_bounce AS (
          SELECT
            s.swing_id, s.session_id, s."point_number(d)", s."shot_number_in_point(d)",
            b.bounce_id,
            b.bounce_ts, b.bounce_s,
            b."x_coalesced(d)" AS "ball_bounce_x(d)",
            b."y_coalesced(d)" AS "ball_bounce_y(d)",
            b.bounce_type     AS "bounce_type_raw(d)"
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

        /* ======= DERIVED FIELDS BLOCK (copy/paste this block when we change logic) ======= */
        -- BEGIN DERIVED FIELDS BLOCK
        pp_receiver_guess AS (
          SELECT pf.session_id, pf."point_number(d)",
                 MIN(p.player_id) AS "receiver_id(d)"
          FROM pt_first pf
          LEFT JOIN po p
            ON p.session_id = pf.session_id
           AND p."point_number(d)" = pf."point_number(d)"
           AND p."shot_number_in_point(d)" > 1
           AND p.player_id IS NOT NULL
           AND p.player_id <> pf.server_id
          GROUP BY pf.session_id, pf."point_number(d)"
        ),
        names AS (
          SELECT
            pf.session_id, pf."point_number(d)", pf.server_id,
            COALESCE(rg."receiver_id(d)",
                     (SELECT MIN(dp.player_id)
                        FROM dim_player dp
                       WHERE dp.session_id = pf.session_id
                         AND dp.player_id <> pf.server_id)) AS "receiver_id(d)"
          FROM pt_first pf
          LEFT JOIN pp_receiver_guess rg
            ON rg.session_id = pf.session_id AND rg."point_number(d)" = pf."point_number(d)"
        ),
        names_joined AS (
          SELECT
            n.*,
            dp1.full_name          AS "server_name(d)",
            dp1.sportai_player_uid AS "server_uid(d)",
            dp2.full_name          AS "receiver_name(d)",
            dp2.sportai_player_uid AS "receiver_uid(d)"
          FROM names n
          LEFT JOIN dim_player dp1 ON dp1.session_id = n.session_id AND dp1.player_id = n.server_id
          LEFT JOIN dim_player dp2 ON dp2.session_id = n.session_id AND dp2.player_id = n."receiver_id(d)"
        ),
        point_outcome AS (
          SELECT
            pl.session_id, pl."point_number(d)",
            sb."bounce_type_raw(d)" AS "point_end_bounce_type(d)",
            CASE
              WHEN sb."bounce_type_raw(d)" IN ('out','net','long','wide') THEN
                CASE WHEN pl.last_hitter_id = nj.server_id THEN nj."receiver_id(d)" ELSE nj.server_id END
              ELSE NULL
            END AS "point_winner_id(d)"
          FROM pt_last pl
          LEFT JOIN s_bounce sb
                 ON sb.session_id = pl.session_id AND sb.swing_id = pl.last_swing_id
          LEFT JOIN names_joined nj
                 ON nj.session_id = pl.session_id AND nj."point_number(d)" = pl."point_number(d)"
        ),
        point_headers AS (
          SELECT
            nj.*,
            pf."first_hit_ts(d)",
            LAG(nj.server_id) OVER (PARTITION BY nj.session_id ORDER BY nj."point_number(d)") AS "prev_server_id(d)",
            CASE WHEN LAG(nj.server_id) OVER (PARTITION BY nj.session_id ORDER BY nj."point_number(d)")
                      IS DISTINCT FROM nj.server_id THEN 1 ELSE 0 END AS "new_game_flag(d)"
          FROM names_joined nj
          LEFT JOIN pt_first pf
            ON pf.session_id = nj.session_id AND pf."point_number(d)" = nj."point_number(d)"
        ),
        game_numbered AS (
          SELECT
            ph.*,
            1 + SUM("new_game_flag(d)") OVER (PARTITION BY ph.session_id ORDER BY ph."point_number(d)"
                                              ROWS UNBOUNDED PRECEDING) AS "game_number(d)"
          FROM point_headers ph
        ),
        game_seq AS (
          SELECT
            gn.*,
            ROW_NUMBER() OVER (PARTITION BY gn.session_id, "game_number(d)" ORDER BY gn."point_number(d)")
              AS "point_in_game(d)"
          FROM game_numbered gn
        ),
        game_score AS (
          SELECT
            gs.session_id, gs."point_number(d)", gs."game_number(d)", gs."point_in_game(d)",
            gs.server_id, gs."receiver_id(d)", gs."server_name(d)", gs."receiver_name(d)",
            gs."server_uid(d)", gs."receiver_uid(d)", gs."first_hit_ts(d)",
            po2."point_winner_id(d)",
            SUM(CASE WHEN po2."point_winner_id(d)" = gs.server_id     THEN 1 ELSE 0 END)
               OVER (PARTITION BY gs.session_id, gs."game_number(d)" ORDER BY gs."point_number(d)")
                 AS "server_points_in_game(d)",
            SUM(CASE WHEN po2."point_winner_id(d)" = gs."receiver_id(d)" THEN 1 ELSE 0 END)
               OVER (PARTITION BY gs.session_id, gs."game_number(d)" ORDER BY gs."point_number(d)")
                 AS "receiver_points_in_game(d)"
          FROM game_seq gs
          LEFT JOIN point_outcome po2
            ON po2.session_id = gs.session_id AND po2."point_number(d)" = gs."point_number(d)"
        ),
        score_text AS (
          SELECT
            g.*,
            CASE
              WHEN GREATEST(COALESCE("server_points_in_game(d)",0), COALESCE("receiver_points_in_game(d)",0)) >= 4
                   AND ABS(COALESCE("server_points_in_game(d)",0) - COALESCE("receiver_points_in_game(d)",0)) >= 2
                THEN 'game'
              WHEN COALESCE("server_points_in_game(d)",0) >= 3 AND COALESCE("receiver_points_in_game(d)",0) >= 3 THEN
                CASE
                  WHEN "server_points_in_game(d)" = "receiver_points_in_game(d)" THEN '40-40'
                  WHEN "server_points_in_game(d)"  > "receiver_points_in_game(d)" THEN 'Ad-40'
                  ELSE '40-Ad'
                END
              ELSE
                concat(
                  CASE COALESCE("server_points_in_game(d)",0)
                    WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END,
                  '-',
                  CASE COALESCE("receiver_points_in_game(d)",0)
                    WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END
                )
            END AS "score_server_first(d)"
          FROM game_score g
        ),
        serve_bounce AS (
          SELECT
            pf.session_id, pf."point_number(d)",
            sb."ball_bounce_x(d)", sb."ball_bounce_y(d)", sb."bounce_type_raw(d)"
          FROM pt_first pf
          LEFT JOIN s_bounce sb
            ON sb.session_id = pf.session_id AND sb.swing_id = pf.first_swing_id
        ),
        serve_bucket AS (
          SELECT
            sb.*,
            CASE
              WHEN sb."ball_bounce_x(d)" IS NULL OR sb."ball_bounce_y(d)" IS NULL THEN NULL
              ELSE
                (CASE WHEN sb."ball_bounce_y(d)" >= 0 THEN 1 ELSE 0 END) * 4
                + (CASE
                     WHEN sb."ball_bounce_x(d)" < -0.5 THEN 1
                     WHEN sb."ball_bounce_x(d)" <  0.0 THEN 2
                     WHEN sb."ball_bounce_x(d)" <  0.5 THEN 3
                     ELSE 4
                   END)
            END AS "serve_bucket_1_8(d)"
          FROM serve_bounce sb
        ),
        /* serve-detection per swing */
        per_swing_serve AS (
          SELECT
            po.*,
            sb."ball_bounce_x(d)", sb."ball_bounce_y(d)", sb."bounce_type_raw(d)",

            -- fault on an overhead (0 speed or fault bounce)
            CASE
              WHEN (po.swing_type ILIKE '%overhead%')
                   AND (COALESCE(po.ball_speed,0)=0
                        OR sb."bounce_type_raw(d)" IN ('out','net','long','wide'))
                THEN 1 ELSE 0
            END AS "serve_fault(d)",

            -- derived serve flag: first overhead; or second overhead only if prev was a fault
            CASE
              WHEN (po.swing_type ILIKE '%overhead%') AND po."shot_number_in_point(d)" = 1
                THEN TRUE
              WHEN (po.swing_type ILIKE '%overhead%') AND po."shot_number_in_point(d)" = 2
                   AND COALESCE(LAG(CASE
                                      WHEN (po.swing_type ILIKE '%overhead%')
                                           AND (COALESCE(po.ball_speed,0)=0
                                                OR sb."bounce_type_raw(d)" IN ('out','net','long','wide'))
                                        THEN 1 ELSE 0
                                    END)
                       OVER (PARTITION BY po.session_id, po."point_number(d)" ORDER BY po."shot_number_in_point(d)"),0) = 1
                THEN TRUE
              ELSE FALSE
            END AS "serve(d)"
          FROM po
          LEFT JOIN s_bounce sb
                 ON sb.session_id = po.session_id AND sb.swing_id = po.swing_id
        )
        -- END DERIVED FIELDS BLOCK

        SELECT
          po."session_uid(d)",             -- label from swing silver
          ps.session_id,
          po.rally_id,                     -- Bronze diagnostic parity
          po."point_number(d)" AS "point_number(d)",
          po."shot_number_in_point(d)" AS "shot_number(d)",

          po.swing_id,
          po.player_id, po.player_name, po."player_uid(d)",

          -- server/receiver/game context
          st."game_number(d)",
          st."point_in_game(d)",
          CASE WHEN (st."point_in_game(d)" % 2) = 1 THEN 'deuce' ELSE 'ad' END AS "serving_side(d)",
          st.server_id, st."server_name(d)", st."server_uid(d)",
          st."receiver_id(d)", st."receiver_name(d)", st."receiver_uid(d)",

          -- derived serve flag
          ps."serve(d)",
          po.serve_type,          -- Bronze
          po.swing_type AS swing_type,  -- Bronze

          po.ball_speed,          -- Bronze
          po.ball_player_distance,-- Bronze

          po.start_s, po.end_s, po.ball_hit_s,     -- Bronze
          po.start_ts, po.end_ts, po.ball_hit_ts,  -- Bronze

          -- bounce after this swing
          ps."ball_bounce_x(d)",
          ps."ball_bounce_y(d)",
          ps."bounce_type_raw(d)",

          -- coarse shot bucket
          CASE
            WHEN ps."ball_bounce_x(d)" IS NULL OR ps."ball_bounce_y(d)" IS NULL THEN NULL
            WHEN ps."ball_bounce_y(d)" >= 0 AND ps."ball_bounce_x(d)" < 0  THEN 'A'
            WHEN ps."ball_bounce_y(d)" >= 0 AND ps."ball_bounce_x(d)" >= 0 THEN 'B'
            WHEN ps."ball_bounce_y(d)" <  0 AND ps."ball_bounce_x(d)" < 0  THEN 'C'
            ELSE 'D'
          END AS "shot_loc_bucket_ad(d)",

          -- point outcome
          po2."point_winner_id(d)",
          CASE
            WHEN po2."point_winner_id(d)" IS NULL THEN NULL
            WHEN po2."point_winner_id(d)" = st.server_id THEN st."server_name(d)" ELSE st."receiver_name(d)"
          END AS "point_winner_name(d)",
          lb."point_end_bounce_type(d)",
          CASE WHEN lb."point_end_bounce_type(d)" IN ('out','net','long','wide') THEN TRUE ELSE FALSE END AS "is_error(d)",
          CASE
            WHEN lb."point_end_bounce_type(d)" IN ('out','net','long','wide') THEN lb."point_end_bounce_type(d)"
            ELSE NULL
          END AS "error_type(d)",

          -- score text (server-first)
          st."score_server_first(d)",

          -- serve placement bucket for the point
          sv."serve_bucket_1_8(d)"

        FROM per_swing_serve ps
        JOIN po ON po.session_id = ps.session_id AND po.swing_id = ps.swing_id
        LEFT JOIN score_text st
               ON st.session_id = ps.session_id AND st."point_number(d)" = ps."point_number(d)"
        LEFT JOIN point_outcome po2
               ON po2.session_id = ps.session_id AND po2."point_number(d)" = ps."point_number(d)"
        LEFT JOIN (
          SELECT session_id, "point_number(d)", "point_end_bounce_type(d)"
          FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY session_id, "point_number(d)" ORDER BY "point_number(d)") rn
            FROM point_outcome
          ) q WHERE rn = 1
        ) lb ON lb.session_id = ps.session_id AND lb."point_number(d)" = ps."point_number(d)"
        LEFT JOIN serve_bucket sv
               ON sv.session_id = ps.session_id AND sv."point_number(d)" = ps."point_number(d)"
        ORDER BY po."session_uid(d)", ps."point_number(d)", ps."shot_number_in_point(d)", ps.swing_id;
    """,

    # =============================================================================
    # =============================  GOLD (mirror)  ===============================
    # =============================================================================
    "vw_swing_gold": """
        CREATE OR REPLACE VIEW vw_swing_gold AS
        SELECT * FROM vw_swing_silver;
    """,

    "vw_ball_position_gold": """
        CREATE OR REPLACE VIEW vw_ball_position_gold AS
        SELECT * FROM vw_ball_position_silver;
    """,

    "vw_player_position_gold": """
        CREATE OR REPLACE VIEW vw_player_position_gold AS
        SELECT * FROM vw_player_position_silver;
    """,

    "vw_bounce_gold": """
        CREATE OR REPLACE VIEW vw_bounce_gold AS
        SELECT * FROM vw_bounce_silver;
    """,

    "vw_point_order_gold": """
        CREATE OR REPLACE VIEW vw_point_order_gold AS
        SELECT * FROM vw_point_order_silver;
    """,

    "vw_point_log_gold": """
        CREATE OR REPLACE VIEW vw_point_log_gold AS
        SELECT * FROM vw_point_log_silver;
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
