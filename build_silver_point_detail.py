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
    "game_number":             "integer",
    "game_winner_player_id":   "integer"
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
      - Uses existing serve_d / point_number / valid / exclude_d / court_x / court_y.
      - serve_try_ix_in_point:
          * '1st'   – first serve in the point
          * '2nd'   – second (or later) serve with court_x & court_y present
          * 'Double'– second (or later) serve with court_x OR court_y NULL
      - service_winner_d:
          TRUE only on the last VALID serve in the point when:
            * That serve is NOT labelled 'Double', and
            * There are NO later shots in the point with valid court_x & court_y.
          NULL everywhere else.
    """
    sql = f"""
    WITH shots AS (
      SELECT
        p.id,
        p.task_id,
        p.player_id,
        p.valid,
        p.point_number,
        COALESCE(p.exclude_d, FALSE) AS exclude_d,
        COALESCE(p.ball_hit_s, 1e15) AS ord_t,
        COALESCE(p.serve_d, FALSE)   AS is_serve,
        p.court_x,
        p.court_y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),

    -- per-point serve ordering (1,2,…) + last-serve flag
    serve_seq AS (
      SELECT
        s.*,
        CASE
          WHEN s.is_serve AND s.point_number IS NOT NULL THEN
            ROW_NUMBER() OVER (
              PARTITION BY s.task_id, s.point_number, s.player_id
              ORDER BY s.ord_t, s.id
            )
          ELSE NULL
        END AS serve_ix,
        CASE
          WHEN s.is_serve AND s.point_number IS NOT NULL THEN
            ROW_NUMBER() OVER (
              PARTITION BY s.task_id, s.point_number, s.player_id
              ORDER BY s.ord_t DESC, s.id DESC
            )
          ELSE NULL
        END AS serve_rev_ix
      FROM shots s
    ),

    -- features per serve: later valid court shots + double flag
    serve_features AS (
      SELECT
        sq.*,

        -- any later shot in the same point with valid court_x & court_y
        EXISTS (
          SELECT 1
          FROM shots r
          WHERE r.task_id      = sq.task_id
            AND r.point_number = sq.point_number
            AND r.ord_t        > sq.ord_t
            AND r.valid        = TRUE
            AND COALESCE(r.exclude_d, FALSE) = FALSE
            AND r.court_x IS NOT NULL
            AND r.court_y IS NOT NULL
        ) AS has_valid_court_after,

        -- double fault condition: 2nd+ serve, this serve missing court coords
        (sq.serve_ix >= 2 AND (sq.court_x IS NULL OR sq.court_y IS NULL)) AS is_double
      FROM serve_seq sq
    ),

    serve_labels AS (
      SELECT
        sf.id,

        -- serve_try_ix_in_point: 1st / 2nd / Double
        CASE
          WHEN sf.is_serve AND sf.point_number IS NOT NULL THEN
            CASE
              WHEN sf.serve_ix = 1 THEN '1st'
              WHEN sf.is_double THEN 'Double'
              ELSE '2nd'
            END
          ELSE NULL
        END AS serve_try_ix_in_point,

        -- service_winner_d:
        --  TRUE on last VALID serve in the point when:
        --   - that serve is NOT Double, and
        --   - there are NO later valid court_x/y shots in the point
        CASE
          WHEN sf.is_serve
               AND sf.valid = TRUE
               AND sf.point_number IS NOT NULL
               AND sf.serve_rev_ix = 1
               AND sf.is_double = FALSE
               AND sf.has_valid_court_after = FALSE
          THEN TRUE
          ELSE NULL
        END AS service_winner_d
      FROM serve_features sf
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_try_ix_in_point = sl.serve_try_ix_in_point,
      service_winner_d      = sl.service_winner_d
    FROM serve_labels sl
    WHERE p.task_id = :tid
      AND p.id = sl.id;
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
    # 4) game won by (server_end_d near↔far flips on first serves)
    phase5_set_game_winner(conn, task_id)
    # 5) game number (server_end_d near↔far flips on first serves)
    phase5_fix_game_number(conn, task_id)
    return 1

def phase5_fix_point_number(conn: Connection, task_id: str) -> int:
    """
    point_number increments ONLY when serve_side_d changes at FIRST serves.
    FIRST serves are rows with serve_d = TRUE and serve_try_ix_in_point in ('1st','Ace').
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
        AND LOWER(p.serve_try_ix_in_point::text) IN ('1st', 'ace')
        AND p.serve_side_d IN ('deuce','ad')
      ORDER BY p.ball_hit_s
    ),
    incs AS (
      SELECT
        a.task_id,
        a.anchor_s,
        a.side,
        CASE
          WHEN LAG(a.side) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s)
               IS DISTINCT FROM a.side THEN 1
          ELSE 0
        END AS inc0
      FROM anchors a
    ),
    incs_norm AS (
      SELECT
        i.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY i.task_id ORDER BY i.anchor_s) = 1
            THEN 1
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
    Phase 5 exclusions — spec:

      1) If serve_side_d is NULL → exclude_d = TRUE.
      2) If serve_d = FALSE and ball_hit_s is less than ball_hit_s of the LAST serve
         in the point → exclude_d = TRUE.
      3) Where ball_hit_s is greater than last serve in point AND ball_hit_s is more
         than 5 seconds apart from the previous shot, exclude that shot PLUS all
         later shots in the point.
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.point_number,
        p.player_id,
        p.ball_hit_s,
        COALESCE(p.serve_d, FALSE)    AS serve_d,
        p.serve_side_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),

    -- last serve time per point
    point_last_serve AS (
      SELECT
        b.task_id,
        b.point_number,
        MAX(CASE WHEN b.serve_d THEN b.ball_hit_s END) AS last_serve_s
      FROM base b
      GROUP BY b.task_id, b.point_number
    ),

    ordered AS (
      SELECT
        b.*,
        pls.last_serve_s,
        LAG(b.ball_hit_s) OVER (
          PARTITION BY b.task_id, b.point_number
          ORDER BY b.ball_hit_s, b.id
        ) AS prev_s
      FROM base b
      LEFT JOIN point_last_serve pls
        ON pls.task_id = b.task_id
       AND pls.point_number = b.point_number
    ),

    -- rule flags
    flagged AS (
      SELECT
        o.*,

        -- Rule 1: serve_side_d is NULL
        (o.serve_side_d IS NULL) AS r1_side_null,

        -- Rule 2: non-serve before the last serve in the point
        (NOT o.serve_d
         AND o.last_serve_s IS NOT NULL
         AND o.ball_hit_s < o.last_serve_s) AS r2_before_last_serve,

        -- Rule 3 (updated):
        -- gap > 5s AND this shot is AFTER the last serve in the point
        CASE
          WHEN o.prev_s IS NULL
               OR o.last_serve_s IS NULL
               OR o.ball_hit_s <= o.last_serve_s
            THEN FALSE
          ELSE (o.ball_hit_s - o.prev_s) > 5.0
        END AS gap_break
      FROM ordered o
    ),

    -- Rule 3: once a gap_break happens in a point, everything from that row onwards is excluded
    gap_chain AS (
      SELECT
        f.*,
        BOOL_OR(f.gap_break) OVER (
          PARTITION BY f.task_id, f.point_number
          ORDER BY f.ball_hit_s, f.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS r3_gap_chain
      FROM flagged f
    ),

    excl AS (
      SELECT
        g.id,
        (g.r1_side_null OR g.r2_before_last_serve OR g.r3_gap_chain) AS exclude_d
      FROM gap_chain g
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
    Point winner logic (per point_number):

      1) If any serve in the point has serve_try_ix_in_point = 'Double'
         → winner is the receiver (the other player in the task).

      2) Else, use non-excluded shots only:

         - Shot is IN if court_x & court_y are not NULL and court_y <= 23.11.

         - If last non-excluded shot is IN → last hitter wins.
         - Else if there is a last IN shot earlier in the point → that hitter wins.
         - Else (no IN at all) → opponent of the last hitter wins.

      So every real point gets a winner (no NULL), except truly degenerate cases
      where we have neither any serve nor any non-excluded shot.
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.player_id,
        COALESCE(p.exclude_d, FALSE) AS exclude_d,
        COALESCE(p.serve_d, FALSE)   AS serve_d,
        p.serve_try_ix_in_point,
        p.court_x,
        p.court_y,
        p.ball_hit_s,
        p.point_number
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),

    -- all players in this task (usually 2)
    task_players AS (
      SELECT DISTINCT
        b.task_id,
        b.player_id
      FROM base b
    ),

    -- server per point = first serve in the point
    point_server AS (
      SELECT DISTINCT ON (b.task_id, b.point_number)
        b.task_id,
        b.point_number,
        b.player_id AS server_id
      FROM base b
      WHERE b.point_number > 0
        AND b.serve_d = TRUE
      ORDER BY b.task_id, b.point_number, b.ball_hit_s
    ),

    -- receiver per point = "the other player in the task"
    point_receiver AS (
      SELECT
        ps.task_id,
        ps.point_number,
        MIN(tp.player_id) FILTER (WHERE tp.player_id <> ps.server_id) AS receiver_id
      FROM point_server ps
      JOIN task_players tp
        ON tp.task_id = ps.task_id
      GROUP BY ps.task_id, ps.point_number
    ),

    -- any Double in the point? (tolerant of spacing etc.)
    point_flags AS (
      SELECT
        b.task_id,
        b.point_number,
        BOOL_OR(
          b.serve_d
          AND b.serve_try_ix_in_point IS NOT NULL
          AND LOWER(b.serve_try_ix_in_point::text) LIKE 'double%'
        ) AS any_double
      FROM base b
      WHERE b.point_number > 0
      GROUP BY b.task_id, b.point_number
    ),

    -- non-excluded shots with IN/OUT flag (baseline at 23.11)
    ordered_included AS (
      SELECT
        b.*,
        (b.court_x IS NOT NULL AND b.court_y IS NOT NULL AND b.court_y <= 23.11) AS is_in
      FROM base b
      WHERE b.point_number > 0
        AND b.exclude_d = FALSE
    ),

    -- last shot in the point (any type)
    last_any AS (
      SELECT DISTINCT ON (o.task_id, o.point_number)
        o.task_id,
        o.point_number,
        o.player_id AS last_pid,
        o.is_in     AS last_is_in
      FROM ordered_included o
      ORDER BY o.task_id, o.point_number, o.ball_hit_s DESC, o.id DESC
    ),

    -- last IN shot in the point
    last_in AS (
      SELECT DISTINCT ON (o.task_id, o.point_number)
        o.task_id,
        o.point_number,
        o.player_id AS last_in_pid
      FROM ordered_included o
      WHERE o.is_in
      ORDER BY o.task_id, o.point_number, o.ball_hit_s DESC, o.id DESC
    ),

    -- opponent of last_pid (for the rare "no IN, no Double" case)
    last_opponent AS (
      SELECT
        la.task_id,
        la.point_number,
        MIN(tp.player_id) FILTER (WHERE tp.player_id <> la.last_pid) AS opp_pid
      FROM last_any la
      JOIN task_players tp
        ON tp.task_id = la.task_id
      GROUP BY la.task_id, la.point_number
    ),

    winners AS (
      SELECT
        ps.task_id,
        ps.point_number,
        CASE
          -- 1) any Double → receiver wins
          WHEN pf.any_double = TRUE
               AND pr.receiver_id IS NOT NULL
            THEN pr.receiver_id

          -- 2) no Double: decide from last shot / last IN shot / opponent of last
          WHEN la.last_pid IS NOT NULL THEN
            CASE
              -- last shot IN → last player wins
              WHEN la.last_is_in = TRUE
                THEN la.last_pid

              -- last shot NOT IN, but we have a last IN shot → that player wins
              WHEN la.last_is_in = FALSE
                   AND li.last_in_pid IS NOT NULL
                THEN li.last_in_pid

              -- no IN at all → opponent of last hitter wins
              WHEN la.last_is_in = FALSE
                   AND li.last_in_pid IS NULL
                   AND lo.opp_pid IS NOT NULL
                THEN lo.opp_pid

              ELSE NULL
            END

          ELSE NULL
        END AS point_winner_player_id
      FROM point_server ps
      LEFT JOIN point_receiver pr
        ON pr.task_id = ps.task_id
       AND pr.point_number = ps.point_number
      LEFT JOIN point_flags pf
        ON pf.task_id = ps.task_id
       AND pf.point_number = ps.point_number
      LEFT JOIN last_any la
        ON la.task_id = ps.task_id
       AND la.point_number = ps.point_number
      LEFT JOIN last_in li
        ON li.task_id = ps.task_id
       AND li.point_number = ps.point_number
      LEFT JOIN last_opponent lo
        ON lo.task_id = ps.task_id
       AND lo.point_number = ps.point_number
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
    FIRST serves are rows with serve_d = TRUE and serve_try_ix_in_point in ('1st','Ace').
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
        AND LOWER(p.serve_try_ix_in_point::text) IN ('1st', 'ace')
        AND p.server_end_d IN ('near','far')
      ORDER BY p.ball_hit_s
    ),
    incs AS (
      SELECT
        a.task_id,
        a.anchor_s,
        a.end_d,
        CASE
          WHEN LAG(a.end_d) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s)
               IS DISTINCT FROM a.end_d THEN 1
          ELSE 0
        END AS inc0
      FROM anchors a
    ),
    incs_norm AS (
      SELECT
        i.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY i.task_id ORDER BY i.anchor_s) = 1
            THEN 1
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

def phase5_set_game_winner(conn: Connection, task_id: str) -> int:
    sql = f"""
    WITH pts AS (
      SELECT
        p.task_id,
        p.game_number,
        p.point_number,
        p.point_winner_player_id,
        MAX(p.ball_hit_s) AS last_s
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.point_number > 0
        AND COALESCE(p.exclude_d, FALSE) = FALSE
      GROUP BY p.task_id, p.game_number, p.point_number, p.point_winner_player_id
    ),

    last_points AS (
      SELECT DISTINCT ON (task_id, game_number)
        task_id,
        game_number,
        point_winner_player_id AS winner_pid
      FROM pts
      ORDER BY task_id, game_number, last_s DESC
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET game_winner_player_id = lp.winner_pid::int
    FROM last_points lp
    WHERE p.task_id = :tid
      AND p.game_number = lp.game_number;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


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
