# build_silver_point_detail.py
# Silver point_detail — additive, phase-by-phase builder (single entrypoint)
#
# Phase 1  (BRONZE → SILVER copy):
#   - Pure 1:1 mapping from bronze.player_swing into silver.point_detail
#   - Only rows with valid=TRUE
#   - Timestamps read directly from JSON objects' "timestamp" key
#   - XY read directly from ball_hit_location array [x, y]
#
# Phase 2  (Bounces; pure bronze pull + minimal placement helpers):
#   - For each swing, choose the FIRST bronze.ball_bounce strictly AFTER ball_hit_s
#   - Write: bounce_x_m, bounce_y_m, bounce_type_d, bounce_s
#   - For NON-SERVES ONLY: hit_x_resolved_m and hit_source_d
#       hit_x_resolved_m = bounce_x -> next_contact_x -> ball_hit_x
#       hit_source_d     = floor_bounce | any_bounce | next_contact | ball_hit
#   - Window cap: end at MIN(next_ball_hit_s, ball_hit_s + 2.5)
#
# Phase 3  (Serve-only derived fields):
#   - serve_d: swing_type LIKE '%overhead%' AND |ball_hit_y| ≥ baseline (± tolerance)
#   - serve_side_d: 'deuce'|'ad' from ball_hit_x with small ε around center
#   - server_end_d: 'near'/'far' from sign of ball_hit_y
#   - serve_try_ix_in_point: within each contiguous side segment:
#         side flips present:
#            cnt=1  → that 1 is first-serve in
#            cnt>=2 → last in segment is second-serve in
#         last segment (no flip):
#            cnt in {1,2} → last is decisive (1/2 respectively)
#            cnt > 2      → double_fault_d=TRUE on last (try_ix NULL)
#   - service_winner_d: TRUE on decisive serve when no opponent swing afterwards
#
# Phases 4–5: schema placeholders.
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

# Phase 3 — serve-only (derived)
PHASE3_COLS: TOrderedDict[str, str] = OrderedDict({
    "serve_d":                 "boolean",
    "server_id":               "text",
    "serve_side_d":            "text",
    "serve_try_ix_in_point":   "integer",
    "double_fault_d":          "boolean",
    "service_winner_d":        "boolean",
    "server_end_d":            "text"      # 'near' | 'far'
})

# Phase 4 — serve/point logic (schema only for now)
PHASE4_COLS: TOrderedDict[str, str] = OrderedDict({
    # placeholder
})

