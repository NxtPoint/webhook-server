# db_views.py — Silver passthrough + serve-aware point ordering & enriched point rows
# Rules implemented:
# - Point #1 always belongs to Game #1 (point numbers normalized to start at 1)
# - Serve := any swing where swing_type ILIKE '%overhead%'  (fh_overhead and variants)
# - Error := ball_speed <= 0  (NULL treated as 0)
# - Second serve: consecutive overheads inside a point; if two serve-faults => double fault (receiver wins, next point)
# - Game changes when SERVER CHANGES between points (by server_uid when present, else server_id)
# - Serving side: odd point_in_game = 'deuce', even = 'ad'

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
    "vw_swing",
    "vw_bounce",
    "vw_ball_position",
    "vw_player_position",

    # Order and tennis derivations
    "vw_point_order_by_serve",  # one row per swing with point/shot numbers
    "vw_point_log",             # enriched rows for Power BI
]

# Legacy objects we want to drop to avoid WrongObjectType/500s
LEGACY_OBJECTS = [
    "vw_point_shot_log_gold",
    "vw_shot_order_gold",
    "vw_point_summary",
    "point_log_tbl",
    "point_summary_tbl",
    "vw_point_shot_log",
]

def _drop_any(conn, name: str):
    """Drop any object named `name` in public schema, using the correct DROP."""
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

    # PURE bounce passthrough (we keep raw XY as-is)
    "vw_bounce": """
    CREATE OR REPLACE VIEW vw_bounce AS
    SELECT
      ds.session_uid,
      b.session_id,
      b.bounce_id,
      b.hitter_player_id,
      dp.full_name          AS hitter_name,
      dp.sportai_player_uid AS hitter_player_uid,
      b.rally_id,
      b.bounce_s,
      b.bounce_ts,
      b.x AS x,
      b.y AS y,
      b.bounce_type
    FROM fact_bounce b
    LEFT JOIN dim_session ds ON ds.session_id = b.session_id
    LEFT JOIN dim_player  dp ON dp.session_id = b.session_id AND dp.player_id = b.hitter_player_id;
""",

    # ---------------- ORDERING (serve-driven points incl. second-serve & double-fault logic) ----------------
    "vw_point_order_by_serve": """
    /* One row per swing with point/shot numbers.
       Serve := swing_type ILIKE '%overhead%'.
       A point begins on a serve if:
         (a) previous swing is not a serve  -> first serve of point
         (b) OR the previous TWO swings were serve faults (double fault ended prior point)
       Second serve (after a single fault) does NOT start a new point.
       Point numbers are normalized to start at 1 per session.
    */
    CREATE OR REPLACE VIEW vw_point_order_by_serve AS
      WITH s AS (
        SELECT
          v.*,
          COALESCE(v.ball_hit_ts, v.start_ts,
                   (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')) AS ord_ts
        FROM vw_swing v
      ),
      flags AS (
        SELECT
          s.*,
          (s.swing_type ILIKE '%overhead%') AS is_overhead_serve,
          (COALESCE(s.ball_speed,0) = 0)    AS is_error_shot,
          LAG(s.swing_type ILIKE '%overhead%') OVER (PARTITION BY s.session_id ORDER BY s.ord_ts, s.swing_id)    AS prev_is_serve,
          LAG(COALESCE(s.ball_speed,0) = 0)  OVER (PARTITION BY s.session_id ORDER BY s.ord_ts, s.swing_id)     AS prev_is_error,
          LAG(s.swing_type ILIKE '%overhead%', 2) OVER (PARTITION BY s.session_id ORDER BY s.ord_ts, s.swing_id) AS prev2_is_serve,
          LAG(COALESCE(s.ball_speed,0) = 0,   2) OVER (PARTITION BY s.session_id ORDER BY s.ord_ts, s.swing_id) AS prev2_is_error
        FROM s
      ),
      base AS (
        SELECT
          f.*,
          CASE
            WHEN f.is_overhead_serve AND (
                   COALESCE(f.prev_is_serve, FALSE) = FALSE
                OR (COALESCE(f.prev_is_serve, FALSE)  = TRUE  AND COALESCE(f.prev_is_error, FALSE)  = TRUE
                 AND COALESCE(f.prev2_is_serve, FALSE) = TRUE AND COALESCE(f.prev2_is_error, FALSE) = TRUE)
              )
            THEN 1 ELSE 0
          END AS is_point_begin
        FROM flags f
      ),
      seq AS (
        SELECT
          b.*,
          1 + SUM(is_point_begin) OVER (PARTITION BY b.session_id ORDER BY b.ord_ts, b.swing_id ROWS UNBOUNDED PRECEDING) AS point_index
        FROM base b
      ),
      shots AS (
        SELECT
          seq.*,
          ROW_NUMBER() OVER (PARTITION BY seq.session_id, seq.point_index ORDER BY seq.ord_ts, seq.swing_id) AS shot_number_in_point,
          MIN(seq.ord_ts) OVER (PARTITION BY seq.session_id, seq.point_index)                                AS point_ts0
        FROM seq
      ),
      rally_fallback AS (
        SELECT
          sh.*,
          dr.rally_number,
          COALESCE(sh.point_index,
                   DENSE_RANK() OVER (PARTITION BY sh.session_id ORDER BY dr.rally_number NULLS LAST, sh.point_ts0, sh.swing_id)
          ) AS point_index_fallback
        FROM shots sh
        LEFT JOIN dim_rally dr
               ON dr.session_id = sh.session_id AND sh.rally_id = dr.rally_id
      ),
      normalized AS (
        SELECT
          rf.*,
          (
            rf.point_index_fallback
            - MIN(rf.point_index_fallback) OVER (PARTITION BY rf.session_id)
            + 1
          ) AS point_number
        FROM rally_fallback rf
      )
      SELECT
        session_uid, session_id, swing_id, player_id, player_name, player_uid,
        rally_id, start_s, end_s, ball_hit_s, start_ts, end_ts, ball_hit_ts,
        ball_hit_x, ball_hit_y, ball_speed, ball_player_distance,
        is_in_rally, serve, serve_type, swing_type, meta,
        /* carry helpful flags forward */
        is_overhead_serve,
        is_error_shot,
        point_number,
        shot_number_in_point
      FROM normalized;
    """,

    # ---------------- ENRICHED POINT ROWS (Power BI target) ----------------
    "vw_point_log": """
    /* One row per swing with tennis-aware point & shot numbers and clear derived fields:
       - server/receiver per point (from first derived-serve in point)
       - game boundaries when server changes (by server_uid when present)
       - serving side: odd point_in_game = deuce, even = ad
       - error from ball_speed (<=0)
       - double fault if >=2 serve-faults (overhead AND error) in the point
    */
    CREATE OR REPLACE VIEW vw_point_log AS
    WITH po AS (
      SELECT * FROM vw_point_order_by_serve
    ),

    /* ===== DERIVED FIELDS (BEGIN) ===== */
    d0 AS (
      SELECT
        po.*,
        (po.is_overhead_serve)              AS serve_derived,
        (COALESCE(po.ball_speed,0) <= 0)    AS is_error_bs
      FROM po
    ),
    -- Count serves (overheads) within each point to mark 1st/2nd serve
    d AS (
      SELECT
        d0.*,
        SUM(CASE WHEN d0.serve_derived THEN 1 ELSE 0 END)
          OVER (PARTITION BY d0.session_id, d0.point_number ORDER BY d0.shot_number_in_point
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS serve_try_num,
        CASE WHEN d0.serve_derived THEN TRUE ELSE FALSE END AS is_serve_row,
        CASE WHEN d0.serve_derived AND d0.is_error_bs THEN 1 ELSE 0 END AS is_fault_serve
      FROM d0
    ),
    -- first derived-serve row per point defines server
    pt_first AS (
      SELECT DISTINCT ON (session_id, point_number)
        session_id, point_number,
        swing_id AS first_swing_id,
        player_id AS server_id
      FROM d
      WHERE is_serve_row
      ORDER BY session_id, point_number, shot_number_in_point
    ),
    -- last swing in the point (for outcome if not double fault)
    pt_last AS (
      SELECT DISTINCT ON (session_id, point_number)
        session_id, point_number,
        swing_id AS last_swing_id,
        player_id AS last_hitter_id,
        is_error_bs AS last_is_error
      FROM d
      ORDER BY session_id, point_number, shot_number_in_point DESC
    ),
    -- receiver: first opponent present in the same point (fallback to "other player in session")
    receiver_pick AS (
      SELECT
        f.session_id, f.point_number,
        MIN(x.player_id) FILTER (WHERE x.player_id IS NOT NULL AND x.player_id <> f.server_id) AS receiver_id
      FROM pt_first f
      LEFT JOIN d x
        ON x.session_id = f.session_id
       AND x.point_number = f.point_number
       AND x.shot_number_in_point > 1
      GROUP BY f.session_id, f.point_number
    ),
    names AS (
      SELECT
        f.session_id, f.point_number, f.server_id,
        COALESCE(r.receiver_id,
          (SELECT MIN(dp.player_id) FROM dim_player dp WHERE dp.session_id = f.session_id AND dp.player_id <> f.server_id)
        ) AS receiver_id
      FROM pt_first f
      LEFT JOIN receiver_pick r ON r.session_id = f.session_id AND r.point_number = f.point_number
    ),
    -- attach names/uids for server/receiver
    names_labeled AS (
      SELECT
        n.*,
        dp1.full_name AS server_name,
        dp1.sportai_player_uid AS server_uid,
        dp2.full_name AS receiver_name,
        dp2.sportai_player_uid AS receiver_uid
      FROM names n
      LEFT JOIN dim_player dp1 ON dp1.session_id = n.session_id AND dp1.player_id = n.server_id
      LEFT JOIN dim_player dp2 ON dp2.session_id = n.session_id AND dp2.player_id = n.receiver_id
    ),
    faults AS (
      SELECT session_id, point_number, SUM(is_fault_serve) AS n_faults
      FROM d
      GROUP BY session_id, point_number
    ),
    point_outcome AS (
      SELECT
        pl.session_id, pl.point_number,
        pl.last_swing_id, pl.last_is_error,
        nl.server_id, nl.receiver_id,
        (COALESCE(f.n_faults,0) >= 2) AS is_double_fault,
        CASE
          WHEN COALESCE(f.n_faults,0) >= 2 THEN nl.receiver_id
          WHEN pl.last_is_error         THEN CASE WHEN pl.last_hitter_id = nl.server_id THEN nl.receiver_id ELSE nl.server_id END
          ELSE NULL
        END AS point_winner_id
      FROM pt_last pl
      LEFT JOIN names_labeled nl ON nl.session_id = pl.session_id AND nl.point_number = pl.point_number
      LEFT JOIN faults f        ON f.session_id  = pl.session_id AND f.point_number  = pl.point_number
    ),
    -- game changes when SERVER CHANGES between points (prefer server_uid for stability)
    point_headers AS (
      SELECT
        nl.*,
        LAG(nl.server_uid) OVER (PARTITION BY nl.session_id ORDER BY nl.point_number) AS prev_server_uid,
        CASE WHEN LAG(nl.server_uid) OVER (PARTITION BY nl.session_id ORDER BY nl.point_number) IS DISTINCT FROM nl.server_uid
             THEN 1 ELSE 0 END AS new_game_flag
      FROM names_labeled nl
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
    score_tally AS (
      SELECT
        gs.session_id, gs.point_number, gs.game_number, gs.point_in_game,
        gs.server_id, gs.receiver_id,
        po.point_winner_id,
        SUM(CASE WHEN po.point_winner_id = gs.server_id   THEN 1 ELSE 0 END)
          OVER (PARTITION BY gs.session_id, gs.game_number ORDER BY gs.point_number) AS server_points_in_game,
        SUM(CASE WHEN po.point_winner_id = gs.receiver_id THEN 1 ELSE 0 END)
          OVER (PARTITION BY gs.session_id, gs.game_number ORDER BY gs.point_number) AS receiver_points_in_game
      FROM game_seq gs
      LEFT JOIN point_outcome po ON po.session_id = gs.session_id AND po.point_number = gs.point_number
    ),
    score_text AS (
      SELECT
        st.*,
        CASE
          WHEN GREATEST(COALESCE(server_points_in_game,0), COALESCE(receiver_points_in_game,0)) >= 4
               AND ABS(COALESCE(server_points_in_game,0) - COALESCE(receiver_points_in_game,0)) >= 2
            THEN 'game'
          WHEN COALESCE(server_points_in_game,0) >= 3 AND COALESCE(receiver_points_in_game,0) >= 3 THEN
            CASE WHEN server_points_in_game = receiver_points_in_game THEN '40-40'
                 WHEN server_points_in_game  > receiver_points_in_game THEN 'Ad-40'
                 ELSE '40-Ad' END
          ELSE
            concat(
              CASE COALESCE(server_points_in_game,0) WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END,
              '-',
              CASE COALESCE(receiver_points_in_game,0) WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END
            )
        END AS score_server_first
      FROM score_tally st
    ),
    -- nearest bounce after each swing (optional diagnostics)
    s_bounce AS (
      SELECT
        s.swing_id, s.session_id,
        b.bounce_id,
        b.bounce_ts, b.bounce_s,
        b.x AS bounce_x, b.y AS bounce_y,
        b.bounce_type AS bounce_type_raw
      FROM d s
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
    last_bounce AS (
      SELECT
        pl.session_id, pl.point_number,
        sb.bounce_x AS last_bounce_x, sb.bounce_y AS last_bounce_y, sb.bounce_type_raw AS last_bounce_type
      FROM pt_last pl
      LEFT JOIN s_bounce sb
        ON sb.session_id = pl.session_id AND sb.swing_id = pl.last_swing_id
    ),
    serve_bounce AS (
      SELECT
        pf.session_id, pf.point_number,
        sb.bounce_x, sb.bounce_y
      FROM pt_first pf
      LEFT JOIN s_bounce sb
        ON sb.session_id = pf.session_id AND sb.swing_id = pf.first_swing_id
    ),
    serve_bucket AS (
      SELECT
        sb.*,
        CASE
          WHEN sb.bounce_x IS NULL OR sb.bounce_y IS NULL THEN NULL
          ELSE (CASE WHEN sb.bounce_y >= 0 THEN 1 ELSE 0 END) * 4
             + (CASE WHEN sb.bounce_x < -0.5 THEN 1
                     WHEN sb.bounce_x <  0.0 THEN 2
                     WHEN sb.bounce_x <  0.5 THEN 3
                     ELSE 4 END)
        END AS serve_bucket_1_8
      FROM serve_bounce sb
    )
    /* ===== DERIVED FIELDS (END) ===== */

    SELECT
      d.session_uid,
      d.session_id,
      d.rally_id,
      d.point_number         AS point_number,
      d.shot_number_in_point AS shot_number,

      d.swing_id,
      d.player_id,           d.player_name,         d.player_uid,

      -- context per point
      st.game_number,
      st.point_in_game,
      CASE WHEN (st.point_in_game % 2) = 1 THEN 'deuce' ELSE 'ad' END AS serving_side,
      st.server_id, st.server_name, st.server_uid,
      st.receiver_id, st.receiver_name, st.receiver_uid,

      -- RAW pass-through (silver)
      d.serve        AS serve_raw,
      d.swing_type   AS swing_type_raw,
      d.ball_speed,
      d.ball_player_distance,
      d.start_s, d.end_s, d.ball_hit_s,
      d.start_ts, d.end_ts, d.ball_hit_ts,

      -- ===== DERIVED FIELDS (single block to tweak later) =====
      d.serve_derived,
      CASE WHEN d.serve_derived THEN d.serve_try_num ELSE NULL END AS serve_try,  -- 1,2,...
      d.is_error_bs,
      po.is_double_fault,
      -- point outcome (winner id by derived rules)
      po.point_winner_id,
      st.score_server_first,
      -- bounce diagnostics (optional)
      lb.last_bounce_type AS point_end_bounce_type,
      CASE WHEN lb.last_bounce_type IN ('out','net','long','wide') THEN TRUE ELSE FALSE END AS is_error_bounce,
      sv.serve_bucket_1_8

    FROM d
    LEFT JOIN names_labeled nl
           ON nl.session_id = d.session_id AND nl.point_number = d.point_number
    LEFT JOIN point_outcome po
           ON po.session_id = d.session_id AND po.point_number = d.point_number
    LEFT JOIN score_text st
           ON st.session_id = d.session_id AND st.point_number = d.point_number
    LEFT JOIN last_bounce lb
           ON lb.session_id = d.session_id AND lb.point_number = d.point_number
    LEFT JOIN serve_bucket sv
           ON sv.session_id = d.session_id AND sv.point_number = d.point_number
    ORDER BY d.session_uid, d.point_number, d.shot_number_in_point, d.swing_id;
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
