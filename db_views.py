# db_views.py — orchestrator + stable views (V1 only)
# Exposes: init_views/run_views(engine)

import os
from typing import List
from sqlalchemy import text
from views_point_variants import get_point_view_sql  # still used to emit the V1 SQL

__all__ = ["init_views", "run_views", "VIEW_SQL_STMTS", "VIEW_NAMES", "CREATE_STMTS"]
VIEW_SQL_STMTS: List[str] = []

# ==================================================================================
# Utilities
# ==================================================================================

def _table_exists(conn, t: str) -> bool:
    return conn.execute(text(r"""
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema='public' AND table_name=:t
        LIMIT 1
    """), {"t": t}).first() is not None

def _column_exists(conn, t: str, c: str) -> bool:
    return conn.execute(text(r"""
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
    kind = conn.execute(text(r"""
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

def _exec_with_clear_errors(conn, name: str, sql: str):
    try:
        conn.execute(text(sql))
    except Exception as err:
        dbmsg = ""
        if hasattr(err, "orig"):
            o = err.orig
            dbmsg = getattr(getattr(o, "diag", None), "message_primary", "") or str(o)
        lines = sql.strip().splitlines()
        snippet = "\n".join([*lines[:12], "    ...", *lines[-12:]]) if len(lines) > 30 else "\n".join(lines)
        raise RuntimeError(f"[init-views:{name}] {dbmsg or err.__class__.__name__}\n--- SQL snippet ---\n{snippet}\n")

# Auto-detect your player-side column and expose it as player_side_far_d
def _player_side_select_snippet(conn) -> str:
    for col in ["is_far_side", "is_far_side_d", "player_is_far", "is_far"]:
        if _column_exists(conn, "fact_swing", col):
            return f"fs.{col} AS player_side_far_d"
    for col in ["player_side", "player_side_d", "side", "court_side", "player_end"]:
        if _column_exists(conn, "fact_swing", col):
            return (
                "CASE lower(nullif(fs.{c},'')) "
                " WHEN 'far' THEN TRUE WHEN 'far_side' THEN TRUE WHEN 'far-end' THEN TRUE WHEN 'far end' THEN TRUE "
                " WHEN 'near' THEN FALSE WHEN 'near_side' THEN FALSE WHEN 'near-end' THEN FALSE WHEN 'near end' THEN FALSE "
                " ELSE NULL END AS player_side_far_d"
            ).format(c=col)
    return "NULL::boolean AS player_side_far_d"

# ---- Helper: A–D placement function (two overloads) ----
PLACEMENT_AD_FN_SQL_NUMERIC = r'''
CREATE OR REPLACE FUNCTION placement_ad(
  x_src          numeric,
  landing_is_far boolean,
  cw             numeric,
  eps            numeric
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
  WITH clamped AS (
    SELECT LEAST(
             GREATEST( CASE WHEN landing_is_far THEN x_src ELSE cw - x_src END,
                       0::numeric),
             cw - eps
           ) AS x_eff
  ),
  lane AS (
    SELECT (1 + FLOOR(x_eff / (cw/4.0)))::int AS lane_1_4
    FROM clamped
  )
  SELECT CASE lane_1_4
           WHEN 1 THEN 'A'
           WHEN 2 THEN 'B'
           WHEN 3 THEN 'C'
           WHEN 4 THEN 'D'
         END
  FROM lane;
$$;
'''

PLACEMENT_AD_FN_SQL_FLOAT8 = r'''
CREATE OR REPLACE FUNCTION placement_ad(
  x_src          double precision,
  landing_is_far boolean,
  cw             double precision,
  eps            double precision
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
  WITH clamped AS (
    SELECT LEAST(
             GREATEST( CASE WHEN landing_is_far THEN x_src ELSE cw - x_src END,
                       0::double precision),
             cw - eps
           ) AS x_eff
  ),
  lane AS (
    SELECT (1 + FLOOR(x_eff / (cw/4.0)))::int AS lane_1_4
    FROM clamped
  )
  SELECT CASE lane_1_4
           WHEN 1 THEN 'A'
           WHEN 2 THEN 'B'
           WHEN 3 THEN 'C'
           WHEN 4 THEN 'D'
         END
  FROM lane;
$$;
'''

# ==================================================================================
# View manifest (V1 only)
# ==================================================================================

VIEW_NAMES = [
    "vw_swing_silver",
    "vw_ball_position_silver",
    "vw_bounce_silver",
    "vw_point_silver_core",   # full-fidelity V1
    "vw_point_silver",        # dashboard-clean (drops unwanted cols)
    "vw_bounce_stream_debug",
    "vw_point_bounces_debug",
]

CREATE_STMTS = {
    # ------------------------------ SILVER: swing view ------------------------------
    "vw_swing_silver": r'''
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
          fs.serve, fs.serve_type AS serve_type, fs.swing_type, fs.is_in_rally,
          fs.ball_player_distance,
          fs.meta AS swing_meta_json,
          ds.session_uid AS session_uid_d,
          {PLAYER_SIDE_SELECT}
        FROM fact_swing fs
        LEFT JOIN dim_session ds USING (session_id);
    ''',

    "vw_ball_position_silver": r'''
        CREATE OR REPLACE VIEW vw_ball_position_silver AS
        SELECT session_id, ts_s, ts, x, y
        FROM fact_ball_position;
    ''',

    "vw_bounce_silver": r'''
        CREATE OR REPLACE VIEW vw_bounce_silver AS
        SELECT
          b.session_id,
          b.bounce_id,
          b.hitter_player_id AS bounce_hitter_id,
          b.rally_id,
          b.bounce_s,
          b.bounce_ts,
          b.x,
          b.y,
          b.bounce_type
        FROM fact_bounce b;
    ''',
}

# Inject the single V1 view as our "core". Rename any variant names to *_core just in case.
_base_sql_v1 = get_point_view_sql("v1")
_base_sql_v1 = (
    _base_sql_v1
      .replace("vw_point_silver_v1", "vw_point_silver_core")
      .replace("vw_point_silver_af", "vw_point_silver_core")
)
CREATE_STMTS["vw_point_silver_core"] = _base_sql_v1

# ------------------------------ DEBUG views (read from CORE) ----------------------
CREATE_STMTS["vw_bounce_stream_debug"] = r'''
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
      FROM vw_point_silver_core vps
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
'''

CREATE_STMTS["vw_point_bounces_debug"] = r'''
    CREATE OR REPLACE VIEW vw_point_bounces_debug AS
    SELECT
      vps.session_id,
      vps.session_uid_d,
      COUNT(*)                                                        AS swings_total,
      COUNT(*) FILTER (WHERE vps.valid_swing_d)                        AS swings_valid,
      COUNT(*) FILTER (WHERE vps.between_serves_d)                     AS swings_between_serves,
      COUNT(*) FILTER (WHERE vps.cluster_kill_d)                       AS cluster_kills,
      COUNT(*) FILTER (WHERE vps.alt_kill_d)                           AS alt_kills,

      COUNT(*) FILTER (WHERE vps.bounce_id IS NOT NULL)               AS swings_with_any_bounce,
      COUNT(*) FILTER (WHERE vps.bounce_type_raw = 'floor')           AS swings_with_floor_primary,
      COUNT(*) FILTER (WHERE vps.bounce_type_raw <> 'floor' AND vps.bounce_id IS NOT NULL)
                                                                      AS swings_with_racquet_primary,
      COUNT(*) FILTER (WHERE vps.bounce_id IS NULL)                   AS swings_with_no_bounce,
      COALESCE((SELECT COUNT(*) FROM (
          SELECT bounce_id
          FROM vw_point_silver_core v2
          WHERE v2.session_id = vps.session_id AND v2.bounce_id IS NOT NULL
          GROUP BY bounce_id
          HAVING COUNT(*) > 1
        ) d),0)                                                       AS dup_bounce_ids
    FROM vw_point_silver_core vps
    GROUP BY vps.session_id, vps.session_uid_d;
'''

# ==================================================================================
# Apply
# ==================================================================================

def _build_point_clean_view_sql(conn) -> str:
    drop_cols = {
        "swing_id", "start_ts", "end_ts", "ball_hit_ts",
        "bounce_id", "bounce_ts_d", "primary_source"
    }

    rows = conn.execute(text(r"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='vw_point_silver_core'
        ORDER BY ordinal_position
    """)).fetchall()
    cols = [r[0] for r in rows]
    keep = [c for c in cols if c not in drop_cols]
    if not keep:
        raise RuntimeError("vw_point_silver_core has no columns after exclusion filter.")

    select_items = [f'base."{c}" AS "{c}"' for c in keep]

    has_task_id = any(c.lower() == "task_id" for c in cols)
    if not has_task_id:
        select_items.append(
            "COALESCE("
            " NULLIF(base.\"task_id\"::text,''),"
            " NULLIF(base.\"sportai_task_id\"::text,''),"
            " NULLIF(base.\"job_id\"::text,''),"
            " NULLIF((row_to_json(base)::jsonb ->> 'task_id'),''),"
            " NULLIF((row_to_json(base)::jsonb ->> 'sportai_task_id'),''),"
            " NULLIF((row_to_json(base)::jsonb ->> 'job_id'),'')"
            ")::text AS task_id"
        )

    select_list = ",\n          ".join(select_items)
    return f'''
        CREATE OR REPLACE VIEW public.vw_point_silver AS
        SELECT
          {select_list}
        FROM public.vw_point_silver_core AS base;
    '''


def _apply_views(engine):
    global VIEW_SQL_STMTS
    with engine.begin() as conn:
        _preflight_or_raise(conn)

        # helper functions for placement
        conn.execute(text(PLACEMENT_AD_FN_SQL_NUMERIC))
        conn.execute(text(PLACEMENT_AD_FN_SQL_FLOAT8))

        # drop then recreate in clean order
        for name in reversed(VIEW_NAMES):
            _drop_any(conn, name)

        VIEW_SQL_STMTS = []
        for name in VIEW_NAMES:
            if name == "vw_point_silver":
                sql = _build_point_clean_view_sql(conn)
            else:
                sql = CREATE_STMTS[name]
                if name == "vw_swing_silver":
                    sql = sql.replace("{PLAYER_SIDE_SELECT}", _player_side_select_snippet(conn))
            VIEW_SQL_STMTS.append(sql)
            _exec_with_clear_errors(conn, name, sql)

# Back-compat names
init_views = _apply_views
run_views  = _apply_views
