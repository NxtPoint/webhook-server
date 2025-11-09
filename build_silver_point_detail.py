# build_silver_point_detail.py
# Silver point_detail — additive, phase-by-phase builder (single entrypoint)
#
# Phase 1: Verbatim pull (17 cols) from bronze.player_swing with 2 JSON extractions,
#          using SAFE guards that never cast unless text looks like JSON.
# Phase 2: Verbatim pull (+6 cols = 23 total) from bronze.ball_bounce for first bounce in window.
# Phase 3: Serve logic (near if y>23, far if y<1; side threshold |x|>=4), derived ONLY from P1+P2.
#
# P1 columns (17):
#   task_id, swing_id, player_id, valid, serve, swing_type, volley, is_in_rally,
#   ball_player_distance, ball_speed, ball_impact_type, rally,
#   ball_hit_x, ball_hit_y, start_s, end_s, ball_hit_s
# P2 adds (6) → total 23:
#   bounce_x_m, bounce_y_m, bounce_s, bounce_type_d, hit_x_resolved_m, hit_source_d

from typing import Dict, Optional, OrderedDict as TOrderedDict
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ------------------------------- Column specs -------------------------------

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

PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "hit_x_resolved_m": "double precision",
    "hit_source_d":     "text",
    "bounce_x_m":       "double precision",
    "bounce_y_m":       "double precision",
    "bounce_type_d":    "text",
    "bounce_s":         "double precision"
})

PHASE3_COLS = OrderedDict({
    "serve_d":               "boolean",
    "server_id":             "text",
    "serve_side_d":          "text",
    "serve_try_ix_in_point": "integer",
    "double_fault_d":        "boolean",
    "service_winner_d":      "boolean",
    "server_end_d":          "text"
})

PHASE4_COLS: TOrderedDict[str, str] = OrderedDict({})
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({})

# ------------------------------- helpers ---------------------------------

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

# ------------------------------- PHASE 1 — strict, safe JSON guards -------------------------------

def phase1_load(conn: Connection, task_id: str) -> int:
    """
    STRICT pull from bronze.player_swing with guarded JSON extractions:
    - ball_hit_x / ball_hit_y from ball_hit_location [x,y] if text looks like a JSON array
    - ball_hit_s from ball_hit->'timestamp' if text looks like a JSON object containing "timestamp"
    No casts happen unless the text check passes (prevents invalid JSON cast errors).
    """
    sql = (
        f"INSERT INTO {SILVER_SCHEMA}.{TABLE} ("
        "  task_id, swing_id, player_id,"
        "  valid, serve, swing_type, volley, is_in_rally,"
        "  ball_player_distance, ball_speed, ball_impact_type,"
        "  rally, ball_hit_x, ball_hit_y,"
        "  start_s, end_s, ball_hit_s"
        ") "
        "SELECT "
        "  s.task_id::uuid                                   AS task_id,"
        "  s.id::bigint                                      AS swing_id,"
        "  s.player_id                                       AS player_id,"
        "  COALESCE(s.valid, FALSE)                          AS valid,"
        "  COALESCE(s.serve, FALSE)                          AS serve,"
        "  s.swing_type                                      AS swing_type,"
        "  COALESCE(s.volley, FALSE)                         AS volley,"
        "  COALESCE(s.is_in_rally, FALSE)                    AS is_in_rally,"
        "  s.ball_player_distance::double precision          AS ball_player_distance,"
        "  s.ball_speed::double precision                    AS ball_speed,"
        "  s.ball_impact_type                                AS ball_impact_type,"
        "  s.rally::int                                      AS rally,"
        "  CASE"
        "    WHEN s.ball_hit_location IS NOT NULL"
        "     AND s.ball_hit_location::text LIKE '[%'"
        "    THEN (s.ball_hit_location::jsonb ->> 0)::double precision"
        "    ELSE NULL::double precision"
        "  END                                               AS ball_hit_x,"
        "  CASE"
        "    WHEN s.ball_hit_location IS NOT NULL"
        "     AND s.ball_hit_location::text LIKE '[%'"
        "    THEN (s.ball_hit_location::jsonb ->> 1)::double precision"
        "    ELSE NULL::double precision"
        "  END                                               AS ball_hit_y,"
        "  s.start_ts::double precision                      AS start_s,"
        "  s.end_ts::double precision                        AS end_s,"
        "  CASE"
        "    WHEN s.ball_hit IS NOT NULL"
        "     AND s.ball_hit::text LIKE '{%'"
        "     AND s.ball_hit::text LIKE '%\"timestamp\"%'"
        "    THEN (s.ball_hit::jsonb ->> 'timestamp')::double precision"
        "    ELSE NULL::double precision"
        "  END                                               AS ball_hit_s "
        "FROM bronze.player_swing s "
        "WHERE s.task_id::uuid = :tid "
        "  AND COALESCE(s.valid, FALSE) = TRUE;"
    )
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- PHASE 2 — strict, first bounce in window -------------------------------

