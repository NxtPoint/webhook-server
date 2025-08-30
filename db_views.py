# db_views.py — Silver passthrough + tennis-aware point ordering & enriched point rows
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
# SILVER = strict pass-through from BRONZE (no edits)
# GOLD-ish (tennis-aware) = reads only SILVER; adds ordering + safe derivations (no mutation of bronze)
VIEW_NAMES = [
    # SILVER (pure)
    "vw_swing",
    "vw_bounce",
    "vw_ball_position",
    "vw_player_position",

    # Order and tennis derivations
    "vw_point_order_by_serve",  # one row per swing with point/shot numbers
    "vw_point_log",             # enriched rows Power BI reads (kept name)
]

# Legacy objects we want to drop if they still exist, to avoid 500s in init-views
LEGACY_OBJECTS = [
    "vw_point_shot_log_gold",
    "vw_shot_order_gold",
    "vw_point_summary",
    "point_log_tbl",
    "point_summary_tbl",
    # add any older aliases here if they show up in errors:
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

CREATE_STMTS = {
    # ---------------- SILVER (PURE passthrough + labels) ----------------
    "vw_swing": """
        CREATE OR REPLACE VIEW vw_swing AS
        SELECT
          ds.session_uid,
          fs.session_id,
          fs.swing_id,
          fs.player_id,
          dp.full_name          AS player_name,
          dp.sportai_player_uid AS player_uid,
          fs.rally_id,
          fs.start_s, fs.end_s, fs.ball_hit_s,
          fs.start_ts, fs.end_ts, fs.ball_hit_ts,
          fs.ball_hit_x, fs.ball_hit_y,
          fs.ball_speed,
          fs.ball_player_distance,
          fs.is_in_rally,
          fs.serve, fs.serve_type,
          fs.swing_type,
          fs.meta
        FROM fact_swing fs
        LEFT JOIN dim_session ds
               ON ds.session_id = fs.session_id
        LEFT JOIN dim_player  dp
               ON dp.session_id = fs.session_id
              AND dp.player_id  = fs.player_id;
    """,

    "vw_ball_position": """
        CREATE OR REPLACE VIEW vw_ball_position AS
        SELECT
          ds.session_uid,
          fbp.session_id,
          fbp.ts_s,
          fbp.ts,
          fbp.x, fbp.y
        FROM fact_ball_position fbp
        LEFT JOIN dim_session ds
               ON ds.session_id = fbp.session_id;
    """,

    "vw_player_position": """
        CREATE OR REPLACE VIEW vw_player_position AS
        SELECT
          ds.session_uid,
          fpp.session_id,
          fpp.player_id,
          dp.full_name          AS player_name,
          dp.sportai_player_uid AS player_uid,
          fpp.ts_s,
          fpp.ts,
          fpp.x, fpp.y
        FROM fact_player_position fpp
        LEFT JOIN dim_session ds
               ON ds.session_id = fpp.session_id
        LEFT JOIN dim_player  dp
               ON dp.session_id = fpp.session_id
              AND dp.player_id  = fpp.player_id;
    """,

    # PURE bounce; if fact_bounce.x/y are NULL, take exact-equality XY from fact_ball_position at the same instant
    "vw_bounce": """
    CREATE OR REPLACE VIEW vw_bounce AS
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
      ds.session_uid,
      s.session_id,
      s.bounce_id,
      s.hitter_player_id,
      dp.full_name          AS hitter_name,
      dp.sportai_player_uid AS hitter_player_uid,
      s.rally_id,
      s.bounce_s,
      s.bounce_ts,
      COALESCE(s.x_raw, xab.x_bp) AS x,
      COALESCE(s.y_raw, xab.y_bp) AS y,
      s.bounce_type
    FROM src s
    LEFT JOIN xy_at_bounce xab ON xab.bounce_id = s.bounce_id
    LEFT JOIN dim_session ds    ON ds.session_id = s.session_id
    LEFT JOIN dim_player  dp    ON dp.session_id = s.session_id AND dp.player_id = s.hitter_player_id;
""",

    # ---------------- ORDERING (serve-driven points; rally is fallback) ----------------
    "vw_point_order_by_serve": """
    /* One row per swing with point/shot numbering.
       We open a new point when we see a serve=True swing.
       If serve labels are missing, we fallback to rally segmentation. */
    CREATE OR REPLACE VIEW vw_point_order_by_serve AS
    WITH s AS (
      SELECT
        v.*,
        /* robust ordering key */
        COALESCE(v.ball_hit_ts, v.start_ts, make_timestamp(1970,1,1,0,0,0) + make_interval(secs => COALESCE(v.ball_hit_s, v.start_s, 0))) AS ord_ts
      FROM vw_swing v
    ),
    base AS (
      SELECT
        s.*,
        /* mark begins strictly on serve=True */
        CASE WHEN COALESCE(s.serve, FALSE) THEN 1 ELSE 0 END AS is_serve_begin
      FROM s
    ),
    seq AS (
      SELECT
        b.*,
        /* continuous point index within a session: +1 on every serve begin; if no serves exist, we still get 1 via max() later */
        CASE
          WHEN SUM(is_serve_begin) OVER (PARTITION BY b.session_id) = 0
            THEN 1
          ELSE 1 + SUM(is_serve_begin) OVER (PARTITION BY b.session_id ORDER BY b.ord_ts, b.swing_id ROWS UNBOUNDED PRECEDING)
        END AS point_index
      FROM base b
    ),
    shots AS (
      SELECT
        seq.*,
        ROW_NUMBER() OVER (PARTITION BY seq.session_id, seq.point_index ORDER BY seq.ord_ts, seq.swing_id) AS shot_number_in_point,
        MIN(seq.ord_ts) OVER (PARTITION BY seq.session_id, seq.point_index) AS point_ts0
      FROM seq
    ),
    rally_fallback AS (
      /* In case serves are absent for a session, align to dim_rally */
      SELECT
        sh.*,
        dr.rally_number,
        COALESCE(sh.point_index,
                 /* dense rally-based index per session */
                 DENSE_RANK() OVER (PARTITION BY sh.session_id ORDER BY dr.rally_number NULLS LAST, sh.point_ts0, sh.swing_id)
        ) AS point_number
      FROM shots sh
      LEFT JOIN dim_rally dr
        ON dr.session_id = sh.session_id
       AND sh.rally_id   = dr.rally_id
    )
    SELECT
      session_uid, session_id, swing_id, player_id, player_name, player_uid,
      rally_id, start_s, end_s, ball_hit_s, start_ts, end_ts, ball_hit_ts,
      ball_hit_x, ball_hit_y, ball_speed, ball_player_distance,
      is_in_rally, serve, serve_type, swing_type, meta,
      point_number,
      shot_number_in_point
    FROM rally_fallback;
    """,

    # ---------------- ENRICHED POINT ROWS (Power BI target) ----------------
    "vw_point_log": """
    /* One row per swing with tennis-aware point & shot numbers, plus safe derivations:
       - server/receiver per point
       - serving side (deuce/ad) from point index within game
       - point winner & error flags from the last shot in the point
       - game boundaries when server changes or a game is won by rules
       - score text (server-first) per point
       - serve bucket (1..8) and generic shot buckets (A..D) from bounce XY
    */
    CREATE OR REPLACE VIEW vw_point_log AS
    WITH po AS (
      SELECT * FROM vw_point_order_by_serve
    ),
    /* first-shot row per point is the serve */
    pt_first AS (
      SELECT DISTINCT ON (session_id, point_number)
        session_id, point_number, swing_id AS first_swing_id,
        player_id AS server_id, player_name AS server_name, player_uid AS server_uid,
        ball_hit_ts AS first_hit_ts, ball_hit_s AS first_hit_s
      FROM po
      ORDER BY session_id, point_number, shot_number_in_point
    ),
    /* last-shot row per point decides outcome */
    pt_last AS (
      SELECT DISTINCT ON (session_id, point_number)
        session_id, point_number, swing_id AS last_swing_id,
        player_id AS last_hitter_id, player_name AS last_hitter_name,
        ball_hit_ts AS last_hit_ts, ball_hit_s AS last_hit_s
      FROM po
      ORDER BY session_id, point_number, shot_number_in_point DESC
    ),
    /* bounce immediately after each swing */
    s_bounce AS (
      SELECT
        s.swing_id, s.session_id,
        b.bounce_id,
        b.bounce_ts, b.bounce_s,
        b.x AS bounce_x, b.y AS bounce_y,
        b.bounce_type AS bounce_type_raw
      FROM po s
      LEFT JOIN LATERAL (
        SELECT b.*
        FROM vw_bounce b
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
        /* receiver for the point = first opponent present in the point (fallback to "other id seen in session") */
    point_players AS (
      SELECT
        p.session_id,
        p.point_number,
        /* keep a check copy of server id from the first shot (serve) */
        MIN(CASE WHEN p.shot_number_in_point = 1 AND p.serve THEN p.player_id END) AS server_id_chk,
        /* receiver = any other player present after the serve */
        MIN(CASE
              WHEN p.shot_number_in_point > 1
               AND p.player_id <> pf.server_id
              THEN p.player_id
            END) AS receiver_id_guess
      FROM po p
      /* get server_id per (session,point) from pt_first (the serve row) */
      JOIN pt_first pf
        ON pf.session_id  = p.session_id
       AND pf.point_number = p.point_number
      GROUP BY p.session_id, p.point_number
    ),

    server_receiver AS (
      SELECT
        pf.session_id, pf.point_number,
        pf.server_id,
        COALESCE(pp.receiver_id_guess,
                 /* fallback: any other player id present in session */
                 (SELECT MIN(dp.player_id) FROM dim_player dp WHERE dp.session_id = pf.session_id AND dp.player_id <> pf.server_id)
        ) AS receiver_id
      FROM pt_first pf
      LEFT JOIN point_players pp
        ON pp.session_id = pf.session_id AND pp.point_number = pf.point_number
    ),
    names AS (
      SELECT
        sr.*,
        dp1.full_name AS server_name,
        dp1.sportai_player_uid AS server_uid,
        dp2.full_name AS receiver_name,
        dp2.sportai_player_uid AS receiver_uid
      FROM server_receiver sr
      LEFT JOIN dim_player dp1 ON dp1.session_id = sr.session_id AND dp1.player_id = sr.server_id
      LEFT JOIN dim_player dp2 ON dp2.session_id = sr.session_id AND dp2.player_id = sr.receiver_id
    ),
    /* outcome per point from last shot's bounce */
    point_outcome AS (
      SELECT
        pl.session_id, pl.point_number,
        pl.last_swing_id,
        sb.bounce_type_raw AS last_bounce_type,
        CASE
          WHEN sb.bounce_type_raw IN ('out','net','long','wide') THEN
            /* error by the last hitter -> opponent wins */
            CASE WHEN pl.last_hitter_id = n.server_id THEN n.receiver_id ELSE n.server_id END
          ELSE NULL
        END AS point_winner_id
      FROM pt_last pl
      LEFT JOIN s_bounce sb
             ON sb.session_id = pl.session_id AND sb.swing_id = pl.last_swing_id
      LEFT JOIN names n
             ON n.session_id = pl.session_id AND n.point_number = pl.point_number
    ),
    /* game numbering: a game starts when server changes from previous point or at first point */
    point_headers AS (
      SELECT
        n.*,
        po_first.first_hit_ts,
        LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number) AS prev_server_id,
        CASE WHEN LAG(n.server_id) OVER (PARTITION BY n.session_id ORDER BY n.point_number) IS DISTINCT FROM n.server_id THEN 1 ELSE 0 END AS new_game_flag
      FROM names n
      LEFT JOIN pt_first po_first
        ON po_first.session_id = n.session_id AND po_first.point_number = n.point_number
    ),
    game_seq AS (
      SELECT
        ph.*,
        1 + SUM(new_game_flag) OVER (PARTITION BY ph.session_id ORDER BY ph.point_number ROWS UNBOUNDED PRECEDING) AS game_number,
        ROW_NUMBER() OVER (PARTITION BY ph.session_id,
                                     (1 + SUM(new_game_flag) OVER (PARTITION BY ph.session_id ORDER BY ph.point_number ROWS UNBOUNDED PRECEDING))
                           ORDER BY ph.point_number) AS point_in_game
      FROM point_headers ph
    ),
    /* cumulative game score (server-first) */
    game_score AS (
      SELECT
        gs.session_id, gs.point_number, gs.game_number, gs.point_in_game,
        gs.server_id, gs.receiver_id, gs.server_name, gs.receiver_name, gs.server_uid, gs.receiver_uid, gs.first_hit_ts,
        po.point_winner_id,
        SUM(CASE WHEN po.point_winner_id = gs.server_id   THEN 1 ELSE 0 END)
           OVER (PARTITION BY gs.session_id, gs.game_number ORDER BY gs.point_number) AS server_points_in_game,
        SUM(CASE WHEN po.point_winner_id = gs.receiver_id THEN 1 ELSE 0 END)
           OVER (PARTITION BY gs.session_id, gs.game_number ORDER BY gs.point_number) AS receiver_points_in_game
      FROM game_seq gs
      LEFT JOIN point_outcome po
        ON po.session_id = gs.session_id AND po.point_number = gs.point_number
    ),
    score_text AS (
      SELECT
        g.*,
        /* textual score at that point, server-first, lawn-style */
        CASE
          WHEN GREATEST(COALESCE(server_points_in_game,0), COALESCE(receiver_points_in_game,0)) >= 4
               AND ABS(COALESCE(server_points_in_game,0) - COALESCE(receiver_points_in_game,0)) >= 2
            THEN 'game'
          WHEN COALESCE(server_points_in_game,0) >= 3 AND COALESCE(receiver_points_in_game,0) >= 3 THEN
            CASE
              WHEN server_points_in_game = receiver_points_in_game THEN '40-40'
              WHEN server_points_in_game  > receiver_points_in_game THEN 'Ad-40'
              ELSE '40-Ad'
            END
          ELSE
            concat(
              CASE COALESCE(server_points_in_game,0)
                WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END,
              '-',
              CASE COALESCE(receiver_points_in_game,0)
                WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END
            )
        END AS score_text
      FROM game_score g
    ),
    serve_bounce AS (
      /* bounce for the serve (first shot of the point) to bin 1..8 */
      SELECT
        pf.session_id, pf.point_number,
        sb.bounce_x, sb.bounce_y, sb.bounce_type_raw
      FROM pt_first pf
      LEFT JOIN s_bounce sb
        ON sb.session_id = pf.session_id AND sb.swing_id = pf.first_swing_id
    ),
    serve_bucket AS (
      SELECT
        sb.*,
        /* naive 1..8 grid: 4 buckets across (x), 2 along (y). You can refine the cutoffs once we lock the coordinate system. */
        CASE
          WHEN sb.bounce_x IS NULL OR sb.bounce_y IS NULL THEN NULL
          ELSE
            (CASE
               WHEN sb.bounce_y >= 0 THEN 1 ELSE 0
             END) * 4
            +
            (CASE
               WHEN sb.bounce_x < -0.5 THEN 1
               WHEN sb.bounce_x <  0.0 THEN 2
               WHEN sb.bounce_x <  0.5 THEN 3
               ELSE 4
             END)
        END AS serve_bucket_1_8
      FROM serve_bounce sb
    ),
    last_bounce AS (
      SELECT
        pl.session_id, pl.point_number,
        sb.bounce_x AS last_bounce_x, sb.bounce_y AS last_bounce_y, sb.bounce_type_raw AS last_bounce_type
      FROM pt_last pl
      LEFT JOIN s_bounce sb
        ON sb.session_id = pl.session_id AND sb.swing_id = pl.last_swing_id
    )
    SELECT
      po.session_uid,
      po.session_id,
      po.rally_id,            /* still exposed for diagnostic parity */
      po.point_number         AS point_number,
      po.shot_number_in_point AS shot_number,

      po.swing_id,
      po.player_id,           po.player_name,         po.player_uid,

      /* server/receiver context for the point */
      st.game_number,
      st.point_in_game,
      CASE WHEN MOD(st.point_in_game,2)=1 THEN 'deuce' ELSE 'ad' END AS serving_side,
      st.server_id,   st.server_name,   st.server_uid,
      st.receiver_id, st.receiver_name, st.receiver_uid,

      po.serve, po.serve_type,
      po.swing_type AS swing_type_raw,

      po.ball_speed,
      po.ball_player_distance,

      po.start_s, po.end_s, po.ball_hit_s,
      po.start_ts, po.end_ts, po.ball_hit_ts,

      /* bounce after this swing */
      sb.bounce_id,
      sb.bounce_x AS ball_bounce_x,
      sb.bounce_y AS ball_bounce_y,
      sb.bounce_type_raw,

      /* coarse shot buckets for any swing (A..D) via XY: deep/short × left/right */
      CASE
        WHEN sb.bounce_x IS NULL OR sb.bounce_y IS NULL THEN NULL
        WHEN sb.bounce_y >= 0 AND sb.bounce_x < 0 THEN 'A'  /* deep-left  */
        WHEN sb.bounce_y >= 0 AND sb.bounce_x >= 0 THEN 'B' /* deep-right */
        WHEN sb.bounce_y <  0 AND sb.bounce_x < 0 THEN 'C'  /* short-left */
        ELSE 'D'                                             /* short-right */
      END AS shot_loc_bucket_ad,

      /* point outcome at the point level */
      po2.point_winner_id,
      CASE
        WHEN po2.point_winner_id IS NULL THEN NULL
        WHEN po2.point_winner_id = st.server_id THEN st.server_name ELSE st.receiver_name
      END AS point_winner_name,
      lb.last_bounce_type AS point_end_bounce_type,
      /* boolean helpers */
      CASE WHEN lb.last_bounce_type IN ('out','net','long','wide') THEN TRUE ELSE FALSE END AS is_error,
      CASE
        WHEN lb.last_bounce_type IN ('out','net','long','wide') THEN lb.last_bounce_type
        ELSE NULL
      END AS error_type,

      /* game-score text at this point (server-first) */
      st.score_text AS score_server_first,

      /* serve placement bucket for the point (based on the actual serve bounce) */
      sv.serve_bucket_1_8

    FROM po
    LEFT JOIN s_bounce sb
           ON sb.session_id = po.session_id AND sb.swing_id = po.swing_id
    LEFT JOIN score_text st
           ON st.session_id = po.session_id AND st.point_number = po.point_number
    LEFT JOIN point_outcome po2
           ON po2.session_id = po.session_id AND po2.point_number = po.point_number
    LEFT JOIN last_bounce lb
           ON lb.session_id = po.session_id AND lb.point_number = po.point_number
    LEFT JOIN serve_bucket sv
           ON sv.session_id = po.session_id AND sv.point_number = po.point_number
    ORDER BY po.session_uid, po.point_number, po.shot_number_in_point, po.swing_id;
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

        # 1) Proactively drop legacy blockers (gold-era objects, old aliases)
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
