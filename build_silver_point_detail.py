# build_silver_point_detail.py
# Silver point_detail — additive, phase-by-phase builder (single entrypoint)
#
# PHASE 1  (BRONZE → SILVER copy; 1:1):
#   • Source: bronze.player_swing
#   • Exact field copy for Section-1 columns (valid=TRUE only)
#   • ball_hit_x / ball_hit_y:
#       - use ball_hit_location_x / ball_hit_location_y if present
#       - else parse ball_hit_location array/json [x,y]
#   • ball_hit_s:
#       - prefer JSON 'timestamp' from ball_hit object
#       - else numeric/text cast (safe)
#   • rally:
#       - cast to int only if text looks like an integer
#
# PHASE 2  (BRONZE → SILVER bounces; minimal placement helpers):
#   • Source: bronze.ball_bounce
#   • For every swing (incl. serves):
#       - find FIRST bounce strictly after ball_hit_s within window:
#           start = ball_hit_s + 0.005
#           end   = LEAST(next_ball_hit_s, ball_hit_s + 2.5)
#         (if no next_ball_hit_s, cap at ball_hit_s + 2.5)
#       - prefer bounce_type = 'floor' in ties; else earliest
#       - write: bounce_x_m, bounce_y_m, bounce_type_d, bounce_s
#   • For NON-SERVE swings only:
#       - hit_x_resolved_m = bounce_x → next_contact_x → ball_hit_x
#       - hit_source_d     = floor_bounce | any_bounce | next_contact | ball_hit
#   • All comparisons are type-safe against json/arrays.
#
# PHASES 3–5: schema placeholders (no logic in this file right now).
#
# Usage:
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase 1
#   python build_silver_point_detail.py --task-id <UUID> --phase 2
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase all

from typing import Dict, Optional, Tuple, OrderedDict as TOrderedDict
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ------------------------------- Column specs -------------------------------

# Phase 1 — Section 1 (exactly from your sheet; swing_id = bronze.id)
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
    "ball_impact_type":     "text",              # not in bronze; stays NULL
    "rally":                "integer",
    "ball_hit_x":           "double precision",
    "ball_hit_y":           "double precision",
    "start_s":              "double precision",
    "end_s":                "double precision",
    "ball_hit_s":           "double precision"
})

# Phase 2 — bounce + minimal placement helpers (additive)
PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "hit_x_resolved_m": "double precision",
    "hit_source_d":     "text",              # floor_bounce | any_bounce | next_contact | ball_hit
    "bounce_x_m":       "double precision",
    "bounce_y_m":       "double precision",
    "bounce_type_d":    "text",
    "bounce_s":         "double precision"
})

# Phase 3 — serve-only (placeholder)
PHASE3_COLS: TOrderedDict[str, str] = OrderedDict({
    "serve_d":                 "boolean",
    "server_id":               "text",
    "serve_side_d":            "text",
    "serve_try_ix_in_point":   "integer",
    "double_fault_d":          "boolean",
    "service_winner_d":        "boolean",
    "server_end_d":            "text"      # 'near' | 'far'
})

# Phase 4 — placeholder
PHASE4_COLS: TOrderedDict[str, str] = OrderedDict({
})