def phase2_update(conn: Connection, task_id: str) -> int:
    """
    Assumes bronze.ball_bounce has flat columns:
      task_id, bounce_s (double), court_x (double), court_y (double), bounce_type (text)
    Window: (ball_hit_s + 0.005, min(next_ball_hit_s, ball_hit_s + 2.5)]
    For NON-SERVE swings:
      hit_x_resolved_m = COALESCE(bounce_x, next_ball_hit_x, ball_hit_x)
      hit_source_d     = floor_bounce | any_bounce | next_contact | ball_hit
    """
    sql = (
        "WITH p AS ("
        f"  SELECT p.task_id, p.swing_id, p.player_id, p.rally,"
        "         COALESCE(p.valid, FALSE) AS valid,"
        "         COALESCE(p.serve, FALSE) AS serve,"
        "         p.ball_hit_s, p.ball_hit_x"
        f"  FROM {SILVER_SCHEMA}.{TABLE} p"
        "  WHERE p.task_id = :tid AND COALESCE(p.valid, FALSE) = TRUE"
        "),"
        "p_lead AS ("
        "  SELECT"
        "    p.*, "
        "    LEAD(p.ball_hit_s) OVER (PARTITION BY p.task_id, p.rally ORDER BY p.ball_hit_s, p.swing_id) AS next_ball_hit_s,"
        "    LEAD(p.ball_hit_x) OVER (PARTITION BY p.task_id, p.rally ORDER BY p.ball_hit_s, p.swing_id) AS next_ball_hit_x"
        "  FROM p"
        "),"
        "p_win AS ("
        "  SELECT"
        "    p_lead.*, "
        "    (p_lead.ball_hit_s + 0.005) AS win_start,"
        "    LEAST(COALESCE(p_lead.next_ball_hit_s, p_lead.ball_hit_s + 2.5), p_lead.ball_hit_s + 2.5) AS win_end"
        "  FROM p_lead"
        "),"
        "chosen AS ("
        "  SELECT"
        "    w.swing_id,"
        "    b.court_x AS bounce_x,"
        "    b.court_y AS bounce_y,"
        "    b.bounce_type,"
        "    b.bounce_s"
        "  FROM p_win w"
        "  LEFT JOIN LATERAL ("
        "    SELECT court_x, court_y, bounce_type, bounce_s"
        "    FROM bronze.ball_bounce b"
        "    WHERE b.task_id::uuid = w.task_id"
        "      AND b.bounce_s IS NOT NULL"
        "      AND b.bounce_s >  w.win_start"
        "      AND b.bounce_s <= w.win_end"
        "    ORDER BY (bounce_type = 'floor') DESC, bounce_s"
        "    LIMIT 1"
        "  ) b ON TRUE"
        ") "
        f"UPDATE {SILVER_SCHEMA}.{TABLE} p "
        "SET "
        "  bounce_x_m       = c.bounce_x,"
        "  bounce_y_m       = c.bounce_y,"
        "  bounce_type_d    = c.bounce_type,"
        "  bounce_s         = c.bounce_s,"
        "  hit_x_resolved_m = CASE"
        "                       WHEN p.serve = FALSE THEN COALESCE(c.bounce_x, w.next_ball_hit_x, w.ball_hit_x)"
        "                       ELSE p.hit_x_resolved_m"
        "                     END,"
        "  hit_source_d     = CASE"
        "                       WHEN p.serve = FALSE THEN"
        "                         CASE"
        "                           WHEN c.bounce_x IS NOT NULL AND c.bounce_type = 'floor' THEN 'floor_bounce'"
        "                           WHEN c.bounce_x IS NOT NULL THEN 'any_bounce'"
        "                           WHEN w.next_ball_hit_x IS NOT NULL THEN 'next_contact'"
        "                           ELSE 'ball_hit'"
        "                         END"
        "                       ELSE p.hit_source_d"
        "                     END "
        "FROM chosen c "
        "JOIN p_win w ON w.swing_id = c.swing_id "
        "WHERE p.task_id = :tid "
        "  AND p.swing_id = c.swing_id;"
    )
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- PHASE 3 — serve logic -------------------------------

