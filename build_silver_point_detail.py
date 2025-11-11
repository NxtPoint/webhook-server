# build_silver_point_detail.py
# NextPoint Silver point_detail — P1 (with ball_hit_s and x/y split), P2 (bounce), P3 (serve)
# All logic in Python-executed SQL; no ad-hoc DB DDL beyond CREATE/ALTER as needed.

from typing import Dict, Optional, OrderedDict as TOrderedDict
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ------------------------------- Column specs -------------------------------
# Phase 1 (14): strict bronze fields + requested transforms
PHASE1_COLS = OrderedDict({
    "id":                    "bigint",            # swing id (bronze.id)
    "task_id":               "uuid",
    "player_id":             "text",
    "valid":                 "boolean",
    "serve":                 "boolean",
    "swing_type":            "text",
    "volley":                "boolean",
    "is_in_rally":           "boolean",
    "ball_player_distance":  "double precision",
    "ball_speed":            "double precision",
    "ball_impact_type":      "text",
    # transformed from raw JSON:
    "ball_hit_s":            "double precision",  # <- ball_hit -> 'timestamp'
    "ball_hit_location_x":   "double precision",  # <- ball_hit_location[0]
    "ball_hit_location_y":   "double precision"   # <- ball_hit_location[1]
})

# Phase 2 (4): exact bronze.ball_bounce names
PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "type":      "text",
    "timestamp": "double precision",
    "court_x":   "double precision",
    "court_y":   "double precision"
})

# Phase 3 (5): serve summary fields (per your spec)
PHASE3_COLS = OrderedDict({
    "serve_d":               "boolean",
    "server_id":             "text",
    "serve_side_d":          "text",
    "serve_try_ix_in_point": "integer",
    "server_end_d":          "text"
})

PHASE4_COLS = OrderedDict({
    "serve_location_ix": "integer",  # 1..8 on serve rows
    "rally_location_d":  "text"      # 'A'..'D' on non-serve rows
})

# --- Add to your column spec block ---
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({
    "exclude_d":               "boolean",
    "point_number":            "integer",
    "point_winner_player_id":  "text",
    "game_number":             "integer"
})


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
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task    ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task_id ON {SILVER_SCHEMA}.{TABLE}(task_id, id);")

def ensure_phase_columns(conn: Connection, spec: Dict[str, str]):
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in spec.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

# ------------------------------- PHASE 1 — bronze.player_swing → strict + x/y/t -------------------------------
def phase1_load(conn: Connection, task_id: str) -> int:
    """
    Copy core fields verbatim; extract:
      - ball_hit_s from ball_hit->'timestamp'
      - ball_hit_location_x / ball_hit_location_y from ball_hit_location[0/1]
    Safely guarded so non-JSON strings won't be cast unless they look like JSON.
    """
    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      id, task_id, player_id, valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      ball_hit_s, ball_hit_location_x, ball_hit_location_y
    )
    SELECT
      s.id::bigint                               AS id,
      s.task_id::uuid                            AS task_id,
      s.player_id                                AS player_id,
      COALESCE(s.valid, FALSE)                   AS valid,
      COALESCE(s.serve, FALSE)                   AS serve,
      s.swing_type                               AS swing_type,
      COALESCE(s.volley, FALSE)                  AS volley,
      COALESCE(s.is_in_rally, FALSE)             AS is_in_rally,
      s.ball_player_distance::double precision   AS ball_player_distance,
      s.ball_speed::double precision             AS ball_speed,
      s.ball_impact_type                         AS ball_impact_type,
      -- ball_hit_s
      CASE
        WHEN s.ball_hit IS NOT NULL
         AND s.ball_hit::text LIKE '{{%%'
         AND s.ball_hit::text LIKE '%%"timestamp"%%'
        THEN (s.ball_hit::jsonb ->> 'timestamp')::double precision
        ELSE NULL::double precision
      END                                         AS ball_hit_s,
      -- ball_hit_location_x
      CASE
        WHEN s.ball_hit_location IS NOT NULL
         AND s.ball_hit_location::text LIKE '[%%'
      THEN (s.ball_hit_location::jsonb ->> 0)::double precision
      ELSE NULL::double precision END             AS ball_hit_location_x,
      -- ball_hit_location_y
      CASE
        WHEN s.ball_hit_location IS NOT NULL
         AND s.ball_hit_location::text LIKE '[%%'
      THEN (s.ball_hit_location::jsonb ->> 1)::double precision
      ELSE NULL::double precision END             AS ball_hit_location_y
    FROM bronze.player_swing s
    WHERE s.task_id::uuid = :tid
      AND COALESCE(s.valid, FALSE) = TRUE;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- PHASE 2 — bronze.ball_bounce (exact 4 cols) -------------------------------