# Phase 5 — placeholder
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({
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

# ------------------------------- schema ensure -------------------------------

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

def ensure_table_exists(conn: Connection):
    _exec(conn, DDL_CREATE_SCHEMA)
    if not _table_exists(conn, SILVER_SCHEMA, TABLE):
        cols_sql = ",\n  ".join([f"{k} {v}" for k, v in PHASE1_COLS.items()])
        _exec(conn, f"CREATE TABLE {SILVER_SCHEMA}.{TABLE} (\n  {cols_sql}\n);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task       ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task_swing ON {SILVER_SCHEMA}.{TABLE}(task_id, swing_id);")

def ensure_phase_columns(conn: Connection, spec: Dict[str, str]):
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in spec.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

# ------------------------------- PHASE 1 — loader (pure 1:1 from bronze) -------------------------------

def phase1_load(conn: Connection, task_id: str) -> int:
    """
    PHASE 1 — Exact 1:1 copy from bronze.player_swing (valid=TRUE only).
    Uses bronze.id as swing_id. Supports either:
      • ball_hit_location_x / ball_hit_location_y
      • ball_hit_location (array/json) fallback → [0],[1]
    Rally is cast to int only if text is integer-like.
    """
    bcols = _columns_types(conn, "bronze", "player_swing")
    must_have = {
        "task_id","start_ts","end_ts","player_id","valid","serve","swing_type","volley",
        "is_in_rally","ball_player_distance","ball_speed","rally","ball_hit","id"
    }
    missing = [c for c in must_have if c not in bcols]
    if missing:
        raise RuntimeError(f"bronze.player_swing missing columns: {missing}")

    if "ball_hit_location_x" in bcols and "ball_hit_location_y" in bcols:
        bhx_expr = "s.ball_hit_location_x::double precision"
        bhy_expr = "s.ball_hit_location_y::double precision"
    elif "ball_hit_location" in bcols:
        bhx_expr = """
          CASE
            WHEN jsonb_typeof(s.ball_hit_location::jsonb)='array' AND jsonb_array_length(s.ball_hit_location::jsonb)>0
              THEN (s.ball_hit_location::jsonb->>0)::double precision
            WHEN left(s.ball_hit_location::text,1)='['
              THEN (jsonb_extract_path_text(s.ball_hit_location::jsonb,'0'))::double precision
            ELSE NULL::double precision
          END
        """.strip()
        bhy_expr = """
          CASE
            WHEN jsonb_typeof(s.ball_hit_location::jsonb)='array' AND jsonb_array_length(s.ball_hit_location::jsonb)>1
              THEN (s.ball_hit_location::jsonb->>1)::double precision
            WHEN left(s.ball_hit_location::text,1)='['
              THEN (jsonb_extract_path_text(s.ball_hit_location::jsonb,'1'))::double precision
            ELSE NULL::double precision
          END
        """.strip()
    else:
        raise RuntimeError("bronze.player_swing missing ball_hit_location[_x/_y] columns")

    rally_expr = "CASE WHEN (s.rally)::text ~ '^-?\\d+$' THEN (s.rally)::int ELSE NULL::int END"

    ball_hit_s_expr = """
      COALESCE(
        (CASE
           WHEN jsonb_typeof(s.ball_hit::jsonb)='object' AND (s.ball_hit::jsonb ? 'timestamp')
           THEN (s.ball_hit::jsonb->>'timestamp')::double precision
         END),
        NULLIF(s.ball_hit::text, '')::double precision
      )
    """.strip()

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id, player_id,
      valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit_x, ball_hit_y,
      start_s, end_s, ball_hit_s
    )
    SELECT
      s.task_id::uuid                          AS task_id,
      s.id                                     AS swing_id,        -- exact bronze id
      s.player_id                              AS player_id,
      s.valid                                  AS valid,
      s.serve                                  AS serve,
      s.swing_type                             AS swing_type,
      s.volley                                 AS volley,
      s.is_in_rally                            AS is_in_rally,
      s.ball_player_distance::double precision AS ball_player_distance,
      s.ball_speed::double precision           AS ball_speed,
      NULL::text                               AS ball_impact_type,  -- not present in player_swing
      {rally_expr}                             AS rally,
      {bhx_expr}                               AS ball_hit_x,
      {bhy_expr}                               AS ball_hit_y,
      s.start_ts::double precision             AS start_s,
      s.end_ts::double precision               AS end_s,
      {ball_hit_s_expr}                        AS ball_hit_s
    FROM bronze.player_swing s
    WHERE s.task_id::uuid = :tid
      AND COALESCE(s.valid, FALSE) IS TRUE;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- PHASE 2 — updater (pure bounce + helpers) -------------------------------

# Helpers that reference alias `b` (bronze.ball_bounce)
def _colref_b(name: str) -> str:
    n = name.lower()
    return f'b."end"' if n == "end" else f"b.{n}"

def _sec_b(cols: dict, name: str) -> str:
    n = name.lower()
    if n not in cols:
        return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(
          CASE
            WHEN jsonb_typeof({_colref_b(n)}::jsonb)='object'
                 AND ({_colref_b(n)}::jsonb ? 'timestamp')
                 AND jsonb_typeof(({_colref_b(n)}::jsonb)->'timestamp')='number'
              THEN (({_colref_b(n)}::jsonb)->>'timestamp')::double precision
            WHEN jsonb_typeof({_colref_b(n)}::jsonb)='number'
              THEN ({_colref_b(n)}::text)::double precision
            ELSE NULL::double precision
          END)"""
    if any(k in dt for k in ("double","real","numeric","integer")):
        return _colref_b(n)
    return "NULL::double precision"

def _num_b(cols: dict, name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(CASE WHEN jsonb_typeof({_colref_b(n)})='number'
                   THEN ({_colref_b(n)}::text)::double precision
                   ELSE NULL::double precision END)"""
    return _colref_b(n)

def _text_b(cols: dict, name: str) -> str:
    return _colref_b(name) if name.lower() in cols else "NULL::text"

def _xy_from_json_array_b(colref: str, index: int) -> str:
    return f"""(
      CASE
        WHEN {colref} IS NOT NULL
         AND jsonb_typeof({colref}::jsonb)='array'
         AND jsonb_array_length({colref}::jsonb) > {index}
        THEN ({colref}::jsonb->>{index})::double precision
        ELSE NULL::double precision
      END)"""

def _bronze_cols(conn: Connection, name: str) -> dict:
    rows = conn.execute(text("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema='bronze' AND table_name=:t
    """), {"t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

def _bb_src(conn: Connection):
    if _table_exists(conn, "bronze", "ball_bounce"):
        return "bronze.ball_bounce b", _bronze_cols(conn, "ball_bounce")
    raise RuntimeError("Bronze ball_bounce not found.")

def _bounce_time_expr(bcols: dict) -> str:
    # search common time fields; treat json/object safely
    for cand in ("timestamp","ts","time_s","bounce_s","t"):
        if cand in bcols:
            return _sec_b(bcols, cand)
    # some feeds pack into 'data' object
    if "data" in bcols and "json" in bcols["data"]:
        return f"""
          (CASE
             WHEN jsonb_typeof({_colref_b('data')}::jsonb)='object'
                  AND jsonb_typeof(({_colref_b('data')}::jsonb)->'timestamp')='number'
               THEN (({_colref_b('data')}::jsonb)->>'timestamp')::double precision
             ELSE NULL::double precision
           END)
        """.strip()
    return "NULL::double precision"

def _bounce_x_expr(bcols: dict) -> str:
    exprs = []
    for cand in ("court_x","x","bounce_x","x_center","x_center_m","x_m","x_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            exprs.append(_num_b(bcols, cand))
    for arr in ("court_pos","location","pos"):
        if arr in bcols:
            exprs.append(_xy_from_json_array_b(_colref_b(arr), 0))
    for jcol in ("data","bounce"):
        if jcol in bcols and "json" in bcols[jcol]:
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_x')='number'
                 THEN ({_colref_b(jcol)}::jsonb->>'court_x')::double precision
                 ELSE NULL::double precision
               END)""")
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_pos')='array'
                      AND jsonb_array_length({_colref_b(jcol)}::jsonb->'court_pos')>0
                 THEN (({_colref_b(jcol)}::jsonb->'court_pos')->>0)::double precision
                 ELSE NULL::double precision
               END)""")
    return "COALESCE(" + ", ".join(exprs) + ", NULL::double precision)" if exprs else "NULL::double precision"

def _bounce_y_expr(bcols: dict) -> str:
    exprs = []
    for cand in ("court_y","y","bounce_y","y_center","y_center_m","y_m","y_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            exprs.append(_num_b(bcols, cand))
    for arr in ("court_pos","location","pos"):
        if arr in bcols:
            exprs.append(_xy_from_json_array_b(_colref_b(arr), 1))
    for jcol in ("data","bounce"):
        if jcol in bcols and "json" in bcols[jcol]:
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_y')='number'
                 THEN ({_colref_b(jcol)}::jsonb->>'court_y')::double precision
                 ELSE NULL::double precision
               END)""")
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_pos')='array'
                      AND jsonb_array_length({_colref_b(jcol)}::jsonb->'court_pos')>1
                 THEN (({_colref_b(jcol)}::jsonb->'court_pos')->>1)::double precision
                 ELSE NULL::double precision
               END)""")
    return "COALESCE(" + ", ".join(exprs) + ", NULL::double precision)" if exprs else "NULL::double precision"

def _bounce_type_expr(bcols: dict) -> str:
    for cand in ("bounce_type","type"):
        if cand in bcols:
            return _text_b(bcols, cand)
    # sometimes inside 'data'
    if "data" in bcols and "json" in bcols["data"]:
        return f"""
          (CASE
             WHEN jsonb_typeof({_colref_b('data')}::jsonb)='object'
                  AND jsonb_typeof(({_colref_b('data')}::jsonb)->'type')='string'
               THEN ({_colref_b('data')}::jsonb->>'type')
             ELSE NULL::text
           END)
        """.strip()
    return "NULL::text"

def phase2_update(conn: Connection, task_id: str) -> int:
    """
    PHASE 2 — Bounce selection + classic hit resolution (additive, idempotent per task)
    Inputs: silver.point_detail (Phase 1) + bronze.ball_bounce
    NOTE: all joins are type-safe (uuid/text) and all json/array fields guarded.
    """
    bb_src, bcols = _bb_src(conn)
    bounce_s   = _bounce_time_expr(bcols)
    bounce_x   = _bounce_x_expr(bcols)
    bounce_y   = _bounce_y_expr(bcols)
    bounce_typ = _bounce_type_expr(bcols)

    sql = f"""
    WITH p0 AS (
      SELECT
        p.task_id, p.swing_id, p.player_id, p.rally,
        COALESCE(p.valid, FALSE) AS valid,
        COALESCE(p.serve, FALSE) AS serve,
        p.ball_hit_s, p.ball_hit_x
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.valid, FALSE) IS TRUE
    ),
    p1 AS (
      SELECT
        p0.*,
        LEAD(p0.ball_hit_s) OVER (
          PARTITION BY p0.task_id, p0.rally
          ORDER BY p0.ball_hit_s, p0.swing_id
        ) AS next_ball_hit_s,
        LEAD(p0.ball_hit_x) OVER (
          PARTITION BY p0.task_id, p0.rally
          ORDER BY p0.ball_hit_s, p0.swing_id
        ) AS next_ball_hit_x
      FROM p0
    ),
    p2 AS (
      SELECT
        p1.*,
        (p1.ball_hit_s + 0.005) AS win_start,
        LEAST(COALESCE(p1.next_ball_hit_s, p1.ball_hit_s + 2.5), p1.ball_hit_s + 2.5) AS win_end
      FROM p1
    ),
    chosen AS (
      SELECT
        p2.swing_id,
        pick.bounce_x,
        pick.bounce_y,
        pick.bounce_type,
        pick.bounce_s
      FROM p2
      LEFT JOIN LATERAL (
        SELECT
          {bounce_x}   AS bounce_x,
          {bounce_y}   AS bounce_y,
          {bounce_typ} AS bounce_type,
          {bounce_s}   AS bounce_s
        FROM {bb_src}
        WHERE
          -- keep inside same task; cast both sides to text to avoid uuid/text mismatch
          (b.task_id)::text = (p2.task_id)::text
          AND {bounce_s} IS NOT NULL
          AND {bounce_s} >  p2.win_start
          AND {bounce_s} <= p2.win_end
        ORDER BY
          CASE WHEN {bounce_typ} = 'floor' THEN 0 ELSE 1 END,
          {bounce_s}
        LIMIT 1
      ) AS pick ON TRUE
    ),
    resolved AS (
      SELECT
        p2.swing_id,
        p2.serve,
        p2.next_ball_hit_x,
        p2.ball_hit_x,
        chosen.bounce_x,
        chosen.bounce_y,
        chosen.bounce_type,
        chosen.bounce_s
      FROM p2
      LEFT JOIN chosen ON chosen.swing_id = p2.swing_id
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      bounce_x_m    = r.bounce_x,
      bounce_y_m    = r.bounce_y,
      bounce_type_d = r.bounce_type,
      bounce_s      = r.bounce_s,
      hit_x_resolved_m = CASE
        WHEN p.serve IS FALSE THEN COALESCE(r.bounce_x, r.next_ball_hit_x, r.ball_hit_x)
        ELSE p.hit_x_resolved_m
      END,
      hit_source_d = CASE
        WHEN p.serve IS FALSE THEN
          CASE
            WHEN r.bounce_x IS NOT NULL AND r.bounce_type = 'floor' THEN 'floor_bounce'
            WHEN r.bounce_x IS NOT NULL THEN 'any_bounce'
            WHEN r.next_ball_hit_x IS NOT NULL THEN 'next_contact'
            ELSE 'ball_hit'
          END
        ELSE p.hit_source_d
      END
    FROM resolved r
    WHERE p.task_id = :tid
      AND p.swing_id = r.swing_id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- Phase 2–5 (schema only now) -------------------------------

def phase2_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE5_COLS)

