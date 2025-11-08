# build_silver_point_detail.py — Phase 1.2 (Bronze-only, task_id-only, flattened)
# - No business logic. Only field selection/casting/flattening from Bronze.
# - Singles baseline: drop intercepting_player_id; drop annotations.
# - Flatten ball_hit_location -> ball_hit_x, ball_hit_y.
# - Flatten rally JSON -> rally (int) when possible.
# - Filter valid = true. PK = (task_id, swing_id).

from typing import Optional, List, Dict, Tuple
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"
PK = "(task_id, swing_id)"

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

DDL_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SILVER_SCHEMA}.{TABLE} (
  -- Identity
  task_id                   UUID               NOT NULL,
  swing_id                  BIGINT             NOT NULL,

  -- Verbatim/normalized from Bronze (no logic)
  player_id                 TEXT,
  start_ts                  TIMESTAMPTZ,
  start_frame               INTEGER,
  end_ts                    TIMESTAMPTZ,
  end_frame                 INTEGER,
  valid                     BOOLEAN,
  serve                     BOOLEAN,
  swing_type                TEXT,
  volley                    BOOLEAN,
  is_in_rally               BOOLEAN,
  confidence_swing_type     DOUBLE PRECISION,
  confidence                DOUBLE PRECISION,
  confidence_volley         DOUBLE PRECISION,
  ball_player_distance      DOUBLE PRECISION,
  ball_speed                DOUBLE PRECISION,
  ball_impact_type          TEXT,

  -- Rally flattened (int if possible)
  rally                     INTEGER,

  -- Hit instant & positions
  ball_hit                  TIMESTAMPTZ,
  ball_hit_x                DOUBLE PRECISION,
  ball_hit_y                DOUBLE PRECISION,

  -- Optional JSONs kept for future (no logic)
  ball_impact_location      JSONB,
  ball_trajectory           JSONB,

  -- Raw seconds (if present in Bronze)
  start                     DOUBLE PRECISION,
  "end"                     DOUBLE PRECISION,

  -- Phase 2 placeholders (remain NULL here)
  bounce_id                 BIGINT,
  bounce_ts                 TIMESTAMPTZ,
  bounce_s                  DOUBLE PRECISION,
  bounce_type               TEXT,
  court_x                   DOUBLE PRECISION,
  court_y                   DOUBLE PRECISION,

  server_id                 TEXT,
  serving_side              TEXT,
  shot_ix                   INTEGER,
  point_number              INTEGER,
  game_number               INTEGER,
  point_in_game             INTEGER,

  created_at                TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY {PK}
);
"""

DDL_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_task        ON {SILVER_SCHEMA}.{TABLE} (task_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_player      ON {SILVER_SCHEMA}.{TABLE} (task_id, player_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_time_start  ON {SILVER_SCHEMA}.{TABLE} (task_id, start_ts);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_time_hit    ON {SILVER_SCHEMA}.{TABLE} (task_id, ball_hit);",
]

def _exec(conn: Connection, sql: str, params: Optional[dict] = None) -> None:
    conn.execute(text(sql), params or {})

def _table_exists(conn: Connection, schema: str, name: str) -> bool:
    return bool(conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema=:s AND table_name=:t
    """), {"s": schema, "t": name}).fetchone())

