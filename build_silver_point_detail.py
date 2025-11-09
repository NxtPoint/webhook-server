# build_silver_point_detail.py
# Silver point_detail — additive, phase-by-phase builder (single entrypoint)
# Phase 1: Bronze -> Silver (Section 1 only; valid=TRUE), including swing_id
# Phase 2–5: schema placeholders (loaders/derivers added later)
#
# Usage:
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase all
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase 1

from typing import Dict, Optional, Tuple, OrderedDict as TOrderedDict
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ------------------------------- Phase specs (schema only) -------------------------------
# Phase 1 — Section 1 (exactly from your sheet; plus swing_id)
PHASE1_COLS = OrderedDict({
    "task_id":              "uuid",
    "swing_id":             "bigint",
    "player_id":            "text",
    "valid":                "boolean",
    "serve":                "boolean",
    "swing_type":           "text",
    "volley":               "boolean",
    "is_in_rally":          "boolean",
    "ball_player_distance": "double precision",
    "ball_speed":           "double precision",
    "ball_impact_type":     "text",
    "rally":                "integer",
    "ball_hit_x":           "double precision",
    "ball_hit_y":           "double precision",
    "start_s":              "double precision",   # ← add
    "end_s":                "double precision",   # ← add
    "ball_hit_s":           "double precision"    # ← add
})

# Phase 2 — ball-hit location (classic resolution)
PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "hit_x_resolved_m": "double precision",  # final resolved X (meters), non-serve shots
    "hit_source_d":     "text"               # floor_bounce | any_bounce | next_contact | ball_hit
})

# Phase 3 — ball_bounce join (schema only for now)
PHASE3_COLS: TOrderedDict[str, str] = OrderedDict({
    # e.g. "bounce_id": "bigint", "bounce_ts": "timestamptz", "bounce_x_m": "double precision", "bounce_y_m": "double precision",
})

# Phase 4 — serve/point logic (schema only for now)
PHASE4_COLS: TOrderedDict[str, str] = OrderedDict({
    # e.g. "server_id":"text","serving_side":"text","point_number":"integer","game_number":"integer","point_in_game":"integer","shot_ix":"integer",
})

# Phase 5 — serve/rally locations (schema only for now)
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({
    # e.g. "play_d":"text","serve_try_ix_in_point":"integer","first_rally_shot_ix":"integer",
})

PHASE_COLSETS = [PHASE1_COLS, PHASE2_COLS, PHASE3_COLS, PHASE4_COLS, PHASE5_COLS]

# --------------------------------- helpers ---------------------------------
# -----------Phase 1 Helpers ----------------------------

def _exec(conn: Connection, sql: str, params: Optional[dict] = None):
    conn.execute(text(sql), params or {})

def _table_exists(conn: Connection, schema: str, name: str) -> bool:
    q = """SELECT 1 FROM information_schema.tables
           WHERE table_schema=:s AND table_name=:t"""
    return bool(conn.execute(text(q), {"s": schema, "t": name}).fetchone())