# ------------------------------- Orchestrator -------------------------------

def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
    """
    Orchestrate the build. Ensures schema for all phases up to `phase`.
    Phase 1 loads rows (replace deletes rows for this task_id first).
    Later phases update only their own columns.
    """
    if not task_id:
        raise ValueError("task_id is required")
    out: Dict = {"ok": True, "task_id": task_id, "phase": phase}

    with engine.begin() as conn:
        ensure_table_exists(conn)
        ensure_phase_columns(conn, PHASE1_COLS)
        if phase in ("all","2","3","4","5"): phase2_add_schema(conn)
        if phase in ("all","3","4","5"):     phase3_add_schema(conn)
        if phase in ("all","4","5"):         phase4_add_schema(conn)
        if phase in ("all","5"):             phase5_add_schema(conn)

        # Phase 1 (copy)
        if phase in ("all","1"):
            if replace:
                _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})
            out["phase1_rows"] = phase1_load(conn, task_id)

        # Phase 2 (bounces + helpers)
        if phase in ("all","2"):
            out["phase2_rows_updated"] = phase2_update(conn, task_id)

        # Stubs only
        if phase in ("all","3"): out["phase3"] = "schema-ready"
        if phase in ("all","4"): out["phase4"] = "schema-ready"
        if phase in ("all","5"): out["phase5"] = "schema-ready"

    return out

# ------------------------------- CLI -------------------------------

if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser(description="Silver point_detail — additive phases, single entrypoint")
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument("--phase", choices=["1","2","3","4","5","all"], default="all", help="which phase(s) to run")
    p.add_argument("--replace", action="store_true", help="delete existing rows for this task_id before Phase 1 load")
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
