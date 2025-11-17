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

# ---------- PHASE 3: columns (no server_id; add service_winner_d) ----------
PHASE3_COLS = OrderedDict({
    "serve_d":               "boolean",
    "server_end_d":          "text",
    "serve_side_d":          "text",
    # Spec: 1st / 2nd / Ace / Fault / Double  (store as text)
    "serve_try_ix_in_point": "text",
    "service_winner_d":      "boolean"
})

# ------------------------------- PHASE 4 schema -------------------------------
PHASE4_COLS = OrderedDict({
    # keep your existing serve location column name if you already created it;
    # if not present, this will add it.
    "serve_location":          "integer",  # 1..8

    # NEW per spec:
    "rally_location_hit":      "text",     # 'A' | 'B' | 'C' | 'D'
    "rally_location_bounce":   "text"      # 'A' | 'B' | 'C' | 'D'
})


# ------------------------------- PHASE 5 schema -------------------------------
PHASE5_COLS = OrderedDict({
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
    "serve_try_ix_in_point": "text",      # TEXT here
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
      - Keep serve_d, server_end_d, serve_side_d logic EXACTLY as per spec.
      - Fix serve_try_ix_in_point and service_winner_d:

        serve_try_ix_in_point (TEXT):
          - Within each "serve sequence" for a server:
              * First serve in a point:
                  - If another serve by SAME player comes before any valid return → 'Fault'
                  - Else if NO valid return at all after this serve              → 'Ace'
                  - Else                                                        → '1st'
              * Second serve in that point:
                  - If a valid return happens before the next serve             → '2nd'
                  - Else                                                        → 'Double'  (double fault)

        service_winner_d (BOOL):
          - TRUE if:
              * serve_try_ix_in_point is NOT 'Fault' or 'Double'
              * AND there is NO valid return at all after the serve
            (i.e. classic aces / unreturned serves).
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.player_id,
        p.swing_type,
        p.ball_hit_s                       AS t,
        COALESCE(p.ball_hit_s, 1e15)       AS ord_t,
        p.ball_hit_location_x              AS hit_x,
        p.ball_hit_location_y              AS hit_y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),

    -- 1) Detect serves + compute end from hit_y (unchanged)
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

    -- 2) Midpoint per task from serve contact-x (unchanged)
    serve_stats AS (
      SELECT
        m1.task_id,
        (MIN(m1.hit_x) + MAX(m1.hit_x)) / 2.0 AS mid_x
      FROM marks1 m1
      WHERE m1.is_serve
      GROUP BY m1.task_id
    ),

    -- 3) Compute side on the SERVE row only (unchanged)
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

    -- 4) Ordered stream, track last_serve_id for forward-fill (unchanged)
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

    -- 5) All serve rows, with prev/next serve time for same player
    serves AS (
      SELECT
        o.id           AS serve_id,
        o.task_id,
        o.player_id,
        o.ord_t,
        LAG(o.ord_t) OVER (
          PARTITION BY o.task_id, o.player_id
          ORDER BY o.ord_t, o.id
        ) AS prev_serve_ord_t,
        LEAD(o.ord_t) OVER (
          PARTITION BY o.task_id, o.player_id
          ORDER BY o.ord_t, o.id
        ) AS next_serve_ord_t
      FROM ordered o
      WHERE o.is_serve
    ),

    -- 6) Classify serve sequences: second tries & valid returns
    serve_class AS (
      SELECT
        s.*,

        -- Is this serve the SECOND try of the same point?
        -- (No valid return between previous serve by same player and this one)
        CASE
          WHEN s.prev_serve_ord_t IS NULL THEN FALSE
          ELSE NOT EXISTS (
            SELECT 1
            FROM {SILVER_SCHEMA}.{TABLE} q
            WHERE q.task_id = s.task_id
              AND COALESCE(q.ball_hit_s, 1e15) > s.prev_serve_ord_t
              AND COALESCE(q.ball_hit_s, 1e15) < s.ord_t
              AND q.player_id <> s.player_id
              AND COALESCE(q.valid, FALSE) IS TRUE
          )
        END AS is_second_try,

        -- Any valid return at any time after this serve?
        EXISTS (
          SELECT 1
          FROM {SILVER_SCHEMA}.{TABLE} q
          WHERE q.task_id = s.task_id
            AND COALESCE(q.ball_hit_s, 1e15) > s.ord_t
            AND q.player_id <> s.player_id
            AND COALESCE(q.valid, FALSE) IS TRUE
        ) AS has_any_valid_return_after,

        -- Valid return BEFORE the next serve by the same player?
        EXISTS (
          SELECT 1
          FROM {SILVER_SCHEMA}.{TABLE} q
          WHERE q.task_id = s.task_id
            AND COALESCE(q.ball_hit_s, 1e15) > s.ord_t
            AND (s.next_serve_ord_t IS NULL OR COALESCE(q.ball_hit_s, 1e15) < s.next_serve_ord_t)
            AND q.player_id <> s.player_id
            AND COALESCE(q.valid, FALSE) IS TRUE
        ) AS has_valid_return_before_next
      FROM serves s
    ),

    -- 7) Map to text labels + service winner flag
    serve_labels AS (
      SELECT
        sc.serve_id,
        CASE
          WHEN sc.is_second_try IS FALSE THEN
            CASE
              -- First serve: second serve before any valid return → Fault
              WHEN sc.next_serve_ord_t IS NOT NULL
                   AND sc.has_valid_return_before_next IS FALSE
                THEN 'Fault'
              -- First serve: no second serve before return, and no return at all → Ace
              WHEN sc.has_any_valid_return_after IS FALSE
                THEN 'Ace'
              -- First serve: rally starts → 1st
              ELSE
                '1st'
            END
          ELSE
            CASE
              -- Second serve with valid return before next serve → 2nd
              WHEN sc.has_valid_return_before_next IS TRUE
                THEN '2nd'
              -- Second serve with no valid return → Double fault
              ELSE
                'Double'
            END
        END AS serve_try_label,
        CASE
          -- Service winner only if NOT a fault/double and no valid return at all
          WHEN (
            CASE
              WHEN sc.is_second_try IS FALSE THEN
                CASE
                  WHEN sc.next_serve_ord_t IS NOT NULL
                       AND sc.has_valid_return_before_next IS FALSE THEN 'Fault'
                  WHEN sc.has_any_valid_return_after IS FALSE THEN 'Ace'
                  ELSE '1st'
                END
              ELSE
                CASE
                  WHEN sc.has_valid_return_before_next IS TRUE THEN '2nd'
                  ELSE 'Double'
                END
            END
          ) NOT IN ('Fault','Double')
          AND sc.has_any_valid_return_after IS FALSE
          THEN TRUE
          ELSE FALSE
        END AS service_winner_d
      FROM serve_class sc
    ),

    -- 8) Serve rows for forward-fill of end/side (unchanged)
    serve_rows AS (
      SELECT
        m2.id AS serve_row_id,
        m2.task_id,
        m2.server_end_d_calc,
        m2.serve_side_d_calc
      FROM marks2 m2
      WHERE m2.is_serve
    ),

    -- 9) Final frame: every row + latest serve attributes + serve labels
    ff AS (
      SELECT
        o.id,
        o.task_id,
        o.is_serve,
        sr.server_end_d_calc,
        sr.serve_side_d_calc,
        sl.serve_try_label,
        CASE
          WHEN o.is_serve THEN sl.service_winner_d
          ELSE NULL
        END AS service_winner_d
      FROM ordered o
      LEFT JOIN serve_rows sr
        ON sr.task_id = o.task_id
       AND sr.serve_row_id = o.last_serve_id
      LEFT JOIN serve_labels sl
        ON sl.serve_id = o.id
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d               = ff.is_serve,
      -- forward-fill end/side from most recent serve
      server_end_d          = COALESCE(ff.server_end_d_calc, p.server_end_d),
      serve_side_d          = COALESCE(ff.serve_side_d_calc, p.serve_side_d),
      -- text label: '1st' / '2nd' / 'Ace' / 'Fault' / 'Double'
      serve_try_ix_in_point = ff.serve_try_label,
      -- winner flag only on serve rows, else leave as-is / NULL
      service_winner_d      = COALESCE(ff.service_winner_d, p.service_winner_d)
    FROM ff
    WHERE p.task_id = :tid
      AND p.id = ff.id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ----------------------- Phase 4 updater ------------------------------

def phase4_update(conn: Connection, task_id: str) -> int:
    """
    Phase 4 (spec-only):
      - Columns: serve_location (int 1–8), rally_location_hit (A–D), rally_location_bounce (A–D)
      - Inputs only from spec: serve_d, server_end_d, serve_side_d, serve_try_ix_in_point,
        ball_hit_location_x, ball_hit_location_y, ball_hit_s, valid.
    """

    # 0) Ensure P4 columns exist (safe no-ops if they already do)
    ensure_phase_columns(conn, OrderedDict({
        "serve_location":        "integer",
        "rally_location_hit":    "text",
        "rally_location_bounce": "text",
    }))

    # 1) Midpoint from FIRST serves only (try = 1). Default to 4.0 if none.
    sql_mid = f"""
    WITH fs AS (
      SELECT
        NULLIF(TRIM(p.ball_hit_location_x::text), '')::double precision AS x
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND (
          (p.serve_try_ix_in_point IS NOT NULL AND p.serve_try_ix_in_point::text ~ '^[0-9]+$'
           AND p.serve_try_ix_in_point::int = 1)
        )
        AND NULLIF(TRIM(p.ball_hit_location_x::text), '') IS NOT NULL
    )
    SELECT COALESCE(AVG(x), 4.0) FROM fs;
    """
    mid_x = conn.execute(text(sql_mid), {"tid": task_id}).scalar() or 4.0

    # 2) Serve location (1–8): recompute for ALL serves (robust cast/trim/lower)
    sql_srv = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET serve_location =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS FALSE THEN NULL
        WHEN NULLIF(TRIM(p.ball_hit_location_x::text), '') IS NULL THEN NULL
        ELSE
          CASE
            WHEN lower(TRIM(p.server_end_d)) = 'near' AND lower(TRIM(p.serve_side_d)) = 'deuce'
                 AND (NULLIF(TRIM(p.ball_hit_location_x::text), '')::double precision) < :mid THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision < 1 THEN 1
                WHEN (p.ball_hit_location_x)::double precision < 2 THEN 2
                WHEN (p.ball_hit_location_x)::double precision < 3 THEN 3
                ELSE 4
              END

            WHEN lower(TRIM(p.server_end_d)) = 'near' AND lower(TRIM(p.serve_side_d)) = 'ad'
                 AND (NULLIF(TRIM(p.ball_hit_location_x::text), '')::double precision) >= :mid THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision < (:mid + 1) THEN 5
                WHEN (p.ball_hit_location_x)::double precision < (:mid + 2) THEN 6
                WHEN (p.ball_hit_location_x)::double precision < (:mid + 3) THEN 7
                ELSE 8
              END

            WHEN lower(TRIM(p.server_end_d)) = 'far'  AND lower(TRIM(p.serve_side_d)) = 'deuce'
                 AND (NULLIF(TRIM(p.ball_hit_location_x::text), '')::double precision) > :mid THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision < (:mid + 1) THEN 5
                WHEN (p.ball_hit_location_x)::double precision < (:mid + 2) THEN 6
                WHEN (p.ball_hit_location_x)::double precision < (:mid + 3) THEN 7
                ELSE 8
              END

            WHEN lower(TRIM(p.server_end_d)) = 'far'  AND lower(TRIM(p.serve_side_d)) = 'ad'
                 AND (NULLIF(TRIM(p.ball_hit_location_x::text), '')::double precision) <= :mid THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision < 1 THEN 1
                WHEN (p.ball_hit_location_x)::double precision < 2 THEN 2
                WHEN (p.ball_hit_location_x)::double precision < 3 THEN 3
                ELSE 4
              END

            -- Fallback: if side missing but end+x present, infer bands with mid_x
            WHEN lower(TRIM(p.server_end_d)) = 'near' AND (NULLIF(TRIM(p.ball_hit_location_x::text), '')::double precision) IS NOT NULL THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision <  :mid THEN
                  CASE
                    WHEN (p.ball_hit_location_x)::double precision < 1 THEN 1
                    WHEN (p.ball_hit_location_x)::double precision < 2 THEN 2
                    WHEN (p.ball_hit_location_x)::double precision < 3 THEN 3
                    ELSE 4
                  END
                ELSE
                  CASE
                    WHEN (p.ball_hit_location_x)::double precision < (:mid + 1) THEN 5
                    WHEN (p.ball_hit_location_x)::double precision < (:mid + 2) THEN 6
                    WHEN (p.ball_hit_location_x)::double precision < (:mid + 3) THEN 7
                    ELSE 8
                  END
              END

            WHEN lower(TRIM(p.server_end_d)) = 'far'  AND (NULLIF(TRIM(p.ball_hit_location_x::text), '')::double precision) IS NOT NULL THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision <= :mid THEN
                  CASE
                    WHEN (p.ball_hit_location_x)::double precision < 1 THEN 1
                    WHEN (p.ball_hit_location_x)::double precision < 2 THEN 2
                    WHEN (p.ball_hit_location_x)::double precision < 3 THEN 3
                    ELSE 4
                  END
                ELSE
                  CASE
                    WHEN (p.ball_hit_location_x)::double precision < (:mid + 1) THEN 5
                    WHEN (p.ball_hit_location_x)::double precision < (:mid + 2) THEN 6
                    WHEN (p.ball_hit_location_x)::double precision < (:mid + 3) THEN 7
                    ELSE 8
                  END
              END

            ELSE NULL
          END
      END
    WHERE p.task_id = :tid;
    """
    conn.execute(text(sql_srv), {"tid": task_id, "mid": float(mid_x)})

    # 3) Rally location (hit): A–D, non-serves only (11.6 y split), robust casts
    sql_rl_hit = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_hit =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        ELSE
          CASE
            WHEN NULLIF(TRIM(p.ball_hit_location_x::text), '') IS NULL THEN NULL
            WHEN NULLIF(TRIM(p.ball_hit_location_y::text), '') IS NULL THEN NULL
            WHEN (p.ball_hit_location_y)::double precision >= 11.6 THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision < 2 THEN 'D'
                WHEN (p.ball_hit_location_x)::double precision < 4 THEN 'B'
                WHEN (p.ball_hit_location_x)::double precision < 6 THEN 'C'
                ELSE 'A'
              END
            ELSE
              CASE
                WHEN (p.ball_hit_location_x)::double precision < 2 THEN 'A'
                WHEN (p.ball_hit_location_x)::double precision < 4 THEN 'B'
                WHEN (p.ball_hit_location_x)::double precision < 6 THEN 'C'
                ELSE 'D'
              END
          END
      END
    WHERE p.task_id = :tid;
    """
    conn.execute(text(sql_rl_hit), {"tid": task_id})

    # 4) Rally location (bounce): A–D, non-serves only, reversed bands per spec
    sql_rl_bnc = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_bounce =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        ELSE
          CASE
            WHEN NULLIF(TRIM(p.ball_hit_location_x::text), '') IS NULL THEN NULL
            WHEN NULLIF(TRIM(p.ball_hit_location_y::text), '') IS NULL THEN NULL
            WHEN (p.ball_hit_location_y)::double precision >= 11.6 THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision < 2 THEN 'D'
                WHEN (p.ball_hit_location_x)::double precision < 4 THEN 'C'
                WHEN (p.ball_hit_location_x)::double precision < 6 THEN 'B'
                ELSE 'A'
              END
            ELSE
              CASE
                WHEN (p.ball_hit_location_x)::double precision < 2 THEN 'A'
                WHEN (p.ball_hit_location_x)::double precision < 4 THEN 'B'
                WHEN (p.ball_hit_location_x)::double precision < 6 THEN 'C'
                ELSE 'D'
              END
          END
      END
    WHERE p.task_id = :tid;
    """
    conn.execute(text(sql_rl_bnc), {"tid": task_id})

    return 1

# ------------------------------- PHASE 5 ---------------------------------

def phase5_update(conn: Connection, task_id: str) -> int:
    # 1) point_number from serve_side_d flips (first serves)
    phase5_fix_point_number(conn, task_id)
    # 2) exclusions (pre-serve, >5s gap, <0.05s same-player)
    phase5_apply_exclusions(conn, task_id)
    # 3) point winner (DF via serve_try_ix_in_point, then service_winner_d, else last valid swing)
    phase5_set_point_winner(conn, task_id)
    # 4) game number (server_end_d near↔far flips on first serves)
    phase5_fix_game_number(conn, task_id)
    return 1

def phase5_fix_point_number(conn: Connection, task_id: str) -> int:
    """
    point_number increments ONLY when serve_side_d changes at FIRST serves.
    Persist across all rows by ball_hit_s.
    """
    sql = f"""
    WITH anchors AS (
      SELECT
        p.task_id,
        p.ball_hit_s AS anchor_s,
        p.serve_side_d AS side
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND (p.serve_try_ix_in_point::text ~ '^[0-9]+$' AND p.serve_try_ix_in_point::int = 1)
        AND p.serve_side_d IN ('deuce','ad')
      ORDER BY p.ball_hit_s
    ),
    incs AS (
      SELECT
        a.task_id,
        a.anchor_s,
        a.side,
        CASE
          WHEN LAG(a.side) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s) IS DISTINCT FROM a.side THEN 1
          ELSE 0
        END AS inc0
      FROM anchors a
    ),
    incs_norm AS (
      SELECT
        i.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY i.task_id ORDER BY i.anchor_s) = 1 THEN 1
          ELSE i.inc0
        END AS inc
      FROM incs i
    ),
    pn_rows AS (
      SELECT
        p.id,
        COALESCE(
          (SELECT SUM(n.inc)
           FROM incs_norm n
           WHERE n.task_id = p.task_id
             AND n.anchor_s <= p.ball_hit_s),
          0
        ) AS pn
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET point_number = r.pn
    FROM pn_rows r
    WHERE p.id = r.id
      AND p.task_id = :tid;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


def phase5_apply_exclusions(conn: Connection, task_id: str) -> int:
    """
    exclude_d inside each point_number:
      - point_number = 0 → TRUE
      - gap > 5.0s → TRUE
      - same player within 0.05s → TRUE
      - else FALSE
    """
    sql = f"""
    WITH ordered AS (
      SELECT
        p.id, p.task_id, p.point_number, p.player_id, p.ball_hit_s, p.valid
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),
    gaps AS (
      SELECT
        o.*,
        LAG(o.ball_hit_s) OVER (PARTITION BY o.task_id, o.point_number ORDER BY o.ball_hit_s) AS prev_s,
        LAG(o.player_id)  OVER (PARTITION BY o.task_id, o.point_number ORDER BY o.ball_hit_s) AS prev_pid
      FROM ordered o
    ),
    excl AS (
      SELECT
        g.id,
        CASE
          WHEN COALESCE(g.point_number,0) = 0 THEN TRUE
          WHEN g.prev_s IS NULL THEN FALSE
          WHEN (g.ball_hit_s - g.prev_s) > 5.0 THEN TRUE
          WHEN (g.player_id = g.prev_pid) AND (g.ball_hit_s - g.prev_s) < 0.05 THEN TRUE
          ELSE FALSE
        END AS exclude_d
      FROM gaps g
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET exclude_d = e.exclude_d
    FROM excl e
    WHERE p.task_id = :tid
      AND p.id = e.id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


def phase5_set_point_winner(conn: Connection, task_id: str) -> int:
    """
    Winner priority per point:
      - any double-fault → receiver
      - else any service_winner_d → server
      - else last non-excluded, valid swing → that player
    Double-fault derived ONLY from serve_try_ix_in_point.
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id, p.task_id, p.player_id, p.valid,
        p.serve_d, p.serve_try_ix_in_point, p.service_winner_d,
        p.ball_hit_s, p.point_number
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),
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
          WHEN COALESCE(o.point_number,0) = 0 THEN TRUE
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
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