def _columns_types(conn: Connection, schema: str, name: str) -> Dict[str, str]:
    rows = conn.execute(text("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema=:s AND table_name=:t
    """), {"s": schema, "t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

def _bronze_source_table(conn: Connection) -> Tuple[str, str, Dict[str, str]]:
    if _table_exists(conn, "bronze", "player_swing"):
        cols = _columns_types(conn, "bronze", "player_swing")
        return ("bronze", "player_swing", cols)
    if _table_exists(conn, "bronze", "swing"):
        cols = _columns_types(conn, "bronze", "swing")
        return ("bronze", "swing", cols)
    raise RuntimeError("Neither bronze.player_swing nor bronze.swing exists")

def _colref(name: str) -> str:
    n = name.lower()
    return 's."end"' if n == "end" else f"s.{n}"

def _ts_expr(cols: Dict[str, str], col_ts: str, fb_seconds: str) -> str:
    c = col_ts.lower(); fb = fb_seconds.lower()
    if c in cols:
        dt = cols[c]
        if "timestamp" in dt:
            return _colref(c)
        if any(k in dt for k in ("double","real","numeric","integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(c)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(
              CASE WHEN jsonb_typeof({_colref(c)})='number'
                   THEN (TIMESTAMP 'epoch' + ({_colref(c)}::text)::double precision * INTERVAL '1 second')
                   ELSE NULL::timestamptz END)"""
    if fb in cols:
        dt = cols[fb]
        if any(k in dt for k in ("double","real","numeric","integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(fb)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(
              CASE WHEN jsonb_typeof({_colref(fb)})='number'
                   THEN (TIMESTAMP 'epoch' + ({_colref(fb)}::text)::double precision * INTERVAL '1 second')
                   ELSE NULL::timestamptz END)"""
    return "NULL::timestamptz"

def _jsonb_expr(cols: Dict[str, str], name: str) -> str:
    return f"{_colref(name)}::jsonb" if name.lower() in cols else "NULL::jsonb"

def _num_expr(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(
          CASE WHEN jsonb_typeof({_colref(n)})='number'
               THEN ({_colref(n)}::text)::double precision
               ELSE NULL::double precision END)"""
    return _colref(n)

def _int_expr(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::int"
    dt = cols[n]
    if "json" in dt:
        return f"""(
          CASE WHEN jsonb_typeof({_colref(n)})='number'
               THEN ({_colref(n)}::text)::int
               ELSE
                 CASE
                   WHEN jsonb_typeof({_colref(n)})='object'
                        AND ({_colref(n)} ? 'index')
                        AND jsonb_typeof({_colref(n)}->'index')='number'
                   THEN ({_colref(n)}->>'index')::int
                   ELSE NULL::int
                 END
          END)"""
    return _colref(n)

def _bool_expr(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::boolean"

def _text_expr(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::text"

def ensure_schema_and_table(conn: Connection) -> None:
    _exec(conn, DDL_CREATE_SCHEMA)
    # Recreate if any legacy/dropped columns present
    if _table_exists(conn, SILVER_SCHEMA, TABLE):
        existing = set(_columns_types(conn, SILVER_SCHEMA, TABLE).keys())
        must_have = {"task_id","swing_id","ball_hit_x","ball_hit_y","rally"}
        dropped = {"intercepting_player_id","annotations","session_id","session_uid","rally_json","ball_hit_location"}
        if not must_have.issubset(existing) or (existing & dropped):
            _exec(conn, f"DROP TABLE {SILVER_SCHEMA}.{TABLE} CASCADE;")
    _exec(conn, DDL_CREATE_TABLE)
    for ddl in DDL_INDEXES:
        _exec(conn, ddl)

def insert_base(conn: Connection, task_id: str) -> int:
    schema, name, cols = _bronze_source_table(conn)
    source_ref = f"{schema}.{name} s"

    swing_id_expr = "s.id" if "id" in cols else "NULL::bigint"
    start_ts_expr = _ts_expr(cols, "start_ts", "start")
    end_ts_expr   = _ts_expr(cols, "end_ts",   "end")
    ball_hit_expr = _ts_expr(cols, "ball_hit", "ball_hit_s")

    # rally flattened to int
    rally_expr = _int_expr(cols, "rally")

    # ball_hit_x/y from native columns or from ball_hit_location JSON array
    if "ball_hit_x" in cols and not "json" in cols["ball_hit_x"]:
        bhx_expr = _num_expr(cols, "ball_hit_x")
    else:
        # Try from ball_hit_location JSON array [x,y]
        bhx_expr = """(
          CASE WHEN s.ball_hit_location IS NOT NULL
                AND jsonb_typeof(s.ball_hit_location::jsonb)='array'
               THEN (s.ball_hit_location::jsonb->>0)::double precision
               ELSE NULL::double precision
          END)"""

    if "ball_hit_y" in cols and not "json" in cols["ball_hit_y"]:
        bhy_expr = _num_expr(cols, "ball_hit_y")
    else:
        bhy_expr = """(
          CASE WHEN s.ball_hit_location IS NOT NULL
                AND jsonb_typeof(s.ball_hit_location::jsonb)='array'
               THEN (s.ball_hit_location::jsonb->>1)::double precision
               ELSE NULL::double precision
          END)"""

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id,
      player_id, start_ts, start_frame, end_ts, end_frame,
      valid, serve, swing_type, volley, is_in_rally,
      confidence_swing_type, confidence, confidence_volley,
      ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit, ball_hit_x, ball_hit_y,
      ball_impact_location, ball_trajectory,
      start, "end",
      bounce_id, bounce_ts, bounce_s, bounce_type, court_x, court_y,
      server_id, serving_side, shot_ix, point_number, game_number, point_in_game
    )
    SELECT
      :task_id,
      {swing_id_expr},

      {_text_expr(cols, "player_id")},
      {start_ts_expr},
      {_int_expr(cols, "start_frame")},
      {end_ts_expr},
      {_int_expr(cols, "end_frame")},
      {_bool_expr(cols, "valid")},
      {_bool_expr(cols, "serve")},
      {_text_expr(cols, "swing_type")},
      {_bool_expr(cols, "volley")},
      {_bool_expr(cols, "is_in_rally")},
      {_num_expr(cols, "confidence_swing_type")},
      {_num_expr(cols, "confidence")},
      {_num_expr(cols, "confidence_volley")},
      {_num_expr(cols, "ball_player_distance")},
      {_num_expr(cols, "ball_speed")},
      {_text_expr(cols, "ball_impact_type")},
      {rally_expr},
      {ball_hit_expr},
      {bhx_expr},
      {bhy_expr},
      {_jsonb_expr(cols, "ball_impact_location")},
      {_jsonb_expr(cols, "ball_trajectory")},
      {_num_expr(cols, "start")},
      {_num_expr(cols, "end")},

      NULL::bigint, NULL::timestamptz, NULL::double precision, NULL::text,
      NULL::double precision, NULL::double precision,

      NULL::text, NULL::text, NULL::int, NULL::int, NULL::int, NULL::int
    FROM {source_ref}
    WHERE s.task_id = :task_id
      AND COALESCE(s.valid, TRUE) IS TRUE
    ON CONFLICT {PK} DO UPDATE SET
      player_id                 = EXCLUDED.player_id,
      start_ts                  = EXCLUDED.start_ts,
      start_frame               = EXCLUDED.start_frame,
      end_ts                    = EXCLUDED.end_ts,
      end_frame                 = EXCLUDED.end_frame,
      valid                     = EXCLUDED.valid,
      serve                     = EXCLUDED.serve,
      swing_type                = EXCLUDED.swing_type,
      volley                    = EXCLUDED.volley,
      is_in_rally               = EXCLUDED.is_in_rally,
      confidence_swing_type     = EXCLUDED.confidence_swing_type,
      confidence                = EXCLUDED.confidence,
      confidence_volley         = EXCLUDED.confidence_volley,
      ball_player_distance      = EXCLUDED.ball_player_distance,
      ball_speed                = EXCLUDED.ball_speed,
      ball_impact_type          = EXCLUDED.ball_impact_type,
      rally                     = EXCLUDED.rally,
      ball_hit                  = EXCLUDED.ball_hit,
      ball_hit_x                = EXCLUDED.ball_hit_x,
      ball_hit_y                = EXCLUDED.ball_hit_y,
      ball_impact_location      = EXCLUDED.ball_impact_location,
      ball_trajectory           = EXCLUDED.ball_trajectory,
      start                     = EXCLUDED.start,
      "end"                     = EXCLUDED."end",
      bounce_id                 = EXCLUDED.bounce_id,
      bounce_ts                 = EXCLUDED.bounce_ts,
      bounce_s                  = EXCLUDED.bounce_s,
      bounce_type               = EXCLUDED.bounce_type,
      court_x                   = EXCLUDED.court_x,
      court_y                   = EXCLUDED.court_y,
      server_id                 = EXCLUDED.server_id,
      serving_side              = EXCLUDED.serving_side,
      shot_ix                   = EXCLUDED.shot_ix,
      point_number              = EXCLUDED.point_number,
      game_number               = EXCLUDED.game_number,
      point_in_game             = EXCLUDED.point_in_game;
    """
    res = conn.execute(text(sql), {"task_id": task_id})
    return res.rowcount if res.rowcount is not None else 0

def delete_for_task(conn: Connection, task_id: str) -> None:
    _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid;", {"tid": task_id})

def ensure_schema_and_table(conn: Connection) -> None:
    _exec(conn, DDL_CREATE_SCHEMA)
    if _table_exists(conn, SILVER_SCHEMA, TABLE):
        cols = set(_columns_types(conn, SILVER_SCHEMA, TABLE).keys())
        legacy = {"intercepting_player_id","annotations","session_id","session_uid","rally_json","ball_hit_location"}
        if ("task_id" not in cols or "swing_id" not in cols) or (cols & legacy):
            _exec(conn, f"DROP TABLE {SILVER_SCHEMA}.{TABLE} CASCADE;")
    _exec(conn, DDL_CREATE_TABLE)
    for ddl in DDL_INDEXES:
        _exec(conn, ddl)

def build_point_detail(task_id: str, replace: bool=False) -> dict:
    if not task_id:
        raise ValueError("task_id is required")
    with engine.begin() as conn:
        ensure_schema_and_table(conn)
        if replace:
            delete_for_task(conn, task_id)
        affected = insert_base(conn, task_id)
    return {"ok": True, "task_id": task_id, "replaced": replace, "rows_written": affected}

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Build silver.point_detail (Phase 1.2 — Bronze-only flatten, task_id-only)")
    p.add_argument("--task-id", required=True)
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()
    out = build_point_detail(task_id=args.task_id, replace=args.replace)
    print(out)

#=========================================================================================================================================================
# build_silver_phase2.py
# Phase 2.1 — Derived fields into silver.point_detail (task_id-only)
# Computes: serve_d, serving_side, server_id, point_number, game_number,
# point_in_game, shot_ix, serve_try_ix_in_point, first_rally_shot_ix, play_d.
# NOTE: Requires Phase 1.2 rows already present for the task_id.

from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine  # same engine you use for Phase 1.2

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# Court constants (meters). We keep these here so they’re easy to tune later.
COURT_W = 8.23
COURT_L = 23.77
HALF_W  = COURT_W / 2.0
MID_Y   = COURT_L / 2.0
SRV_BOX = 6.40
SERVE_EPS_M = 0.50  # near baseline tolerance for serve band

# Ensure derived columns exist on silver.point_detail
ALTERS_PHASE_21 = [
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS serve_d boolean;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS serving_side text;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS server_id text;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS point_number integer;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS game_number integer;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS point_in_game integer;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS shot_ix integer;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS serve_try_ix_in_point integer;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS first_rally_shot_ix integer;",
    f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN IF NOT EXISTS play_d text;"
]

def _exec(conn: Connection, sql: str, params=None):
    conn.execute(text(sql), params or {})

def ensure_phase21_columns(conn: Connection) -> None:
    for ddl in ALTERS_PHASE_21:
        _exec(conn, ddl)

SQL_PHASE21_DERIVATIONS = f"""
WITH const AS (
  SELECT
    {COURT_W}::numeric  AS court_w_m,
    {COURT_L}::numeric  AS court_l_m,
    {HALF_W}::numeric   AS half_w_m,
    {MID_Y}::numeric    AS mid_y_m,
    {SRV_BOX}::numeric  AS service_box_depth_m,
    {SERVE_EPS_M}::numeric AS serve_eps_m
),
base AS (
  SELECT
    pd.*,
    COALESCE(pd.ball_hit, pd.start_ts) AS ord_ts
  FROM {SILVER_SCHEMA}.{TABLE} pd
  WHERE pd.task_id = :task_id
),
serve_candidates AS (
  SELECT
    b.task_id, b.swing_id, b.player_id, b.ord_ts, b.ball_hit_x, b.ball_hit_y, b.swing_type, b.serve,
    COALESCE(b.serve, FALSE) AS serve_flag_bronze,
    (lower(b.swing_type) IN ('fh_overhead','fh-overhead','overhead','serve')) AS is_overhead_like,
    CASE
      WHEN b.ball_hit_y IS NULL THEN NULL
      ELSE (b.ball_hit_y <= (SELECT serve_eps_m FROM const)
            OR  b.ball_hit_y >= (SELECT court_l_m FROM const) - (SELECT serve_eps_m FROM const))
    END AS inside_serve_band
  FROM base b
),
serve_events AS (
  SELECT
    s.task_id,
    s.swing_id,
    s.player_id AS server_id,
    s.ord_ts,
    s.ball_hit_x, s.ball_hit_y,
    (s.serve_flag_bronze
     OR (s.is_overhead_like AND COALESCE(s.inside_serve_band, FALSE))) AS is_serve
  FROM serve_candidates s
),
serve_centerline AS (
  SELECT
    se.task_id,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY se.ball_hit_x) AS center_x
  FROM serve_events se
  WHERE se.is_serve AND se.ball_hit_x IS NOT NULL
  GROUP BY se.task_id
),
serve_sided AS (
  SELECT
    se.*,
    CASE
      WHEN NOT se.is_serve OR se.ball_hit_x IS NULL OR se.ball_hit_y IS NULL THEN NULL
      WHEN se.ball_hit_y < (SELECT mid_y_m FROM const)
           THEN CASE WHEN se.ball_hit_x < (SELECT sc.center_x FROM serve_centerline sc WHERE sc.task_id = se.task_id)
                     THEN 'deuce' ELSE 'ad' END
      ELSE      CASE WHEN se.ball_hit_x > (SELECT sc.center_x FROM serve_centerline sc WHERE sc.task_id = se.task_id)
                     THEN 'deuce' ELSE 'ad' END
    END AS serving_side
  FROM serve_events se
),
serve_numbered AS (
  SELECT
    ss.*,
    LAG(ss.serving_side) OVER (PARTITION BY ss.task_id ORDER BY ss.ord_ts, ss.swing_id) AS prev_side,
    LAG(ss.server_id)    OVER (PARTITION BY ss.task_id ORDER BY ss.ord_ts, ss.swing_id) AS prev_server
  FROM serve_sided ss
),
points_games AS (
  SELECT
    sn.*,
    SUM(CASE WHEN sn.prev_side IS NULL THEN 1
             WHEN sn.serving_side IS DISTINCT FROM sn.prev_side THEN 1
             ELSE 0 END)
      OVER (PARTITION BY sn.task_id ORDER BY sn.ord_ts, sn.swing_id) AS point_number,
    SUM(CASE WHEN sn.prev_server IS NULL THEN 1
             WHEN sn.server_id IS DISTINCT FROM sn.prev_server THEN 1
             ELSE 0 END)
      OVER (PARTITION BY sn.task_id ORDER BY sn.ord_ts, sn.swing_id) AS game_number
  FROM serve_numbered sn
),
point_index AS (
  SELECT
    pg.*,
    pg.point_number
      - MIN(pg.point_number) OVER (PARTITION BY pg.task_id, pg.game_number)
      + 1 AS point_in_game
  FROM points_games pg
),
swing_in_point AS (
  SELECT
    b.*,
    pg.server_id,
    pg.serving_side,
    pg.point_number,
    pg.game_number,
    pg.point_in_game
  FROM base b
  LEFT JOIN LATERAL (
    SELECT pg.*
    FROM point_index pg
    WHERE pg.task_id = b.task_id
      AND pg.ord_ts <= b.ord_ts
    ORDER BY pg.ord_ts DESC, pg.swing_id DESC
    LIMIT 1
  ) pg ON TRUE
),
swing_numbered AS (
  SELECT
    sip.*,
    ROW_NUMBER() OVER (PARTITION BY sip.task_id, sip.point_number
                       ORDER BY sip.ord_ts, sip.swing_id) AS shot_ix,
    COUNT(*)    OVER (PARTITION BY sip.task_id, sip.point_number) AS last_shot_ix,
    (COALESCE(sip.serve, FALSE) OR
     (lower(sip.swing_type) IN ('fh_overhead','fh-overhead','overhead','serve')
      AND sip.ball_hit_y IS NOT NULL
      AND (sip.ball_hit_y <= (SELECT serve_eps_m FROM const)
           OR sip.ball_hit_y >= (SELECT court_l_m FROM const) - (SELECT serve_eps_m FROM const))
     )
    ) AS serve_d
  FROM swing_in_point sip
),
serve_try AS (
  SELECT
    sn.*,
    CASE WHEN sn.serve_d THEN
      SUM(CASE WHEN sn.serve_d THEN 1 ELSE 0 END)
        OVER (PARTITION BY sn.task_id, sn.point_number
              ORDER BY sn.ord_ts, sn.swing_id
              ROWS UNBOUNDED PRECEDING)
    END AS serve_try_ix_in_point
  FROM swing_numbered sn
),
first_rally AS (
  SELECT
    st.task_id, st.point_number,
    MIN(st.shot_ix) FILTER (
      WHERE NOT st.serve_d
        AND st.player_id IS DISTINCT FROM st.server_id
    ) AS first_rally_shot_ix
  FROM serve_try st
  GROUP BY st.task_id, st.point_number
),
play_class AS (
  SELECT
    st.*,
    fr.first_rally_shot_ix,
    CASE
      WHEN st.serve_d THEN 'serve'
      WHEN st.shot_ix = fr.first_rally_shot_ix THEN 'return'
      WHEN st.ball_hit_y IS NULL THEN NULL
      WHEN st.ball_hit_y < (SELECT mid_y_m FROM const)
           THEN CASE WHEN st.ball_hit_y > (SELECT mid_y_m FROM const) - (SELECT service_box_depth_m FROM const)
                     THEN 'net' ELSE 'baseline' END
      ELSE CASE WHEN st.ball_hit_y < (SELECT mid_y_m FROM const) + (SELECT service_box_depth_m FROM const)
                THEN 'net' ELSE 'baseline' END
    END AS play_d
  FROM serve_try st
  LEFT JOIN first_rally fr
    ON fr.task_id = st.task_id AND fr.point_number = st.point_number
)
SELECT
  p.task_id, p.swing_id,
  p.serve_d, p.serving_side, p.server_id,
  p.point_number, p.game_number, p.point_in_game,
  p.shot_ix, p.serve_try_ix_in_point, p.first_rally_shot_ix,
  p.play_d
FROM play_class p
"""

def compute_phase21(conn: Connection, task_id: str) -> int:
    # 1) Ensure columns present
    ensure_phase21_columns(conn)
    # 2) Stage derivations in temp table
    _exec(conn, "CREATE TEMP TABLE _pd_p21 AS " + SQL_PHASE21_DERIVATIONS, {"task_id": task_id})
    # 3) Update back into silver.point_detail
    res = conn.execute(text(f"""
        UPDATE {SILVER_SCHEMA}.{TABLE} t
        SET
          serve_d               = s.serve_d,
          serving_side          = s.serving_side,
          server_id             = s.server_id,
          point_number          = s.point_number,
          game_number           = s.game_number,
          point_in_game         = s.point_in_game,
          shot_ix               = s.shot_ix,
          serve_try_ix_in_point = s.serve_try_ix_in_point,
          first_rally_shot_ix   = s.first_rally_shot_ix,
          play_d                = s.play_d
        FROM _pd_p21 s
        WHERE t.task_id = s.task_id AND t.swing_id = s.swing_id
    """))
    _exec(conn, "DROP TABLE IF EXISTS _pd_p21;")
    return res.rowcount or 0

def build_phase2(task_id: str) -> dict:
    if not task_id:
        raise ValueError("task_id is required")
    with engine.begin() as conn:
        affected = compute_phase21(conn, task_id)
    return {"ok": True, "task_id": task_id, "phase": "2.1", "rows_updated": affected}

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Phase 2.1 — derive serve/point sequencing (task_id-only)")
    p.add_argument("--task-id", required=True)
    args = p.parse_args()
    out = build_phase2(task_id=args.task_id)
    print(out)