Y_NEAR_MIN = 23.0   # y > 23 → near
Y_FAR_MAX  = 1.0    # y < 1  → far
X_SIDE_ABS = 4.0    # side threshold

def phase3_update(conn: Connection, task_id: str) -> int:
    sql = (
        "-- Pass A: detect serves + set server_end/side/server_id\n"
        "WITH serves_a AS (\n"
        f"  SELECT p.task_id, p.rally, p.swing_id, p.player_id,\n"
        "         p.ball_hit_s, p.ball_hit_x, p.ball_hit_y,\n"
        "         CASE\n"
        "           WHEN lower(coalesce(p.swing_type,'')) LIKE '%overhead%'\n"
        f"                AND (p.ball_hit_y > {Y_NEAR_MIN} OR p.ball_hit_y < {Y_FAR_MAX})\n"
        "           THEN TRUE ELSE FALSE\n"
        "         END AS is_serve,\n"
        f"         CASE WHEN p.ball_hit_y > {Y_NEAR_MIN} THEN 'near'\n"
        f"              WHEN p.ball_hit_y < {Y_FAR_MAX}  THEN 'far'\n"
        "              ELSE NULL END AS server_end_d,\n"
        f"         CASE WHEN p.ball_hit_y > {Y_NEAR_MIN} THEN CASE WHEN p.ball_hit_x >= {X_SIDE_ABS}  THEN 'ad'    ELSE 'deuce' END\n"
        f"              WHEN p.ball_hit_y < {Y_FAR_MAX}  THEN CASE WHEN p.ball_hit_x <= -{X_SIDE_ABS} THEN 'deuce' ELSE 'ad'    END\n"
        "              ELSE NULL END AS serve_side_d\n"
        f"  FROM {SILVER_SCHEMA}.{TABLE} p\n"
        "  WHERE p.task_id = :tid AND COALESCE(p.valid, FALSE) IS TRUE\n"
        "),\n"
        "apply_a AS (\n"
        f"  UPDATE {SILVER_SCHEMA}.{TABLE} p\n"
        "  SET\n"
        "    serve_d      = s.is_serve,\n"
        "    server_id    = CASE WHEN s.is_serve THEN s.player_id ELSE p.server_id END,\n"
        "    server_end_d = CASE WHEN s.is_serve THEN s.server_end_d ELSE p.server_end_d END,\n"
        "    serve_side_d = CASE WHEN s.is_serve THEN s.serve_side_d ELSE p.serve_side_d END\n"
        "  FROM serves_a s\n"
        "  WHERE p.task_id = s.task_id AND p.swing_id = s.swing_id\n"
        "  RETURNING 1\n"
        "),\n"
        "base AS (\n"
        f"  SELECT p.task_id, p.rally, p.swing_id, p.player_id, p.ball_hit_s\n"
        f"  FROM {SILVER_SCHEMA}.{TABLE} p\n"
        "  WHERE p.task_id = :tid AND COALESCE(p.serve_d, FALSE) IS TRUE\n"
        "),\n"
        "rally_counts AS (\n"
        "  SELECT task_id, rally, COUNT(*) AS serve_cnt, MAX(ball_hit_s) AS last_serve_t\n"
        "  FROM base GROUP BY task_id, rally\n"
        "),\n"
        "try_assign AS (\n"
        "  SELECT b.task_id, b.rally, b.swing_id, b.player_id, b.ball_hit_s,\n"
        "         rc.serve_cnt,\n"
        "         CASE WHEN rc.serve_cnt = 1 THEN 1\n"
        "              WHEN rc.serve_cnt >= 2 THEN 2\n"
        "              ELSE NULL END AS try_ix,\n"
        "         rc.last_serve_t\n"
        "  FROM base b JOIN rally_counts rc USING (task_id, rally)\n"
        "),\n"
        "opp_after_last AS (\n"
        "  SELECT rc.task_id, rc.rally,\n"
        f"         EXISTS (\n"
        f"           SELECT 1 FROM {SILVER_SCHEMA}.{TABLE} q\n"
        "           WHERE q.task_id = rc.task_id AND q.rally = rc.rally\n"
        "             AND q.ball_hit_s > rc.last_serve_t\n"
        "             AND q.player_id <> (\n"
        "               SELECT player_id FROM base b2\n"
        "               WHERE b2.task_id = rc.task_id AND b2.rally = rc.rally\n"
        "               ORDER BY b2.ball_hit_s ASC LIMIT 1)\n"
        "         ) AS opponent_after_last\n"
        "  FROM rally_counts rc\n"
        "),\n"
        "df_flags AS (\n"
        "  SELECT ta.task_id, ta.rally, ta.swing_id,\n"
        "         CASE WHEN ta.serve_cnt > 2 AND o.opponent_after_last = FALSE THEN TRUE ELSE FALSE END AS is_df,\n"
        "         CASE WHEN ta.serve_cnt > 2 AND o.opponent_after_last = FALSE THEN NULL ELSE ta.try_ix END AS final_try_ix\n"
        "  FROM try_assign ta JOIN opp_after_last o USING (task_id, rally)\n"
        "),\n"
        "winners AS (\n"
        "  SELECT ta.task_id, ta.rally, ta.swing_id,\n"
        "         CASE WHEN df.is_df IS TRUE THEN FALSE\n"
        f"              ELSE NOT EXISTS (SELECT 1 FROM {SILVER_SCHEMA}.{TABLE} q\n"
        "                               WHERE q.task_id = ta.task_id AND q.rally = ta.rally\n"
        "                                 AND q.ball_hit_s > ta.ball_hit_s AND q.player_id <> ta.player_id)\n"
        "         END AS service_winner_d\n"
        "  FROM try_assign ta JOIN df_flags df USING (task_id, rally, swing_id)\n"
        ")\n"
        f"UPDATE {SILVER_SCHEMA}.{TABLE} p\n"
        "SET\n"
        "  serve_try_ix_in_point = df.final_try_ix,\n"
        "  double_fault_d        = df.is_df,\n"
        "  service_winner_d      = w.service_winner_d\n"
        "FROM df_flags df JOIN winners w USING (task_id, rally, swing_id)\n"
        "WHERE p.task_id = :tid AND p.swing_id = df.swing_id;\n"
    )
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- Phase 2–5 (schema only) -------------------------------

def phase2_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE5_COLS)

# ------------------------------- Orchestrator -------------------------------

def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
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

        if phase in ("all","1"):
            if replace:
                _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})
            out["phase1_rows"] = phase1_load(conn, task_id)

        if phase in ("all","2"):
            out["phase2_rows_updated"] = phase2_update(conn, task_id)

        if phase in ("all","3"):
            out["phase3_rows_updated"] = phase3_update(conn, task_id)

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