def phase5_fix_game_number(conn: Connection, task_id: str) -> int:
    """
    game_number increments when server_end_d flips near↔far at FIRST serves.
    Persist to all rows by ball_hit_s. First game = 1.
    """
    sql = f"""
    WITH anchors AS (
      SELECT
        p.task_id,
        p.ball_hit_s AS anchor_s,
        p.server_end_d AS end_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND (p.serve_try_ix_in_point::text ~ '^[0-9]+$' AND p.serve_try_ix_in_point::int = 1)
        AND p.server_end_d IN ('near','far')
      ORDER BY p.ball_hit_s
    ),
    incs AS (
      SELECT
        a.task_id, a.anchor_s, a.end_d,
        CASE
          WHEN LAG(a.end_d) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s) IS DISTINCT FROM a.end_d THEN 1
          ELSE 0
        END AS inc0
      FROM anchors a
    ),
    incs_norm AS (
      SELECT
        i.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY i.task_id ORDER BY i.anchor_s) = 1 THEN 1
          ELSE i.inc0
        END AS inc
      FROM incs i
    ),
    g_rows AS (
      SELECT
        p.id,
        COALESCE(
          (SELECT SUM(n.inc)
           FROM incs_norm n
           WHERE n.task_id = p.task_id
             AND n.anchor_s <= p.ball_hit_s),
          0
        ) AS gnum
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET game_number = r.gnum
    FROM g_rows r
    WHERE p.id = r.id
      AND p.task_id = :tid;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0



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