def phase2_update(conn: Connection, task_id: str) -> int:
    """
    Pick FIRST bounce strictly after contact time within:
      (ball_hit_s + 0.005,  min(next_ball_hit_s, ball_hit_s + 2.5)]
    """
    sql = f"""
    WITH p AS (
      SELECT
        p1.id, p1.task_id, p1.ball_hit_s
      FROM {SILVER_SCHEMA}.{TABLE} p1
      WHERE p1.task_id = :tid
    ),
    p_lead AS (
      SELECT
        p.*,
        LEAD(p.ball_hit_s) OVER (
          PARTITION BY p.task_id
          ORDER BY p.ball_hit_s, p.id
        ) AS next_ball_hit_s
      FROM p
    ),
    p_win AS (
      SELECT
        p_lead.*,
        (p_lead.ball_hit_s + 0.005) AS win_start,
        LEAST(COALESCE(p_lead.next_ball_hit_s, p_lead.ball_hit_s + 2.5), p_lead.ball_hit_s + 2.5) AS win_end
      FROM p_lead
    ),
    chosen AS (
      SELECT
        w.id,
        b.type       AS type,
        b.timestamp  AS timestamp,
        b.court_x    AS court_x,
        b.court_y    AS court_y
      FROM p_win w
      LEFT JOIN LATERAL (
        SELECT type, timestamp, court_x, court_y
        FROM bronze.ball_bounce b
        WHERE b.task_id::uuid = w.task_id
          AND w.ball_hit_s IS NOT NULL
          AND b.timestamp IS NOT NULL
          AND b.timestamp >  w.win_start
          AND b.timestamp <= w.win_end
        ORDER BY (type = 'floor') DESC, timestamp
        LIMIT 1
      ) b ON TRUE
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      type      = c.type,
      timestamp = c.timestamp,
      court_x   = c.court_x,
      court_y   = c.court_y
    FROM chosen c
    WHERE p.task_id = :tid
      AND p.id = c.id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ---------- PHASE 3: columns (no server_id; add service_winner_d) ----------
PHASE3_COLS = OrderedDict({
    "serve_d":               "boolean",
    "server_end_d":          "text",
    "serve_side_d":          "text",
    "serve_try_ix_in_point": "integer",
    "service_winner_d":      "boolean"
})

def phase3_add_schema(conn: Connection):
    ensure_phase_columns(conn, PHASE3_COLS)

# ---------- Phase 3 constants ----------
Y_NEAR_MIN = 23.0   # y > 23 → near
Y_FAR_MAX  = 1.0    # y < 1  → far
X_SIDE_ABS = 4.0    # side threshold

# ---------- Phase 3 updater ----------
def phase3_update(conn: Connection, task_id: str) -> int:
    """
    Phase 3:
      - Detect serves from swing_type + ball_hit_location_y (hit_y).
      - Compute server_end_d from hit_y (near if >23, far if <1).
      - Compute serve_side_d on the SERVE row using ball_hit_location_x (hit_x)
        vs a per-end midpoint, then FORWARD-FILL both end & side to all rows
        until the next serve.
      - serve_try_ix_in_point only on serve rows.
      - service_winner_d TRUE if no opponent swing occurs before next serve.
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id, p.task_id, p.player_id, p.swing_type,
        p.ball_hit_s AS t,
        COALESCE(p.ball_hit_s, 1e15) AS ord_t,
        p.ball_hit_location_x AS hit_x,
        p.ball_hit_location_y AS hit_y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),

    -- 1) detect serves + compute end from hit_y
    marks1 AS (
      SELECT
        b.*,
        (lower(coalesce(b.swing_type,'')) LIKE '%overhead%'
         AND (b.hit_y > {Y_NEAR_MIN} OR b.hit_y < {Y_FAR_MAX})) AS is_serve,
        CASE
          WHEN b.hit_y > {Y_NEAR_MIN} THEN 'near'
          WHEN b.hit_y < {Y_FAR_MAX}  THEN 'far'
          ELSE NULL
        END AS server_end_d_calc
      FROM base b
    ),

    -- 2) midpoint per end (near/far) using serve contact-x
    serve_stats AS (
      SELECT
        m1.task_id,
        (MIN(m1.hit_x) + MAX(m1.hit_x)) / 2.0 AS mid_x
      FROM marks1 m1
      WHERE m1.is_serve
      GROUP BY m1.task_id
    ),

    -- 3) compute side on the SERVE row only, using (end + hit_x) vs per-end midpoint
    marks2 AS (
      SELECT
        m1.*,
        CASE
          WHEN m1.server_end_d_calc = 'near'
            THEN CASE WHEN m1.hit_x > ss.mid_x THEN 'deuce' ELSE 'ad' END
          WHEN m1.server_end_d_calc = 'far'
            THEN CASE WHEN m1.hit_x < ss.mid_x THEN 'deuce' ELSE 'ad' END
          ELSE NULL
        END AS serve_side_d_calc
      FROM marks1 m1
      LEFT JOIN serve_stats ss
        ON ss.task_id = m1.task_id
    ),


    -- 4) order stream, track the *last serve id* to forward-fill end/side
    ordered AS (
      SELECT
        m2.*,
        SUM(CASE WHEN m2.is_serve THEN 1 ELSE 0 END)
          OVER (PARTITION BY m2.task_id ORDER BY m2.ord_t, m2.id
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS serve_grp,
        MAX(CASE WHEN m2.is_serve THEN m2.id END)
          OVER (PARTITION BY m2.task_id ORDER BY m2.ord_t, m2.id
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_serve_id
      FROM marks2 m2
    ),

    try_ix AS (
      SELECT
        o.*,
        CASE WHEN o.is_serve THEN
          ROW_NUMBER() OVER (PARTITION BY o.task_id, o.serve_grp ORDER BY o.ord_t, o.id)
        ELSE NULL END AS serve_try_ix
      FROM ordered o
    ),

    -- 5) serves-only to compute next-serve time (no FILTER on window fn)
    serves_only AS (
      SELECT
        s.id AS serve_id, s.task_id, s.player_id, s.ord_t,
        LEAD(s.ord_t) OVER (PARTITION BY s.task_id ORDER BY s.ord_t, s.id) AS next_serve_ord_t
      FROM try_ix s
      WHERE s.is_serve
    ),

    winners AS (
      SELECT
        so.serve_id,
        NOT EXISTS (
          SELECT 1
          FROM {SILVER_SCHEMA}.{TABLE} q
          WHERE q.task_id = so.task_id
            AND COALESCE(q.ball_hit_s, 1e15) >  so.ord_t
            AND (so.next_serve_ord_t IS NULL OR COALESCE(q.ball_hit_s, 1e15) < so.next_serve_ord_t)
            AND q.player_id <> so.player_id
        ) AS service_winner_d
      FROM serves_only so
    ),

    -- 6) take the *attributes from the most recent serve* for every row (FF)
    serve_rows AS (
      SELECT
        m2.id AS serve_row_id, m2.task_id,
        m2.server_end_d_calc, m2.serve_side_d_calc
      FROM marks2 m2
      WHERE m2.is_serve
    ),

    ff AS (
      SELECT
        t.id, t.task_id, t.is_serve, t.serve_try_ix,
        s.server_end_d_calc,
        s.serve_side_d_calc,
        CASE WHEN t.is_serve THEN w.service_winner_d ELSE NULL END AS service_winner_d
      FROM try_ix t
      LEFT JOIN serve_rows s
        ON s.task_id = t.task_id
       AND s.serve_row_id = t.last_serve_id
      LEFT JOIN winners w
        ON w.serve_id = t.id
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d               = ff.is_serve,
      -- forward-fill: apply the most recent serve's end/side to every row until next serve
      server_end_d          = COALESCE(ff.server_end_d_calc, p.server_end_d),
      serve_side_d          = COALESCE(ff.serve_side_d_calc, p.serve_side_d),
      -- only set try index on serve rows
      serve_try_ix_in_point = ff.serve_try_ix,
      -- winner flag only on serve rows
      service_winner_d      = COALESCE(ff.service_winner_d, p.service_winner_d)
    FROM ff
    WHERE p.task_id = :tid
      AND p.id = ff.id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


# ------------------------------- Phase 4 updater --------------------------------------------

def phase4_update(conn: Connection, task_id: str) -> int:
    """
    Phase 4 — Serve and Rally Locations
      - serve_location_ix: 1–8 for serves only
      - rally_location_d:  A–D for non-serves only
    """

    # --- SERVE LOCATION (1..8) ---
    serve_sql = f"""
    WITH base AS (
      SELECT
        p.id, p.task_id,
        p.serve_d, p.server_end_d, p.serve_side_d,
        p.ball_hit_location_x AS hit_x
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND p.ball_hit_location_x IS NOT NULL
    ),
    mid AS (
      SELECT b.task_id, (MIN(b.hit_x) + MAX(b.hit_x)) / 2.0 AS mid_x
      FROM base b
      GROUP BY b.task_id
    ),
    serves AS (
      SELECT
        b.id, b.server_end_d, b.serve_side_d, b.hit_x, m.mid_x,
        GREATEST(1, LEAST(8, FLOOR(b.hit_x)::int + 1)) AS ix_raw
      FROM base b
      JOIN mid m ON m.task_id = b.task_id
    ),
    serve_loc AS (
      SELECT
        s.id,
        CASE
          WHEN s.server_end_d='near' AND s.serve_side_d='deuce' THEN LEAST(4, s.ix_raw)
          WHEN s.server_end_d='near' AND s.serve_side_d='ad'    THEN GREATEST(5, s.ix_raw)
          WHEN s.server_end_d='far'  AND s.serve_side_d='deuce' THEN GREATEST(5, s.ix_raw)
          WHEN s.server_end_d='far'  AND s.serve_side_d='ad'    THEN LEAST(4, s.ix_raw)
          ELSE NULL
        END AS serve_location_ix
      FROM serves s
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET serve_location_ix = sl.serve_location_ix,
        rally_location_d  = NULL
    FROM serve_loc sl
    WHERE p.task_id = :tid
      AND p.id = sl.id;
    """

    # --- RALLY LOCATION (A..D) ---
    rally_sql = f"""
    WITH rallies AS (
      SELECT
        p.id,
        COALESCE(p.court_x, p.ball_hit_location_x) AS x_src,
        p.ball_hit_location_y AS y_src
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.serve_d, FALSE) IS FALSE
    ),
    rally_loc AS (
      SELECT
        r.id,
        CASE
          WHEN r.x_src IS NULL THEN NULL
          WHEN r.y_src >= 11.6 THEN
            CASE
              WHEN r.x_src < 2 THEN 'A'
              WHEN r.x_src < 4 THEN 'B'
              WHEN r.x_src < 6 THEN 'C'
              ELSE 'D'
            END
          ELSE
            CASE
              WHEN r.x_src < 2 THEN 'D'
              WHEN r.x_src < 4 THEN 'C'
              WHEN r.x_src < 6 THEN 'B'
              ELSE 'A'
            END
        END AS rally_location_d
      FROM rallies r
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_d = rl.rally_location_d,
        serve_location_ix = NULL
    FROM rally_loc rl
    WHERE p.task_id = :tid
      AND p.id = rl.id;
    """

    conn.execute(text(serve_sql), {"tid": task_id})
    conn.execute(text(rally_sql), {"tid": task_id})
    return 1

# ------------------------------- Phase 5 Updater -------------------------------------------

def phase5_update(conn, task_id: str) -> int:
    """
    Phase 5 — exclusions, point_number, point_winner_player_id (spec-only columns).
    """
    # ---------- UPDATE 1: point_number + exclude_d ----------
    sql1 = f"""
    WITH base AS (
      SELECT
        p.id, p.task_id, p.player_id, p.valid,
        p.serve_d, p.serve_try_ix_in_point, p.service_winner_d,
        p.ball_hit_s
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),
    pn AS (
      SELECT
        b.*,
        SUM(
          CASE
            WHEN COALESCE(b.serve_d, FALSE) IS TRUE
             AND (
                  (b.serve_try_ix_in_point::text ~ '^[0-9]+$' AND (b.serve_try_ix_in_point::int) = 1)
               )
            THEN 1 ELSE 0
          END
        ) OVER (PARTITION BY b.task_id ORDER BY b.ball_hit_s
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS point_number
      FROM base b
    ),
    gaps AS (
      SELECT
        pn.*,
        LAG(pn.ball_hit_s) OVER (PARTITION BY pn.task_id, pn.point_number ORDER BY pn.ball_hit_s) AS prev_s,
        LAG(pn.player_id)  OVER (PARTITION BY pn.task_id, pn.point_number ORDER BY pn.ball_hit_s) AS prev_pid
      FROM pn
    ),
    excl AS (
      SELECT
        g.*,
        CASE
          WHEN g.point_number = 0 THEN TRUE
          WHEN g.prev_s IS NULL THEN FALSE
          WHEN (g.ball_hit_s - g.prev_s) > 5.0 THEN TRUE
          WHEN (g.player_id = g.prev_pid) AND (g.ball_hit_s - g.prev_s) < 0.05 THEN TRUE
          ELSE FALSE
        END AS exclude_d
      FROM gaps g
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET point_number = e.point_number,
        exclude_d    = e.exclude_d
    FROM excl e
    WHERE p.task_id = :tid
      AND p.id = e.id;
    """
    conn.execute(text(sql1), {"tid": task_id})

    # ---------- UPDATE 2: point_winner_player_id ----------
    sql2 = f"""
    WITH base AS (
      SELECT
        p.id, p.task_id, p.player_id, p.valid,
        p.serve_d, p.serve_try_ix_in_point, p.service_winner_d,
        p.ball_hit_s, p.point_number
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),
    -- recompute exclusions to ensure consistency
    ordered AS (
      SELECT
        b.*,
        LAG(b.ball_hit_s) OVER (PARTITION BY b.task_id, b.point_number ORDER BY b.ball_hit_s) AS prev_s,
        LAG(b.player_id)  OVER (PARTITION BY b.task_id, b.point_number ORDER BY b.ball_hit_s) AS prev_pid
      FROM base b
    ),
    excl AS (
      SELECT
        o.*,
        CASE
          WHEN o.point_number = 0 THEN TRUE
          WHEN o.prev_s IS NULL THEN FALSE
          WHEN (o.ball_hit_s - o.prev_s) > 5.0 THEN TRUE
          WHEN (o.player_id = o.prev_pid) AND (o.ball_hit_s - o.prev_s) < 0.05 THEN TRUE
          ELSE FALSE
        END AS exclude_d
      FROM ordered o
    ),
    point_first_serve AS (
      SELECT DISTINCT ON (e.task_id, e.point_number)
        e.task_id, e.point_number, e.player_id AS server_id
      FROM excl e
      WHERE e.point_number > 0
        AND COALESCE(e.serve_d, FALSE) IS TRUE
        AND (e.serve_try_ix_in_point::text ~ '^[0-9]+$' AND e.serve_try_ix_in_point::int = 1)
      ORDER BY e.task_id, e.point_number, e.ball_hit_s
    ),
    point_receiver AS (
      SELECT DISTINCT ON (e.task_id, e.point_number)
        e.task_id, e.point_number, e.player_id AS receiver_id
      FROM excl e
      JOIN point_first_serve s
        ON s.task_id = e.task_id AND s.point_number = e.point_number
      WHERE e.point_number > 0
        AND e.player_id <> s.server_id
      ORDER BY e.task_id, e.point_number, e.ball_hit_s
    ),
    point_flags AS (
      SELECT
        e.task_id, e.point_number,
        BOOL_OR(
          CASE
            WHEN COALESCE(e.serve_d, FALSE) IS TRUE THEN
              CASE
                WHEN e.serve_try_ix_in_point IS NULL THEN FALSE
                WHEN LOWER(e.serve_try_ix_in_point::text) LIKE '%double%' THEN TRUE
                WHEN LOWER(e.serve_try_ix_in_point::text) LIKE '%df%'     THEN TRUE
                WHEN LOWER(e.serve_try_ix_in_point::text) LIKE '%fault%'  AND
                     (e.serve_try_ix_in_point::text ~ '^[0-9]+$' AND e.serve_try_ix_in_point::int >= 3)
                     THEN TRUE
                WHEN (e.serve_try_ix_in_point::text ~ '^[0-9]+$' AND e.serve_try_ix_in_point::int = 3)
                     THEN TRUE
                ELSE FALSE
              END
            ELSE FALSE
          END
        ) AS any_df,
        BOOL_OR(COALESCE(e.service_winner_d, FALSE)) AS any_sw
      FROM excl e
      WHERE e.point_number > 0
      GROUP BY e.task_id, e.point_number
    ),
    last_swing AS (
      SELECT DISTINCT ON (e.task_id, e.point_number)
        e.task_id, e.point_number, e.player_id AS last_pid, e.ball_hit_s
      FROM excl e
      WHERE e.point_number > 0
        AND COALESCE(e.exclude_d, FALSE) IS FALSE
        AND COALESCE(e.valid, TRUE) IS TRUE
      ORDER BY e.task_id, e.point_number, e.ball_hit_s DESC
    ),
    winners AS (
      SELECT
        pfs.task_id, pfs.point_number,
        CASE
          WHEN pf.any_df IS TRUE THEN pr.receiver_id
          WHEN pf.any_sw IS TRUE THEN pfs.server_id
          ELSE ls.last_pid
        END AS point_winner_player_id
      FROM point_first_serve pfs
      LEFT JOIN point_receiver pr
        ON pr.task_id = pfs.task_id AND pr.point_number = pfs.point_number
      LEFT JOIN point_flags pf
        ON pf.task_id = pfs.task_id AND pf.point_number = pfs.point_number
      LEFT JOIN last_swing ls
        ON ls.task_id = pfs.task_id AND ls.point_number = pfs.point_number
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET point_winner_player_id = w.point_winner_player_id
    FROM winners w
    WHERE p.task_id = :tid
      AND p.point_number = w.point_number;
    """
    conn.execute(text(sql2), {"tid": task_id})
    return 1

# ------------------------------- Phase 2–5 (schema only adds) -------------------------------
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

        if phase in ("all","4"):
            out["phase4_rows_updated"] = phase4_update(conn, task_id)

        if phase in ("all","5"):
            out["phase5_rows_updated"] = phase5_update(conn, task_id)

    return out

# ------------------------------- CLI -------------------------------
if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser(description="Silver point_detail — P1(14 with x/y/t) + P2(4) + P3(5)")
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument("--phase", choices=["1","2","3","4","5","all"], default="all", help="which phase(s) to run")
    p.add_argument("--replace", action="store_true", help="delete existing rows for this task_id before Phase 1 load")
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