# Phase 5 — serve/rally locations (schema only for now)
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({
    # placeholder
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

# --------------------------------- schema ensure ---------------------------------

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

# --------------------------------- Phase 1 loader (pure bronze copy) ---------------------------------

def phase1_load(conn: Connection, task_id: str) -> int:
    """
    PHASE 1 — Exact 1:1 copy from bronze.player_swing (valid=TRUE only).
    No derived/surrogate IDs. swing_id is copied if present, else NULL.
    """
    bcols = _columns_types(conn, "bronze", "player_swing")
    has = lambda c: c in bcols

    swing_id_expr = "s.swing_id" if has("swing_id") else "NULL::bigint"

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id, player_id,
      valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit_x, ball_hit_y,
      start_s, end_s, ball_hit_s
    )
    SELECT
      s.task_id::uuid                         AS task_id,
      {swing_id_expr}                         AS swing_id,
      s.player_id                             AS player_id,
      s.valid                                 AS valid,
      s.serve                                 AS serve,
      s.swing_type                            AS swing_type,
      s.volley                                AS volley,
      s.is_in_rally                           AS is_in_rally,
      s.ball_player_distance::double precision AS ball_player_distance,
      s.ball_speed::double precision           AS ball_speed,
      NULL::text                              AS ball_impact_type,
      s.rally::int                            AS rally,
      s.ball_hit_location_x::double precision AS ball_hit_x,
      s.ball_hit_location_y::double precision AS ball_hit_y,
      s.start_ts::double precision            AS start_s,
      s.end_ts::double precision              AS end_s,
      (s.ball_hit->>'timestamp')::double precision AS ball_hit_s
    FROM bronze.player_swing s
    WHERE s.task_id::uuid = :tid
      AND COALESCE(s.valid, FALSE) IS TRUE;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


# --------------------------------- Phase 2 updater (pure bounce + helpers) ---------------------------------

def phase2_update(conn: Connection, task_id: str) -> int:
    """
    PHASE 2 — Pure bounces from bronze + minimal placement helpers.

    For each swing p:
      Window: [p.ball_hit_s + 0.005, min(next_ball_hit_s, p.ball_hit_s + 2.5)]
      Choose first bounce in window (strictly > hit), preferring type='floor'.
      Write bounce_x_m, bounce_y_m, bounce_type_d, bounce_s.
      For non-serves: resolve hit_x_resolved_m + hit_source_d.
    """
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
        LEAD(p0.ball_hit_s) OVER (PARTITION BY p0.task_id, p0.rally ORDER BY p0.ball_hit_s, p0.swing_id) AS next_ball_hit_s,
        LEAD(p0.ball_hit_x) OVER (PARTITION BY p0.task_id, p0.rally ORDER BY p0.ball_hit_s, p0.swing_id) AS next_ball_hit_x
      FROM p0
    ),
    p2 AS (
      SELECT
        p1.*,
        (p1.ball_hit_s + 0.005) AS win_start,
        LEAST(COALESCE(p1.next_ball_hit_s, p1.ball_hit_s + 2.5), p1.ball_hit_s + 2.5) AS win_end
      FROM p1
    ),
    pick AS (
      SELECT
        p2.swing_id,
        b.timestamp AS bounce_s,
        b.type      AS bounce_type,
        b.court_x   AS bounce_x,
        b.court_y   AS bounce_y
      FROM p2
      LEFT JOIN LATERAL (
        SELECT b.*
        FROM bronze.ball_bounce b
        WHERE b.task_id::uuid = p2.task_id
          AND b.player_id     = p2.player_id
          AND b.rally         = p2.rally
          AND b.timestamp     > p2.win_start
          AND b.timestamp     <= p2.win_end
        ORDER BY CASE WHEN b.type = 'floor' THEN 0 ELSE 1 END, b.timestamp
        LIMIT 1
      ) b ON TRUE
    ),
    resolved AS (
      SELECT
        p2.swing_id,
        p2.serve,
        p2.next_ball_hit_x,
        p2.ball_hit_x,
        pick.bounce_x,
        pick.bounce_y,
        pick.bounce_type,
        pick.bounce_s
      FROM p2
      LEFT JOIN pick ON pick.swing_id = p2.swing_id
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

# --------------------------------- Phase 3 updater (serve logic) ---------------------------------

# Court constants (meters) & tolerances
BASELINE_Y_M   = 11.885      # distance from net to baseline
CENTER_EPS_M   = 0.15        # small epsilon around center line
BASELINE_EPS_M = 0.25        # tolerance for "at/behind baseline"

def phase3_update(conn: Connection, task_id: str) -> int:
    """
    PHASE 3 — Serve detection + side + try index + double fault + service winner.
    Uses ONLY Phase-1/2 fields.
    """
    # Pass A: detect serves, set side, server_id, server_end_d
    sql_a = f"""
    WITH serves AS (
      SELECT p.swing_id,
             p.player_id,
             CASE
               WHEN p.ball_hit_x < -{CENTER_EPS_M} THEN 'deuce'
               WHEN p.ball_hit_x >  {CENTER_EPS_M} THEN 'ad'
               ELSE CASE WHEN p.ball_hit_x >= 0 THEN 'ad' ELSE 'deuce' END
             END AS serve_side_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.valid, FALSE) IS TRUE
        AND lower(coalesce(p.swing_type,'')) LIKE '%overhead%'
        AND ABS(p.ball_hit_y) >= {BASELINE_Y_M - BASELINE_EPS_M}
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d      = TRUE,
      server_id    = s.player_id,
      serve_side_d = s.serve_side_d,
      server_end_d = CASE
                       WHEN p.ball_hit_y >=  1.0 THEN 'far'
                       WHEN p.ball_hit_y <= -1.0 THEN 'near'
                       ELSE NULL
                     END
    FROM serves s
    WHERE p.task_id = :tid
      AND p.swing_id = s.swing_id;
    """

    # Pass B: contiguous-segment logic for try index, double fault, service winner
    sql_b = f"""
    WITH base AS (
      SELECT p.task_id, p.rally, p.swing_id, p.player_id, p.ball_hit_s, p.serve_side_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.valid, FALSE) IS TRUE
        AND COALESCE(p.serve_d, FALSE) IS TRUE
    ),
    ordered AS (
      SELECT
        b.*,
        ROW_NUMBER() OVER (PARTITION BY b.task_id, b.rally ORDER BY b.ball_hit_s, b.swing_id) AS rn,
        LAG(b.serve_side_d) OVER (PARTITION BY b.task_id, b.rally ORDER BY b.ball_hit_s, b.swing_id) AS prev_side
      FROM base b
    ),
    seg_mark AS (
      SELECT
        o.*,
        CASE WHEN o.prev_side IS DISTINCT FROM o.serve_side_d THEN 1 ELSE 0 END AS is_new_seg
      FROM ordered o
    ),
    segmented AS (
      SELECT
        s.*,
        SUM(is_new_seg) OVER (PARTITION BY s.task_id, s.rally ORDER BY s.rn ROWS UNBOUNDED PRECEDING) AS seg_id
      FROM seg_mark s
    ),
    seg_stats AS (
      SELECT task_id, rally, seg_id, COUNT(*) AS cnt_in_seg
      FROM segmented
      GROUP BY task_id, rally, seg_id
    ),
    seg_next AS (
      SELECT s.*,
             EXISTS (
               SELECT 1 FROM seg_stats n
               WHERE n.task_id=s.task_id AND n.rally=s.rally AND n.seg_id=s.seg_id+1
             ) AS has_next_seg
      FROM seg_stats s
    ),
    seg_rows AS (
      SELECT
        g.*,
        ROW_NUMBER() OVER (PARTITION BY g.task_id, g.rally, g.seg_id ORDER BY g.ball_hit_s, g.swing_id) AS rn_in_seg,
        COUNT(*)    OVER (PARTITION BY g.task_id, g.rally, g.seg_id)                                     AS cnt_in_seg
      FROM segmented g
    ),
    resolve AS (
      SELECT
        r.task_id, r.rally, r.seg_id, r.swing_id, r.player_id, r.ball_hit_s,
        CASE
          WHEN n.has_next_seg IS TRUE THEN
            CASE
              WHEN r.cnt_in_seg = 1 THEN 1
              WHEN r.cnt_in_seg >= 2 AND r.rn_in_seg = r.cnt_in_seg THEN 2
              ELSE NULL
            END
          ELSE
            CASE
              WHEN r.cnt_in_seg IN (1,2) AND r.rn_in_seg = r.cnt_in_seg THEN r.cnt_in_seg
              ELSE NULL
            END
        END AS serve_try_ix_in_point,
        CASE
          WHEN n.has_next_seg IS FALSE AND r.cnt_in_seg > 2 AND r.rn_in_seg = r.cnt_in_seg THEN TRUE
          ELSE FALSE
        END AS double_fault_d
      FROM seg_rows r
      JOIN seg_next n
        ON n.task_id=r.task_id AND n.rally=r.rally AND n.seg_id=r.seg_id
    ),
    decisive AS (
      SELECT task_id, rally, swing_id, player_id, ball_hit_s
      FROM resolve
      WHERE serve_try_ix_in_point IN (1,2)
    ),
    winners AS (
      SELECT d.task_id, d.rally, d.swing_id,
             NOT EXISTS (
               SELECT 1 FROM {SILVER_SCHEMA}.{TABLE} b2
               WHERE b2.task_id = d.task_id
                 AND b2.rally   = d.rally
                 AND b2.ball_hit_s > d.ball_hit_s
                 AND b2.player_id <> d.player_id
             ) AS service_winner_d
      FROM decisive d
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_try_ix_in_point = r.serve_try_ix_in_point,
      double_fault_d        = r.double_fault_d,
      service_winner_d      = w.service_winner_d
    FROM resolve r
    LEFT JOIN winners w
      ON w.task_id=r.task_id AND w.rally=r.rally AND w.swing_id=r.swing_id
    WHERE p.task_id = :tid
      AND p.swing_id = r.swing_id;
    """

    conn.execute(text(sql_a), {"tid": task_id})
    res = conn.execute(text(sql_b), {"tid": task_id})
    return res.rowcount or 0

# --------------------------------- Phase 2–5 (schema only now) ---------------------------------

def phase2_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE5_COLS)

# --------------------------------- Orchestrator ---------------------------------

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

        # Phase 3 (serve logic)
        if phase in ("all","3"):
            out["phase3_rows_updated"] = phase3_update(conn, task_id)

        # Stubs for next phases
        if phase in ("all","2"): out["phase2"] = "done-schema"
        if phase in ("all","3"): out["phase3"] = "done-schema"
        if phase in ("all","4"): out["phase4"] = "schema-ready"
        if phase in ("all","5"): out["phase5"] = "schema-ready"

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
