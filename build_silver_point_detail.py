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
})

# Phase 2 — player info (schema only for now)
PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    # e.g. "player_handedness": "text",
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

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id, player_id,
      valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit_x, ball_hit_y
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
      {bhy}
    FROM {src}
    WHERE {_text(bcols, "task_id")}::uuid = :task_id
      AND COALESCE({_bool(bcols, "valid")}, FALSE) IS TRUE;
    """
    res = conn.execute(text(sql), {"task_id": task_id})
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