def _columns_types(conn: Connection, schema: str, name: str) -> Dict[str, str]:
    q = """SELECT column_name, data_type
           FROM information_schema.columns
           WHERE table_schema=:s AND table_name=:t"""
    rows = conn.execute(text(q), {"s": schema, "t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

def _bronze_src(conn: Connection) -> Tuple[str, Dict[str, str]]:
    # Prefer player_swing; fallback swing
    for cand in ("player_swing", "swing"):
        if _table_exists(conn, "bronze", cand):
            return f"bronze.{cand} s", _columns_types(conn, "bronze", cand)
    raise RuntimeError("Bronze source not found (expected bronze.player_swing or bronze.swing).")

def _colref(name: str) -> str:
    n = name.lower()
    return 's."end"' if n == "end" else f"s.{n}"

def _sec(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols:
        return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(
          CASE
            WHEN jsonb_typeof({_colref(n)}::jsonb)='object'
                 AND ({_colref(n)}::jsonb ? 'timestamp')
                 AND jsonb_typeof(({_colref(n)}::jsonb)->'timestamp')='number'
              THEN (({_colref(n)}::jsonb)->>'timestamp')::double precision
            WHEN jsonb_typeof({_colref(n)}::jsonb)='number'
              THEN ({_colref(n)}::text)::double precision
            ELSE NULL::double precision
          END)"""
    if any(k in dt for k in ("double","real","numeric","integer")):
        return _colref(n)
    return "NULL::double precision"

def _jsonb(cols: Dict[str, str], name: str) -> str:
    return f"{_colref(name)}::jsonb" if name.lower() in cols else "NULL::jsonb"

def _num(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(CASE WHEN jsonb_typeof({_colref(n)})='number'
                   THEN ({_colref(n)}::text)::double precision
                   ELSE NULL::double precision END)"""
    return _colref(n)

def _int(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::int"
    dt = cols[n]
    if "json" in dt:
        return f"""(CASE
           WHEN jsonb_typeof({_colref(n)})='number' THEN ({_colref(n)}::text)::int
           WHEN jsonb_typeof({_colref(n)})='object' AND ({_colref(n)} ? 'index')
                AND jsonb_typeof({_colref(n)}->'index')='number'
             THEN ({_colref(n)}->>'index')::int
           ELSE NULL::int END)"""
    return _colref(n)

def _bool(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::boolean"

def _text(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::text"

def _swing_id_expr(cols: Dict[str, str]) -> str:
    """Return an expression for swing_id from Bronze, raising if missing."""
    if "id" in cols:
        return _int(cols, "id")
    if "swing_id" in cols:
        return _int(cols, "swing_id")
    raise RuntimeError("Bronze swing identifier not found (need column 'id' or 'swing_id').")

def _xy_from_json_array(colref: str, index: int) -> str:
    return f"""(
      CASE
        WHEN {colref} IS NOT NULL
         AND jsonb_typeof({colref}::jsonb)='array'
         AND jsonb_array_length({colref}::jsonb) > {index}
        THEN ({colref}::jsonb->>{index})::double precision
        ELSE NULL::double precision
      END)"""

def _ball_hit_x_expr(cols: Dict[str, str]) -> str:
    if "ball_hit_x" in cols and "json" not in cols["ball_hit_x"]:
        return _num(cols, "ball_hit_x")
    return _xy_from_json_array(_colref("ball_hit_location"), 0)

def _ball_hit_y_expr(cols: Dict[str, str]) -> str:
    if "ball_hit_y" in cols and "json" not in cols["ball_hit_y"]:
        return _num(cols, "ball_hit_y")
    return _xy_from_json_array(_colref("ball_hit_location"), 1)

# -----------Phase 2 Helpers ----------------------------

def _bronze_cols(conn: Connection, name: str) -> Dict[str, str]:
    return _columns_types(conn, "bronze", name)

def _bb_src(conn: Connection) -> Tuple[str, Dict[str, str]]:
    if _table_exists(conn, "bronze", "ball_bounce"):
        return "bronze.ball_bounce b", _bronze_cols(conn, "ball_bounce")
    raise RuntimeError("Bronze ball_bounce not found.")

def _bounce_time_expr(bcols: Dict[str, str]) -> str:
    # Seconds from likely time fields, preferring numeric seconds or json-number
    for cand in ("timestamp","ts","time_s","bounce_s","t"):
        if cand in bcols:
            return _sec(bcols, cand)
    # Fallback: NULL
    return "NULL::double precision"

def _bounce_x_expr(bcols: Dict[str, str]) -> str:
    # Typical numeric x or a [x,y] array in 'location'
    if "x" in bcols and "json" not in bcols["x"]:
        return _num(bcols, "x")
    # Common alt names
    for cand in ("bounce_x","x_center","x_center_m","x_m","x_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            return _num(bcols, cand)
    # Array option
    if "location" in bcols:
        return _xy_from_json_array(_colref("location"), 0)
    # Fallback: NULL
    return "NULL::double precision"

def _bounce_type_expr(bcols: Dict[str, str]) -> str:
    # Common fields: bounce_type or type
    if "bounce_type" in bcols:
        return _text(bcols, "bounce_type")
    if "type" in bcols:
        return _text(bcols, "type")
    return "NULL::text"


# --------------------------------- schema ensure ---------------------------------

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

def ensure_table_exists(conn: Connection):
    _exec(conn, DDL_CREATE_SCHEMA)
    if not _table_exists(conn, SILVER_SCHEMA, TABLE):
        cols_sql = ",\n  ".join([f"{k} {v}" for k, v in PHASE1_COLS.items()])
        _exec(conn, f"CREATE TABLE {SILVER_SCHEMA}.{TABLE} (\n  {cols_sql}\n);")
        # Core indexes (no time-based indexes since those columns were dropped)
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task       ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task_swing ON {SILVER_SCHEMA}.{TABLE}(task_id, swing_id);")

def ensure_phase_columns(conn: Connection, spec: Dict[str, str]):
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in spec.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

# --------------------------------- Phase 1 loader ---------------------------------

def phase1_load(conn: Connection, task_id: str) -> int:
    """
    Import exactly the Section 1 fields from Bronze (valid=TRUE only).
    """
    src, bcols = _bronze_src(conn)
    swing_id = _swing_id_expr(bcols)
    bhx      = _ball_hit_x_expr(bcols)
    bhy      = _ball_hit_y_expr(bcols)

    start_s    = f"COALESCE({_sec(bcols,'start_ts')}, {_sec(bcols,'start')})"
    end_s      = f"COALESCE({_sec(bcols,'end_ts')},   {_sec(bcols,'end')})"
    ball_hit_s = f"COALESCE({_sec(bcols,'ball_hit')}, {_sec(bcols,'ball_hit_s')})"

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id, player_id,
      valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit_x, ball_hit_y,
      start_s, end_s, ball_hit_s
    )
    SELECT
      {_text(bcols, "task_id")}::uuid,
      {swing_id},
      {_text(bcols, "player_id")},
      {_bool(bcols, "valid")},
      {_bool(bcols, "serve")},
      {_text(bcols, "swing_type")},
      {_bool(bcols, "volley")},
      {_bool(bcols, "is_in_rally")},
      {_num(bcols, "ball_player_distance")},
      {_num(bcols, "ball_speed")},
      {_text(bcols, "ball_impact_type")},
      {_int(bcols, "rally")},
      {bhx},
      {bhy},
      {start_s},
      {end_s},
      {ball_hit_s}
    FROM {src}
    WHERE {_text(bcols, "task_id")}::uuid = :task_id
      AND COALESCE({_bool(bcols, "valid")}, FALSE) IS TRUE;
    """

    res = conn.execute(text(sql), {"task_id": task_id})
    return res.rowcount or 0

# ------------------------------------------------------------------------------------------
# Phase 2 — Business rules (classic placement logic)
#
# For each NON-SERVE swing in silver.point_detail (valid=TRUE):
# 1) Build a time window from ball_hit_s:
#       start = ball_hit_s + 0.005s
#       end   = LEAST( next_ball_hit_s, ball_hit_s + 2.5s )   // if next_ball_hit_s is NULL, use ball_hit_s + 2.5s
# 2) From bronze.ball_bounce pick the FIRST bounce in that window, prioritizing floor:
#       ORDER BY (bounce_type='floor') ASC, bounce_s ASC
#    i.e., any floor bounce wins ties; otherwise the earliest bounce of any type.
# 3) Resolve hit_x_resolved_m via fallback chain:
#       terminal-in-point → still the same rule as (2); if none → next_contact_x → own ball_hit_x
#       non-terminal     → floor_bounce_x → any_bounce_x → next_contact_x → own ball_hit_x
# 4) Set hit_source_d to: floor_bounce | any_bounce | next_contact | ball_hit
#
# Notes:
# - We do NOT compute serve location zones here (that arrives in Phases 4–5).
# - Placement bucketing and far/near mirroring are deferred to a later phase.
# - Phase 2 is additive and only updates the new Phase-2 columns for the chosen task_id.
# ------------------------------------------------------------------------------------------
def phase2_update(conn: Connection, task_id: str) -> int:
    bb_src, bcols = _bb_src(conn)
    bounce_s   = _bounce_time_expr(bcols)
    bounce_x   = _bounce_x_expr(bcols)
    bounce_typ = _bounce_type_expr(bcols)

    # Build an UPDATE using CTEs: compute next contacts & windowed bounce per swing
    sql = f"""
    WITH p0 AS (
      SELECT
        p.task_id, p.swing_id, p.player_id, p.rally,
        p.valid, COALESCE(p.serve, FALSE) AS serve,
        p.ball_hit_s, p.ball_hit_x
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.valid, FALSE) IS TRUE
        AND COALESCE(p.serve, FALSE) IS FALSE
    ),
    p1 AS (
      SELECT
        p0.*,
        LEAD(p0.ball_hit_s) OVER (PARTITION BY p0.task_id, p0.rally ORDER BY p0.ball_hit_s, p0.swing_id) AS next_ball_hit_s,
        LEAD(p0.ball_hit_x) OVER (PARTITION BY p0.task_id, p0.rally ORDER BY p0.ball_hit_s, p0.swing_id) AS next_ball_hit_x
      FROM p0
    ),
    p2 AS (
      SELECT
        p1.*,
        -- Window bounds
        (p1.ball_hit_s + 0.005) AS win_start,
        LEAST(COALESCE(p1.next_ball_hit_s, p1.ball_hit_s + 2.5), p1.ball_hit_s + 2.5) AS win_end
      FROM p1
    ),
    bb AS (
      SELECT
        {_text(bcols,'task_id')}::uuid AS task_id,
        {bounce_s} AS bounce_s,
        {bounce_x} AS bounce_x,
        {bounce_typ} AS bounce_type
      FROM {bb_src}
      WHERE {_text(bcols,'task_id')}::uuid = :tid
    ),
    chosen AS (
      -- For each swing, pick the first bounce in the window, preferring floor
      SELECT
        p2.swing_id,
        b.bounce_x,
        b.bounce_type,
        ROW_NUMBER() OVER (
          PARTITION BY p2.swing_id
          ORDER BY
            CASE WHEN b.bounce_type = 'floor' THEN 0 ELSE 1 END,
            b.bounce_s
        ) AS rn
      FROM p2
      LEFT JOIN bb b
        ON b.bounce_s > p2.win_start
       AND b.bounce_s <= p2.win_end
    ),
    pick AS (
      SELECT
        c.swing_id,
        c.bounce_x,
        c.bounce_type
      FROM chosen c
      WHERE c.rn = 1
    ),
    resolved AS (
      SELECT
        p2.swing_id,
        COALESCE(
          pick.bounce_x,
          p2.next_ball_hit_x,
          p2.ball_hit_x
        ) AS hit_x_resolved_m,
        CASE
          WHEN pick.bounce_x IS NOT NULL AND pick.bounce_type = 'floor' THEN 'floor_bounce'
          WHEN pick.bounce_x IS NOT NULL THEN 'any_bounce'
          WHEN p2.next_ball_hit_x IS NOT NULL THEN 'next_contact'
          ELSE 'ball_hit'
        END AS hit_source_d
      FROM p2
      LEFT JOIN pick ON pick.swing_id = p2.swing_id
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      hit_x_resolved_m = r.hit_x_resolved_m,
      hit_source_d     = r.hit_source_d
    FROM resolved r
    WHERE p.task_id = :tid
      AND p.swing_id = r.swing_id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    # rowcount can be None depending on driver; recompute from affected swings instead if needed
    return res.rowcount or 0


# --------------------------------- Phase 2–5 (schema only now) ---------------------------------

def phase2_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE5_COLS)

# --------------------------------- Orchestrator ---------------------------------

def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
    """
    Orchestrate the build. Always ensures schema for all phases up to `phase`.
    Phase 1 loads rows (replace deletes rows for this task_id first).
    Later phases currently only ensure columns (ready for their loaders).
    """
    if not task_id:
        raise ValueError("task_id is required")
    out: Dict = {"ok": True, "task_id": task_id, "phase": phase}

    with engine.begin() as conn:
        # Ensure table exists
        ensure_table_exists(conn)
        # Ensure schema up to selected phase (additive, no drops)
        ensure_phase_columns(conn, PHASE1_COLS)
        if phase in ("all","2","3","4","5"): phase2_add_schema(conn)
        if phase in ("all","3","4","5"):     phase3_add_schema(conn)
        if phase in ("all","4","5"):         phase4_add_schema(conn)
        if phase in ("all","5"):             phase5_add_schema(conn)

        # Phase 1 load
        if phase in ("all","1"):
            if replace:
                _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})
            out["phase1_rows"] = phase1_load(conn, task_id)

        # Phase 2 updater (classic ball-hit resolution)
        if phase in ("all","2"):
            out["phase2_rows_updated"] = phase2_update(conn, task_id)


        # Stubs for next phases (fill in later with loaders/updaters)
        if phase in ("all","2"):
            out["phase2"] = "schema-ready"
        if phase in ("all","3"):
            out["phase3"] = "schema-ready"
        if phase in ("all","4"):
            out["phase4"] = "schema-ready"
        if phase in ("all","5"):
            out["phase5"] = "schema-ready"

    return out

# --------------------------------- CLI ---------------------------------

if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser(description="Silver point_detail — additive phases, single entrypoint")
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument("--phase", choices=["1","2","3","4","5","all"], default="all", help="which phase(s) to run")
    p.add_argument("--replace", action="store_true", help="delete existing rows for this task_id before Phase 1 load")
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
