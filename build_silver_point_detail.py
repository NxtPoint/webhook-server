# build_silver_v2.py
# NextPoint Silver: silver.point_detail — rewritten as 5 SQL passes.
#
# Pass 1 (INSERT): bronze.player_swing → silver.point_detail (core fields)
# Pass 2 (UPDATE): bronze.ball_bounce → bounce coordinates (with geometric guard)
# Pass 3 (UPDATE): Serve detection → point/game structure → exclusions → outcomes → winners
# Pass 4 (UPDATE): Zone classification + coordinate normalization
# Pass 5 (UPDATE): Analytics features (serve buckets, stroke, rally length, aggression, depth)
#
# Business rules are identical to build_silver_point_detail.py phases 1-7.
# Column names and types are unchanged.

import logging
from typing import Dict, Optional
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

log = logging.getLogger(__name__)

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ============================================================
# SPORT CONFIG — single source of truth for all court geometry.
# ============================================================

DEFAULT_SPORT_TYPE = "tennis_singles"

SPORT_CONFIG: Dict[str, Dict[str, float]] = {
    "tennis_singles": {
        "court_length_m":      23.77,
        "doubles_width_m":     10.97,
        "singles_left_x":       1.37,
        "singles_right_x":      9.60,
        "singles_width":        8.23,
        "half_y":              11.885,
        "service_line_m":       6.40,
        "far_service_line_m":  17.37,
        "eps_baseline_m":       0.30,
    },
}

# ============================================================
# COLUMN SPECS (unchanged from original)
# ============================================================

ALL_COLS = OrderedDict({
    # Phase 1 (load)
    "id":                    "bigint",
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
    "ball_hit_s":            "double precision",
    "ball_hit_location_x":   "double precision",
    "ball_hit_location_y":   "double precision",
    # Phase 2 (bounce)
    "type":                  "text",
    "timestamp":             "double precision",
    "court_x":               "double precision",
    "court_y":               "double precision",
    # Phase 3 (serve context + point structure)
    "serve_d":               "boolean",
    "server_end_d":          "text",
    "serve_side_d":          "text",
    "serve_try_ix_in_point": "text",
    "service_winner_d":      "boolean",
    "point_number":          "integer",
    "exclude_d":             "boolean",
    "point_winner_player_id":"text",
    "game_number":           "integer",
    "game_winner_player_id": "text",
    "server_id":             "text",
    "shot_ix_in_point":      "integer",
    "shot_phase_d":          "text",
    "shot_outcome_d":        "text",
    "point_key":             "text",
    "set_number":            "integer",
    "set_game_number":       "integer",
    "ace_d":                 "boolean",
    # Phase 4 (zones + normalization)
    "serve_location":        "integer",
    "rally_location_hit":    "text",
    "rally_location_bounce": "text",
    "invert_hit":            "boolean",
    "invert_bounce":         "boolean",
    "ball_hit_x_norm":       "double precision",
    "ball_hit_y_norm":       "double precision",
    "ball_bounce_x_norm":    "double precision",
    "ball_bounce_y_norm":    "double precision",
    # Phase 5 (analytics)
    "serve_bucket_d":        "text",
    "rally_length":          "integer",
    "rally_length_point":    "integer",
    "rally_length_bucket_d": "text",
    "stroke_d":              "text",
    "shot_q":                "integer",
    "shot_key_q":            "text",
    "aggression_d":          "text",
    "depth_d":               "text",
})


# ============================================================
# HELPERS
# ============================================================

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


def ensure_schema(conn: Connection):
    _exec(conn, f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};")

    # Create table with Phase 1 columns if it doesn't exist
    if not _table_exists(conn, SILVER_SCHEMA, TABLE):
        phase1_cols = list(ALL_COLS.items())[:14]  # first 14 = Phase 1
        cols_sql = ",\n  ".join([f"{k} {v}" for k, v in phase1_cols])
        _exec(conn, f"CREATE TABLE {SILVER_SCHEMA}.{TABLE} (\n  {cols_sql}\n);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task_id ON {SILVER_SCHEMA}.{TABLE}(task_id, id);")

    # Unique constraint
    _exec(conn, f"""
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = '{SILVER_SCHEMA}' AND t.relname = '{TABLE}'
          AND c.conname = 'uq_point_detail_task_id'
      ) THEN
        ALTER TABLE {SILVER_SCHEMA}.{TABLE}
        ADD CONSTRAINT uq_point_detail_task_id UNIQUE (task_id, id);
      END IF;
    END $$;
    """)

    # Ensure all columns exist (idempotent)
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in ALL_COLS.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

    # Schema repair: game_winner_player_id must be TEXT
    _exec(conn, f"""
    DO $$
    DECLARE t text;
    BEGIN
      SELECT data_type INTO t FROM information_schema.columns
      WHERE table_schema = '{SILVER_SCHEMA}' AND table_name = '{TABLE}'
        AND column_name = 'game_winner_player_id';
      IF t = 'integer' THEN
        BEGIN
          ALTER TABLE {SILVER_SCHEMA}.{TABLE}
            ALTER COLUMN game_winner_player_id TYPE text USING game_winner_player_id::text;
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
      END IF;
    END $$;
    """)


def _resolve_two_players(conn: Connection, task_id: str) -> dict:
    """Find top 2 players by swing count. Fail if < 2."""
    rows = conn.execute(text("""
        SELECT player_id, COUNT(*) AS n
        FROM bronze.player_swing
        WHERE task_id::uuid = :tid
          AND COALESCE(valid, FALSE) = TRUE
          AND player_id IS NOT NULL
        GROUP BY player_id
        ORDER BY n DESC, player_id
        LIMIT 10
    """), {"tid": task_id}).fetchall()

    if len(rows) < 2:
        raise ValueError(f"Cannot resolve 2 players for task_id={task_id} (found {len(rows)})")

    p1, p2 = rows[0][0], rows[1][0]
    # Map ghost players to p2
    pid_map = {p1: p1, p2: p2}
    for pid, _ in rows[2:]:
        pid_map[pid] = p2

    return {"p1": str(p1), "p2": str(p2), "pid_map": pid_map}


# ============================================================
# PASS 1: Load from bronze (INSERT)
# ============================================================

def pass1_load(conn: Connection, task_id: str, cfg: dict) -> int:
    pf = _resolve_two_players(conn, task_id)

    # Build CASE for player de-ghosting
    params: dict = {"tid": task_id}
    case_lines = []
    for i, (src, dst) in enumerate(pf["pid_map"].items(), 1):
        params[f"src_{i}"] = src
        params[f"dst_{i}"] = dst
        case_lines.append(f"WHEN s.player_id = :src_{i} THEN :dst_{i}")

    pid_expr = ("CASE " + " ".join(case_lines) + " ELSE s.player_id END") if case_lines else "s.player_id"

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      id, task_id, player_id, valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      ball_hit_s, ball_hit_location_x, ball_hit_location_y
    )
    SELECT
      s.id::bigint,
      s.task_id::uuid,
      {pid_expr},
      COALESCE(s.valid, FALSE),
      COALESCE(s.serve, FALSE),
      s.swing_type,
      COALESCE(s.volley, FALSE),
      COALESCE(s.is_in_rally, FALSE),
      s.ball_player_distance::double precision,
      s.ball_speed::double precision,
      s.ball_impact_type,
      s.ball_hit_s,
      s.ball_hit_location_x,
      s.ball_hit_location_y
    FROM bronze.player_swing s
    WHERE s.task_id::uuid = :tid
      AND COALESCE(s.valid, FALSE) = TRUE
      AND COALESCE(s.is_in_rally, TRUE) = TRUE
    ON CONFLICT (task_id, id) DO NOTHING;
    """
    return conn.execute(text(sql), params).rowcount or 0


# ============================================================
# PASS 2: Bounce matching (UPDATE)
# ============================================================

def pass2_bounce(conn: Connection, task_id: str, cfg: dict) -> int:
    half_y = cfg["half_y"]
    sql = f"""
    WITH p AS (
      SELECT id, task_id, ball_hit_s, ball_hit_location_y,
             COALESCE(serve, FALSE) AS serve
      FROM {SILVER_SCHEMA}.{TABLE}
      WHERE task_id = :tid
    ),
    p_lead AS (
      SELECT p.*,
        LEAD(p.ball_hit_s) OVER (PARTITION BY p.task_id ORDER BY p.ball_hit_s, p.id) AS next_s
      FROM p
    ),
    p_win AS (
      SELECT p_lead.*,
        (p_lead.ball_hit_s + 0.005) AS win_lo,
        LEAST(COALESCE(p_lead.next_s, p_lead.ball_hit_s + 2.5), p_lead.ball_hit_s + 2.5) AS win_hi
      FROM p_lead
    ),
    chosen AS (
      SELECT w.id,
             b.type, b.timestamp, b.court_x, b.court_y
      FROM p_win w
      LEFT JOIN LATERAL (
        SELECT type, timestamp, court_x, court_y
        FROM bronze.ball_bounce b
        WHERE b.task_id::uuid = w.task_id
          AND w.ball_hit_s IS NOT NULL
          AND b.timestamp IS NOT NULL
          AND b.timestamp > w.win_lo
          AND b.timestamp <= w.win_hi
          AND (
            w.serve IS TRUE
            OR w.ball_hit_location_y IS NULL
            OR b.court_y IS NULL
            OR (w.ball_hit_location_y < :half_y AND b.court_y > :half_y)
            OR (w.ball_hit_location_y > :half_y AND b.court_y < :half_y)
          )
        ORDER BY (type = 'floor') DESC, timestamp
        LIMIT 1
      ) b ON TRUE
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET type = c.type, timestamp = c.timestamp, court_x = c.court_x, court_y = c.court_y
    FROM chosen c
    WHERE p.task_id = :tid AND p.id = c.id;
    """
    return conn.execute(text(sql), {"tid": task_id, "half_y": float(half_y)}).rowcount or 0


# ============================================================
# PASS 3: Point context mega-CTE (UPDATE)
#
# Single pass that computes ALL of:
#   serve_d, server_end_d, serve_side_d, point_number,
#   exclude_d, server_id, shot_ix_in_point, shot_phase_d,
#   shot_outcome_d, serve_try_ix_in_point, ace_d,
#   service_winner_d, point_winner_player_id,
#   game_number, game_winner_player_id,
#   set_number, set_game_number, point_key, serve_location
# ============================================================

def pass3_point_context(conn: Connection, task_id: str, cfg: dict) -> int:
    pf = _resolve_two_players(conn, task_id)
    p1, p2 = pf["p1"], pf["p2"]

    COURT_LEN       = cfg["court_length_m"]
    EPS             = cfg["eps_baseline_m"]
    SX_LEFT         = cfg["singles_left_x"]
    SX_RIGHT        = cfg["singles_right_x"]
    S_WIDTH         = cfg["singles_width"]
    HALF_Y          = cfg["half_y"]
    SVC_LINE        = cfg["service_line_m"]
    FAR_SVC_LINE    = cfg["far_service_line_m"]

    MID_X_DEFAULT   = SX_LEFT + S_WIDTH / 2.0
    HALF_W          = S_WIDTH / 2.0
    B1              = HALF_W / 4.0
    B2              = HALF_W / 2.0
    B3              = 3.0 * HALF_W / 4.0

    # Compute dynamic midline from serve hit locations
    mid_x_row = conn.execute(text(f"""
        SELECT COALESCE(AVG(ball_hit_location_x), :mid_default)
        FROM {SILVER_SCHEMA}.{TABLE}
        WHERE task_id = :tid
          AND ball_hit_location_x IS NOT NULL
          AND ball_hit_location_y IS NOT NULL
          AND COALESCE(serve, FALSE) IS TRUE
          AND lower(COALESCE(trim(swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
          AND (ball_hit_location_y < :eps OR ball_hit_location_y > (:y_max - :eps))
          AND ball_hit_location_x BETWEEN :sx_left AND :sx_right
    """), {
        "tid": task_id, "mid_default": float(MID_X_DEFAULT),
        "eps": float(EPS), "y_max": float(COURT_LEN),
        "sx_left": float(SX_LEFT), "sx_right": float(SX_RIGHT),
    }).scalar()
    mid_x = float(mid_x_row) if mid_x_row is not None else float(MID_X_DEFAULT)

    sql = f"""
    WITH
    -- ========== SERVE DETECTION ==========
    base AS (
      SELECT
        p.id, p.task_id, p.player_id,
        COALESCE(p.valid, FALSE) AS valid,
        COALESCE(p.serve, FALSE) AS serve,
        p.swing_type,
        COALESCE(p.volley, FALSE) AS volley,
        p.ball_hit_s,
        p.ball_hit_location_x AS x,
        p.ball_hit_location_y AS y,
        p.court_x, p.court_y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid AND p.ball_hit_s IS NOT NULL
    ),

    srv_detect AS (
      SELECT b.*,
        -- serve_d: geometric check
        CASE
          WHEN b.serve IS FALSE THEN FALSE
          WHEN lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
           AND b.y IS NOT NULL
           AND (b.y < :eps OR b.y > (:y_max - :eps))
          THEN TRUE
          ELSE FALSE
        END AS serve_d,
        -- server_end_d (raw)
        CASE
          WHEN b.serve IS FALSE THEN NULL
          WHEN lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
           AND b.y IS NOT NULL
           AND (b.y < :eps OR b.y > (:y_max - :eps))
          THEN CASE WHEN b.y < :eps THEN 'far' ELSE 'near' END
          ELSE NULL
        END AS server_end_raw
      FROM base b
    ),

    -- Forward-fill server_end
    srv_end AS (
      SELECT s.*,
        COALESCE(s.server_end_raw, (
          SELECT s2.server_end_raw FROM srv_detect s2
          WHERE s2.task_id = s.task_id AND s2.server_end_raw IS NOT NULL
            AND (s2.ball_hit_s < s.ball_hit_s OR (s2.ball_hit_s = s.ball_hit_s AND s2.id <= s.id))
          ORDER BY s2.ball_hit_s DESC, s2.id DESC LIMIT 1
        )) AS server_end_d
      FROM srv_detect s
    ),

    -- Serve side (deuce/ad) — only within singles bounds
    srv_side_raw AS (
      SELECT e.*,
        CASE
          WHEN e.serve_d IS NOT TRUE THEN NULL
          WHEN e.x IS NULL THEN NULL
          WHEN e.x NOT BETWEEN :sx_left AND :sx_right THEN NULL
          WHEN e.server_end_d IS NULL THEN NULL
          WHEN e.server_end_d = 'near' THEN
            CASE WHEN e.x > :mid THEN 'deuce' WHEN e.x < :mid THEN 'ad' ELSE 'deuce' END
          WHEN e.server_end_d = 'far' THEN
            CASE WHEN e.x < :mid THEN 'deuce' WHEN e.x > :mid THEN 'ad' ELSE 'deuce' END
          ELSE NULL
        END AS serve_side_raw
      FROM srv_end e
    ),

    -- Forward-fill serve_side
    srv_side AS (
      SELECT s.*,
        COALESCE(s.serve_side_raw, (
          SELECT s0.serve_side_raw FROM srv_side_raw s0
          WHERE s0.task_id = s.task_id AND s0.serve_side_raw IS NOT NULL
            AND (s0.ball_hit_s < s.ball_hit_s OR (s0.ball_hit_s = s.ball_hit_s AND s0.id <= s.id))
          ORDER BY s0.ball_hit_s DESC, s0.id DESC LIMIT 1
        )) AS serve_side_d
      FROM srv_side_raw s
    ),

    -- ========== POINT NUMBERING ==========
    -- Anchors: first serves (p1/p2 only) where serve_side is known
    anchors AS (
      SELECT s.id, s.task_id, s.ball_hit_s, s.player_id, s.serve_side_d,
        LAG(s.serve_side_d) OVER (PARTITION BY s.task_id ORDER BY s.ball_hit_s, s.id) AS prev_side,
        LAG(s.player_id) OVER (PARTITION BY s.task_id ORDER BY s.ball_hit_s, s.id) AS prev_pid
      FROM srv_side s
      WHERE s.serve_d IS TRUE AND s.serve_side_d IS NOT NULL
        AND s.player_id IN (:p1, :p2)
    ),

    point_anchors AS (
      SELECT a.id, a.task_id, a.ball_hit_s,
        1 + SUM(CASE
          WHEN a.prev_side IS DISTINCT FROM a.serve_side_d THEN 1
          WHEN a.prev_pid IS DISTINCT FROM a.player_id THEN 1
          ELSE 0
        END) OVER (PARTITION BY a.task_id ORDER BY a.ball_hit_s, a.id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::integer AS point_number
      FROM anchors a
    ),

    -- Forward-fill point_number to all rows
    with_point AS (
      SELECT s.*,
        MAX(pa.point_number) OVER (
          PARTITION BY s.task_id ORDER BY s.ball_hit_s, s.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )::integer AS point_number
      FROM srv_side s
      LEFT JOIN point_anchors pa ON pa.id = s.id
    ),

    -- ========== SERVE TRY (1st/2nd) ==========
    serve_seq AS (
      SELECT w.id, w.task_id, w.point_number,
        ROW_NUMBER() OVER (
          PARTITION BY w.task_id, w.point_number ORDER BY w.ball_hit_s, w.id
        ) AS srv_rn
      FROM with_point w
      WHERE w.serve_d IS TRUE AND w.point_number IS NOT NULL
    ),

    with_try AS (
      SELECT w.*,
        CASE
          WHEN w.serve_d IS NOT TRUE THEN NULL
          WHEN ss.srv_rn = 1 THEN '1st'
          ELSE '2nd'
        END AS serve_try_raw
      FROM with_point w
      LEFT JOIN serve_seq ss ON ss.id = w.id
    ),

    -- Forward-fill serve_try within point
    with_try_ff AS (
      SELECT t.*,
        COALESCE(t.serve_try_raw,
          MAX(t.serve_try_raw) OVER (
            PARTITION BY t.task_id, t.point_number ORDER BY t.ball_hit_s, t.id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
          )
        ) AS serve_try_ix_in_point
      FROM with_try t
    ),

    -- ========== EXCLUSIONS ==========
    last_serve_per_point AS (
      SELECT task_id, point_number,
        MAX(CASE WHEN serve_d THEN ball_hit_s END) AS last_serve_s
      FROM with_try_ff
      WHERE point_number > 0
      GROUP BY task_id, point_number
    ),

    excl_base AS (
      SELECT w.id, w.task_id, w.point_number, w.ball_hit_s,
        w.serve_d, w.x, w.y,
        ls.last_serve_s,
        LAG(w.ball_hit_s) OVER (
          PARTITION BY w.task_id, w.point_number ORDER BY w.ball_hit_s, w.id
        ) AS prev_s
      FROM with_try_ff w
      LEFT JOIN last_serve_per_point ls
        ON ls.task_id = w.task_id AND ls.point_number = w.point_number
      WHERE w.point_number > 0
    ),

    excl_flags AS (
      SELECT e.id,
        (NOT e.serve_d AND e.last_serve_s IS NOT NULL AND e.ball_hit_s < e.last_serve_s) AS r1,
        (e.prev_s IS NOT NULL AND e.last_serve_s IS NOT NULL
         AND e.ball_hit_s > e.last_serve_s AND (e.ball_hit_s - e.prev_s) > 5.0) AS gap_break,
        (NOT e.serve_d AND e.x IS NULL AND e.y IS NULL) AS r3
      FROM excl_base e
    ),

    excl_chain AS (
      SELECT f.id,
        (f.r1 OR BOOL_OR(f.gap_break) OVER (
          PARTITION BY w.task_id, w.point_number ORDER BY w.ball_hit_s, w.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) OR f.r3) AS exclude_d
      FROM excl_flags f
      JOIN with_try_ff w ON w.id = f.id
    ),

    -- ========== GAME NUMBERING ==========
    -- Anchors: first serves where server changes (p1/p2 only)
    game_anchors AS (
      SELECT a.id, a.task_id, a.ball_hit_s, a.player_id,
        LAG(a.player_id) OVER (PARTITION BY a.task_id ORDER BY a.ball_hit_s, a.id) AS prev_pid
      FROM anchors a
      WHERE a.ball_hit_s IS NOT NULL
    ),

    game_nums AS (
      SELECT g.id,
        1 + SUM(CASE WHEN g.prev_pid IS DISTINCT FROM g.player_id THEN 1 ELSE 0 END)
          OVER (PARTITION BY g.task_id ORDER BY g.ball_hit_s, g.id
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::integer AS game_number
      FROM game_anchors g
    ),

    with_game AS (
      SELECT w.*,
        MAX(gn.game_number) OVER (
          PARTITION BY w.task_id ORDER BY w.ball_hit_s, w.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )::integer AS game_number
      FROM with_try_ff w
      LEFT JOIN game_nums gn ON gn.id = w.id
    ),

    -- ========== SERVER_ID ==========
    first_serve_per_point AS (
      SELECT DISTINCT ON (task_id, point_number)
        task_id, point_number, player_id AS server_id
      FROM with_game
      WHERE serve_d IS TRUE AND point_number > 0 AND ball_hit_s IS NOT NULL
      ORDER BY task_id, point_number, ball_hit_s, id
    ),

    -- ========== SHOT INDEX + PHASE + OUTCOME ==========
    shot_base AS (
      SELECT w.id, w.task_id, w.point_number, w.ball_hit_s,
        w.serve_d, w.player_id, w.valid, w.swing_type, w.volley,
        w.x, w.y, w.court_x, w.court_y,
        w.serve_side_d, w.server_end_d, w.serve_try_ix_in_point,
        w.game_number,
        COALESCE(ec.exclude_d, FALSE) AS exclude_d,
        fs.server_id,
        -- last serve time per point (for shot_ix anchor)
        MAX(CASE WHEN w.serve_d AND COALESCE(ec.exclude_d, FALSE) IS NOT TRUE THEN w.ball_hit_s END)
          OVER (PARTITION BY w.task_id, w.point_number) AS last_serve_s_pt
      FROM with_game w
      LEFT JOIN excl_chain ec ON ec.id = w.id
      LEFT JOIN first_serve_per_point fs ON fs.task_id = w.task_id AND fs.point_number = w.point_number
    ),

    shot_indexed AS (
      SELECT sb.*,
        CASE
          WHEN sb.exclude_d THEN NULL
          WHEN sb.point_number IS NULL OR sb.point_number <= 0 THEN NULL
          WHEN sb.last_serve_s_pt IS NULL THEN NULL
          WHEN sb.ball_hit_s < sb.last_serve_s_pt THEN NULL
          ELSE ROW_NUMBER() OVER (
            PARTITION BY sb.task_id, sb.point_number,
              (CASE WHEN sb.ball_hit_s >= sb.last_serve_s_pt AND NOT sb.exclude_d THEN 1 ELSE 0 END)
            ORDER BY sb.ball_hit_s, sb.id
          )
        END AS shot_ix_in_point
      FROM shot_base sb
    ),

    shot_phased AS (
      SELECT si.*,
        CASE
          WHEN si.exclude_d OR si.shot_ix_in_point IS NULL THEN NULL
          WHEN si.serve_d IS TRUE THEN 'Serve'
          WHEN si.shot_ix_in_point = 2 THEN 'Return'
          WHEN si.y IS NULL THEN NULL
          WHEN si.y < 0 OR si.y > :y_max THEN 'Rally'
          WHEN si.y > :svc AND si.y < :far_svc THEN 'Net'
          ELSE 'Transition'
        END AS shot_phase_d
      FROM shot_indexed si
    ),

    -- Last shot per point
    last_shot AS (
      SELECT task_id, point_number,
        MAX(shot_ix_in_point) AS last_ix
      FROM shot_phased
      WHERE point_number > 0 AND NOT exclude_d AND shot_ix_in_point IS NOT NULL
      GROUP BY task_id, point_number
    ),

    shot_outcome AS (
      SELECT sp.*,
        CASE
          WHEN sp.shot_ix_in_point IS NULL THEN NULL
          WHEN sp.shot_ix_in_point < ls.last_ix THEN 'In'
          WHEN sp.shot_ix_in_point = ls.last_ix THEN
            CASE
              WHEN sp.court_x IS NULL OR sp.court_y IS NULL THEN 'Error'
              WHEN sp.court_x < :sx_left OR sp.court_x > :sx_right THEN 'Error'
              WHEN sp.court_y < 0 OR sp.court_y > :y_max THEN 'Error'
              WHEN sp.serve_d IS TRUE AND ABS(sp.court_y - :half_y) <= 1.60 THEN 'Error'
              WHEN sp.serve_d IS NOT TRUE AND sp.y IS NOT NULL
               AND sp.y < :half_y AND sp.court_y <= :half_y THEN 'Error'
              WHEN sp.serve_d IS NOT TRUE AND sp.y IS NOT NULL
               AND sp.y > :half_y AND sp.court_y >= :half_y THEN 'Error'
              ELSE 'Winner'
            END
          ELSE NULL
        END AS shot_outcome_d
      FROM shot_phased sp
      LEFT JOIN last_shot ls ON ls.task_id = sp.task_id AND ls.point_number = sp.point_number
    ),

    -- ========== SERVE LABELS (ace, double, service_winner) ==========
    -- Last valid row per point
    last_valid AS (
      SELECT DISTINCT ON (task_id, point_number)
        task_id, point_number, id AS last_id, player_id AS last_pid,
        serve_d AS last_serve_d, shot_outcome_d AS last_outcome,
        court_x AS last_cx, court_y AS last_cy,
        serve_try_ix_in_point AS last_try
      FROM shot_outcome
      WHERE NOT exclude_d AND valid AND point_number > 0
      ORDER BY task_id, point_number, ball_hit_s DESC NULLS LAST, id DESC
    ),

    -- First return (non-serve by opponent) per point
    first_return AS (
      SELECT DISTINCT ON (so.task_id, so.point_number)
        so.task_id, so.point_number,
        so.id AS return_id, so.player_id AS returner_id,
        so.shot_outcome_d AS return_outcome
      FROM shot_outcome so
      JOIN first_serve_per_point fs
        ON fs.task_id = so.task_id AND fs.point_number = so.point_number
      WHERE NOT so.exclude_d AND so.valid AND so.serve_d IS NOT TRUE
        AND so.player_id <> fs.server_id
      ORDER BY so.task_id, so.point_number, so.ball_hit_s, so.id
    ),

    -- Non-serve row count per point
    non_serve_count AS (
      SELECT task_id, point_number,
        COUNT(*) FILTER (WHERE NOT exclude_d AND valid AND serve_d IS NOT TRUE) AS n
      FROM shot_outcome
      WHERE point_number > 0
      GROUP BY task_id, point_number
    ),

    -- Double fault: 2nd serve is last row + invalid bounce
    double_pts AS (
      SELECT lv.task_id, lv.point_number
      FROM last_valid lv
      WHERE lv.last_serve_d IS TRUE
        AND lower(COALESCE(lv.last_try, '')) = '2nd'
        AND (
          lv.last_cx IS NULL OR lv.last_cy IS NULL
          OR lv.last_cx < :sx_left OR lv.last_cx > :sx_right
          OR lv.last_cy < 0 OR lv.last_cy > :y_max
          OR ABS(lv.last_cy - :half_y) <= 1.60
        )
    ),

    -- Ace: serve wins, no opponent return
    ace_pts AS (
      SELECT fs.task_id, fs.point_number
      FROM first_serve_per_point fs
      JOIN last_valid lv ON lv.task_id = fs.task_id AND lv.point_number = fs.point_number
      JOIN non_serve_count nsc ON nsc.task_id = fs.task_id AND nsc.point_number = fs.point_number
      LEFT JOIN double_pts dp ON dp.task_id = fs.task_id AND dp.point_number = fs.point_number
      WHERE dp.point_number IS NULL
        AND lv.last_id = (SELECT so2.id FROM shot_outcome so2
                          WHERE so2.task_id = fs.task_id AND so2.point_number = fs.point_number
                            AND so2.serve_d IS TRUE AND NOT so2.exclude_d AND so2.valid
                          ORDER BY so2.ball_hit_s DESC, so2.id DESC LIMIT 1)
        AND lower(COALESCE(lv.last_outcome, '')) = 'winner'
        AND nsc.n = 0
    ),

    -- Service winner: serve + opponent return is error
    svc_winner_pts AS (
      SELECT fs.task_id, fs.point_number
      FROM first_serve_per_point fs
      JOIN first_return fr ON fr.task_id = fs.task_id AND fr.point_number = fs.point_number
      JOIN last_valid lv ON lv.task_id = fs.task_id AND lv.point_number = fs.point_number
      LEFT JOIN double_pts dp ON dp.task_id = fs.task_id AND dp.point_number = fs.point_number
      WHERE dp.point_number IS NULL
        AND lv.last_id = fr.return_id
        AND lower(COALESCE(fr.return_outcome, '')) = 'error'
    ),

    -- ========== POINT WINNER ==========
    -- Server per point (for receiver resolution)
    point_receiver AS (
      SELECT fs.task_id, fs.point_number,
        CASE WHEN fs.server_id = :p1 THEN :p2
             WHEN fs.server_id = :p2 THEN :p1 ELSE NULL END AS receiver_id
      FROM first_serve_per_point fs
    ),

    point_winner AS (
      SELECT fs.task_id, fs.point_number,
        CASE
          WHEN EXISTS (SELECT 1 FROM double_pts d WHERE d.task_id = fs.task_id AND d.point_number = fs.point_number)
            THEN pr.receiver_id
          WHEN lower(COALESCE(lv.last_outcome, '')) = 'winner' THEN lv.last_pid
          WHEN lv.last_pid = :p1 THEN :p2
          WHEN lv.last_pid = :p2 THEN :p1
          ELSE NULL
        END AS winner_pid
      FROM first_serve_per_point fs
      LEFT JOIN point_receiver pr ON pr.task_id = fs.task_id AND pr.point_number = fs.point_number
      LEFT JOIN last_valid lv ON lv.task_id = fs.task_id AND lv.point_number = fs.point_number
    ),

    -- ========== GAME WINNER ==========
    last_point_per_game AS (
      SELECT DISTINCT ON (task_id, game_number)
        task_id, game_number, point_number
      FROM shot_outcome
      WHERE point_number > 0 AND game_number > 0
      ORDER BY task_id, game_number, ball_hit_s DESC NULLS LAST, id DESC
    ),

    game_winner AS (
      SELECT lp.task_id, lp.game_number,
        pw.winner_pid AS game_winner_pid
      FROM last_point_per_game lp
      LEFT JOIN point_winner pw ON pw.task_id = lp.task_id AND pw.point_number = lp.point_number
    ),

    -- ========== SET NUMBER ==========
    set_info AS (
      SELECT
        sc.task_id::uuid AS task_id,
        (COALESCE(sc.player_a_set1_games,0) + COALESCE(sc.player_b_set1_games,0))::int AS g1,
        (COALESCE(sc.player_a_set2_games,0) + COALESCE(sc.player_b_set2_games,0))::int AS g2,
        (COALESCE(sc.player_a_set3_games,0) + COALESCE(sc.player_b_set3_games,0))::int AS g3
      FROM bronze.submission_context sc
      WHERE sc.task_id::uuid = :tid
      LIMIT 1
    ),

    -- ========== SERVE LOCATION (1-8) ==========
    srv_loc AS (
      SELECT so.id,
        CASE
          WHEN so.serve_d IS NOT TRUE THEN NULL
          WHEN so.server_end_d NOT IN ('near','far') THEN NULL
          WHEN so.serve_side_d NOT IN ('deuce','ad') THEN NULL
          WHEN so.court_x IS NULL OR so.court_x < :sx_left OR so.court_x > :sx_right THEN
            CASE WHEN so.serve_side_d = 'deuce' THEN 2 ELSE 7 END
          -- near+deuce
          WHEN so.server_end_d = 'near' AND so.serve_side_d = 'deuce' THEN
            CASE WHEN (so.court_x - :sx_left) < :b1 THEN 1
                 WHEN (so.court_x - :sx_left) < :b2 THEN 2
                 WHEN (so.court_x - :sx_left) < :b3 THEN 3 ELSE 4 END
          -- near+ad
          WHEN so.server_end_d = 'near' AND so.serve_side_d = 'ad' THEN
            CASE WHEN (so.court_x - :mid) < :b1 THEN 5
                 WHEN (so.court_x - :mid) < :b2 THEN 6
                 WHEN (so.court_x - :mid) < :b3 THEN 7 ELSE 8 END
          -- far+deuce
          WHEN so.server_end_d = 'far' AND so.serve_side_d = 'deuce' THEN
            CASE WHEN (:sx_right - so.court_x) < :b1 THEN 1
                 WHEN (:sx_right - so.court_x) < :b2 THEN 2
                 WHEN (:sx_right - so.court_x) < :b3 THEN 3 ELSE 4 END
          -- far+ad
          WHEN so.server_end_d = 'far' AND so.serve_side_d = 'ad' THEN
            CASE WHEN (:mid - so.court_x) < :b1 THEN 5
                 WHEN (:mid - so.court_x) < :b2 THEN 6
                 WHEN (:mid - so.court_x) < :b3 THEN 7 ELSE 8 END
          ELSE NULL
        END AS serve_location_raw
      FROM shot_outcome so
    ),

    -- Forward-fill serve_location
    srv_loc_ff AS (
      SELECT so.id,
        MAX(sl.serve_location_raw) OVER (
          PARTITION BY so.task_id,
            COUNT(sl.serve_location_raw) OVER (
              PARTITION BY so.task_id ORDER BY so.ball_hit_s, so.id
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            )
        ) AS serve_location
      FROM shot_outcome so
      LEFT JOIN srv_loc sl ON sl.id = so.id
    ),

    -- ========== FINAL ASSEMBLY ==========
    final AS (
      SELECT
        so.id,
        so.serve_d,
        so.server_end_d,
        so.serve_side_d,
        so.point_number,
        so.game_number,

        COALESCE(ec.exclude_d, FALSE) AS exclude_d,

        CASE
          WHEN EXISTS (SELECT 1 FROM double_pts d WHERE d.task_id = so.task_id AND d.point_number = so.point_number)
          THEN 'Double'
          ELSE so.serve_try_ix_in_point
        END AS serve_try_ix_in_point,

        EXISTS (SELECT 1 FROM ace_pts a WHERE a.task_id = so.task_id AND a.point_number = so.point_number) AS ace_d,
        EXISTS (SELECT 1 FROM svc_winner_pts sv WHERE sv.task_id = so.task_id AND sv.point_number = so.point_number) AS service_winner_d,

        so.shot_ix_in_point,
        so.shot_phase_d,
        so.shot_outcome_d,

        fs.server_id,
        pw.winner_pid AS point_winner_player_id,
        gw.game_winner_pid AS game_winner_player_id,

        so.task_id::text || '|' || LPAD(COALESCE(so.point_number,0)::text, 4, '0')
          || '|' || COALESCE(fs.server_id::text, '') AS point_key,

        slf.serve_location,

        -- set_number
        CASE
          WHEN so.game_number IS NULL OR so.game_number <= 0 THEN NULL
          WHEN si.g1 IS NULL OR si.g1 <= 0 THEN NULL
          WHEN so.game_number <= si.g1 THEN 1
          WHEN (si.g1 + si.g2) > si.g1 AND so.game_number <= (si.g1 + si.g2) THEN 2
          WHEN (si.g1 + si.g2 + si.g3) > (si.g1 + si.g2) AND so.game_number <= (si.g1 + si.g2 + si.g3) THEN 3
          ELSE NULL
        END AS set_number,
        CASE
          WHEN so.game_number IS NULL OR so.game_number <= 0 THEN NULL
          WHEN si.g1 IS NULL OR si.g1 <= 0 THEN NULL
          WHEN so.game_number <= si.g1 THEN so.game_number
          WHEN (si.g1 + si.g2) > si.g1 AND so.game_number <= (si.g1 + si.g2) THEN so.game_number - si.g1
          WHEN (si.g1 + si.g2 + si.g3) > (si.g1 + si.g2) AND so.game_number <= (si.g1 + si.g2 + si.g3) THEN so.game_number - si.g1 - si.g2
          ELSE NULL
        END AS set_game_number

      FROM shot_outcome so
      LEFT JOIN excl_chain ec ON ec.id = so.id
      LEFT JOIN first_serve_per_point fs ON fs.task_id = so.task_id AND fs.point_number = so.point_number
      LEFT JOIN point_winner pw ON pw.task_id = so.task_id AND pw.point_number = so.point_number
      LEFT JOIN game_winner gw ON gw.task_id = so.task_id AND gw.game_number = so.game_number
      LEFT JOIN srv_loc_ff slf ON slf.id = so.id
      LEFT JOIN set_info si ON TRUE
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d = f.serve_d,
      server_end_d = f.server_end_d,
      serve_side_d = f.serve_side_d,
      point_number = f.point_number,
      game_number = f.game_number,
      exclude_d = f.exclude_d,
      serve_try_ix_in_point = f.serve_try_ix_in_point,
      ace_d = f.ace_d,
      service_winner_d = f.service_winner_d,
      shot_ix_in_point = f.shot_ix_in_point,
      shot_phase_d = f.shot_phase_d,
      shot_outcome_d = f.shot_outcome_d,
      server_id = f.server_id,
      point_winner_player_id = f.point_winner_player_id,
      game_winner_player_id = f.game_winner_player_id,
      point_key = f.point_key,
      serve_location = f.serve_location,
      set_number = f.set_number,
      set_game_number = f.set_game_number
    FROM final f
    WHERE p.task_id = :tid AND p.id = f.id;
    """

    return conn.execute(text(sql), {
        "tid": task_id,
        "p1": p1, "p2": p2,
        "eps": float(EPS), "y_max": float(COURT_LEN),
        "sx_left": float(SX_LEFT), "sx_right": float(SX_RIGHT),
        "mid": float(mid_x),
        "half_y": float(HALF_Y),
        "svc": float(SVC_LINE), "far_svc": float(FAR_SVC_LINE),
        "b1": float(B1), "b2": float(B2), "b3": float(B3),
    }).rowcount or 0


# ============================================================
# PASS 4: Zones + Normalization (UPDATE)
# ============================================================

def pass4_zones_and_normalize(conn: Connection, task_id: str, cfg: dict) -> int:
    SX_LEFT  = cfg["singles_left_x"]
    SX_RIGHT = cfg["singles_right_x"]
    HALF_Y   = cfg["half_y"]
    COURT_LEN = cfg["court_length_m"]
    DOUBLES_W = cfg["doubles_width_m"]

    # Zone boundaries
    z2 = SX_LEFT + cfg["singles_width"] / 4.0      # 3.4275
    z3 = SX_LEFT + cfg["singles_width"] / 2.0      # 5.485
    z4 = SX_LEFT + 3.0 * cfg["singles_width"] / 4.0  # 7.5425

    sql = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      -- Rally location (hit): A-D based on hitter side
      rally_location_hit =
        CASE
          WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
          WHEN p.ball_hit_location_x IS NULL OR p.ball_hit_location_y IS NULL THEN NULL
          WHEN p.ball_hit_location_y < :half_y THEN
            CASE WHEN p.ball_hit_location_x < :z2 THEN 'A'
                 WHEN p.ball_hit_location_x < :z3 THEN 'B'
                 WHEN p.ball_hit_location_x < :z4 THEN 'C' ELSE 'D' END
          ELSE
            CASE WHEN p.ball_hit_location_x < :z2 THEN 'D'
                 WHEN p.ball_hit_location_x < :z3 THEN 'C'
                 WHEN p.ball_hit_location_x < :z4 THEN 'B' ELSE 'A' END
        END,

      -- Rally location (bounce): A-D based on bounce side; fallback to hit location
      rally_location_bounce =
        CASE
          WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
          WHEN p.court_x IS NULL THEN
            -- Fallback: use hit-based classification
            CASE
              WHEN p.ball_hit_location_x IS NULL OR p.ball_hit_location_y IS NULL THEN NULL
              WHEN p.ball_hit_location_y < :half_y THEN
                CASE WHEN p.ball_hit_location_x < :z2 THEN 'A'
                     WHEN p.ball_hit_location_x < :z3 THEN 'B'
                     WHEN p.ball_hit_location_x < :z4 THEN 'C' ELSE 'D' END
              ELSE
                CASE WHEN p.ball_hit_location_x < :z2 THEN 'D'
                     WHEN p.ball_hit_location_x < :z3 THEN 'C'
                     WHEN p.ball_hit_location_x < :z4 THEN 'B' ELSE 'A' END
            END
          WHEN p.court_y IS NULL THEN NULL
          WHEN p.court_y < :half_y THEN
            CASE WHEN p.court_x < :sx_left THEN 'A'
                 WHEN p.court_x < :z2 THEN 'A'
                 WHEN p.court_x < :z3 THEN 'B'
                 WHEN p.court_x < :z4 THEN 'C' ELSE 'D' END
          ELSE
            CASE WHEN p.court_x < :sx_left THEN 'D'
                 WHEN p.court_x < :z2 THEN 'D'
                 WHEN p.court_x < :z3 THEN 'C'
                 WHEN p.court_x < :z4 THEN 'B' ELSE 'A' END
        END,

      -- Invert flags (consistent boundary: < for far side)
      invert_hit = CASE
        WHEN p.ball_hit_location_y IS NOT NULL AND p.ball_hit_location_y < :half_y THEN TRUE
        ELSE FALSE END,

      invert_bounce = CASE
        WHEN p.ball_hit_location_y IS NOT NULL AND p.ball_hit_location_y > :half_y THEN TRUE
        ELSE FALSE END,

      -- Normalized hit coordinates
      ball_hit_x_norm = CASE
        WHEN p.ball_hit_location_x IS NULL THEN NULL
        WHEN p.ball_hit_location_y IS NOT NULL AND p.ball_hit_location_y < :half_y
          THEN :court_w - p.ball_hit_location_x
        ELSE p.ball_hit_location_x END,

      ball_hit_y_norm = CASE
        WHEN p.ball_hit_location_y IS NULL THEN NULL
        WHEN p.ball_hit_location_y < :half_y
          THEN :court_len - p.ball_hit_location_y
        ELSE p.ball_hit_location_y END,

      -- Normalized bounce coordinates
      ball_bounce_x_norm = CASE
        WHEN p.court_x IS NULL THEN NULL
        WHEN p.ball_hit_location_y IS NOT NULL AND p.ball_hit_location_y > :half_y
          THEN :court_w - p.court_x
        ELSE p.court_x END,

      ball_bounce_y_norm = CASE
        WHEN p.court_y IS NULL THEN NULL
        WHEN p.ball_hit_location_y IS NOT NULL AND p.ball_hit_location_y > :half_y
          THEN :court_len - p.court_y
        ELSE p.court_y END

    WHERE p.task_id = :tid;
    """
    return conn.execute(text(sql), {
        "tid": task_id,
        "half_y": float(HALF_Y),
        "sx_left": float(SX_LEFT),
        "z2": float(z2), "z3": float(z3), "z4": float(z4),
        "court_len": float(COURT_LEN),
        "court_w": float(DOUBLES_W),
    }).rowcount or 0


# ============================================================
# PASS 5: Analytics (UPDATE)
# ============================================================

def pass5_analytics(conn: Connection, task_id: str, cfg: dict) -> int:
    sql = f"""
    WITH rl AS (
      SELECT p.id,
        -- Rally length per point (max shot_ix - 1)
        MAX(CASE WHEN p.shot_ix_in_point IS NULL THEN NULL
                 WHEN p.shot_ix_in_point = 1 THEN 0
                 ELSE p.shot_ix_in_point - 1 END)
          OVER (PARTITION BY p.task_id, p.point_key) AS rally_length_point,
        -- Global shot sequence
        ROW_NUMBER() OVER (
          PARTITION BY p.task_id
          ORDER BY p.ball_hit_s, p.player_id, p.shot_ix_in_point, p.timestamp, p.id
        ) AS shot_q
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_bucket_d = CASE
        WHEN p.serve_location IN (1, 8) THEN 'wide'
        WHEN p.serve_location IN (2, 3, 6, 7) THEN 'body'
        WHEN p.serve_location IN (4, 5) THEN 'T'
        ELSE NULL END,

      stroke_d = CASE
        WHEN p.serve_d IS TRUE THEN 'Serve'
        WHEN p.volley IS TRUE THEN 'Volley'
        WHEN lower(COALESCE(p.swing_type,'')) IN ('fh_overhead','bh_overhead','overhead','smash') THEN 'Overhead'
        WHEN lower(COALESCE(p.swing_type,'')) = 'fh' THEN 'Forehand'
        WHEN lower(COALESCE(p.swing_type,'')) IN ('2h_bh','1h_bh') THEN 'Backhand'
        WHEN lower(COALESCE(p.swing_type,'')) IN ('slice','bh_slice','fh_slice') THEN 'Slice'
        ELSE 'Other' END,

      rally_length = CASE
        WHEN p.shot_ix_in_point IS NULL THEN NULL
        WHEN p.shot_ix_in_point = 1 THEN 0
        ELSE p.shot_ix_in_point - 1 END,

      rally_length_point = rl.rally_length_point,

      rally_length_bucket_d = CASE
        WHEN rl.rally_length_point IS NULL THEN NULL
        WHEN rl.rally_length_point <= 4 THEN '0\u20134 shots'
        WHEN rl.rally_length_point <= 8 THEN '5\u20138 shots'
        ELSE '9+ shots' END,

      aggression_d = CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        WHEN p.ball_hit_y_norm IS NULL THEN NULL
        WHEN p.ball_hit_y_norm <= 24 THEN 'Attack'
        WHEN p.ball_hit_y_norm > 24 AND p.ball_hit_y_norm < 26 THEN 'Neutral'
        WHEN p.ball_hit_y_norm >= 26 THEN 'Defence'
        ELSE NULL END,

      depth_d = CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        WHEN p.ball_bounce_y_norm IS NULL THEN NULL
        WHEN p.ball_bounce_y_norm > 20 THEN 'Deep'
        WHEN p.ball_bounce_y_norm > 18 AND p.ball_bounce_y_norm <= 20 THEN 'Middle'
        WHEN p.ball_bounce_y_norm <= 18 THEN 'Short'
        ELSE NULL END,

      shot_q = rl.shot_q,
      shot_key_q = p.task_id::text || '|' || rl.shot_q::text

    FROM rl
    WHERE p.task_id = :tid AND p.id = rl.id;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


# ============================================================
# VALIDATION
# ============================================================

def _validate_rally_count(conn: Connection, task_id: str) -> Dict:
    silver_points = conn.execute(text(f"""
        SELECT COUNT(DISTINCT point_number)
        FROM {SILVER_SCHEMA}.{TABLE}
        WHERE task_id = :tid AND point_number IS NOT NULL AND point_number > 0
    """), {"tid": task_id}).scalar() or 0

    bronze_rallies = conn.execute(text("""
        SELECT COUNT(*) FROM bronze.rally WHERE task_id = :tid
    """), {"tid": task_id}).scalar() or 0

    result: Dict = {
        "validation_silver_points": int(silver_points),
        "validation_bronze_rallies": int(bronze_rallies),
    }

    if bronze_rallies > 0 and silver_points > 0:
        ratio = abs(silver_points - bronze_rallies) / max(silver_points, bronze_rallies)
        result["validation_divergence_pct"] = round(ratio * 100, 1)
        if ratio > 0.30:
            log.warning(
                "RALLY VALIDATION WARNING task_id=%s: silver_points=%d vs bronze_rallies=%d (%.1f%%)",
                task_id, silver_points, bronze_rallies, ratio * 100,
            )
            result["validation_warning"] = (
                f"silver points ({silver_points}) vs bronze rallies ({bronze_rallies}) "
                f"diverge by {result['validation_divergence_pct']}%"
            )
    return result


# ============================================================
# ORCHESTRATOR
# ============================================================

def build_silver_v2(task_id: str, replace: bool = False) -> Dict:
    if not task_id:
        raise ValueError("task_id is required")

    out: Dict = {"ok": True, "task_id": task_id}

    with engine.begin() as conn:
        ensure_schema(conn)

        # Resolve sport_type
        row = conn.execute(text("""
            SELECT sport_type FROM bronze.submission_context WHERE task_id = :tid LIMIT 1
        """), {"tid": task_id}).mappings().first()
        sport_type = (row["sport_type"] if row and row.get("sport_type") else DEFAULT_SPORT_TYPE)
        cfg = SPORT_CONFIG.get(sport_type, SPORT_CONFIG[DEFAULT_SPORT_TYPE])
        out["sport_type"] = sport_type

        # Confidence quality gate
        conf_row = conn.execute(text("""
            SELECT tracking_confidence, court_detection_confidence
            FROM bronze.session_confidences WHERE task_id = :tid LIMIT 1
        """), {"tid": task_id}).mappings().first()
        if conf_row:
            tc = conf_row.get("tracking_confidence")
            cc = conf_row.get("court_detection_confidence")
            if tc is not None:
                out["tracking_confidence"] = float(tc)
            if cc is not None:
                out["court_detection_confidence"] = float(cc)
            if tc is not None and float(tc) < 0.5:
                log.warning("LOW TRACKING CONFIDENCE task_id=%s tc=%.3f", task_id, float(tc))
                out["confidence_warning"] = f"tracking_confidence={float(tc):.3f} below 0.5"

        # Clean slate
        if replace:
            _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})

        out["pass1_rows"] = pass1_load(conn, task_id, cfg)
        out["pass2_rows"] = pass2_bounce(conn, task_id, cfg)
        out["pass3_rows"] = pass3_point_context(conn, task_id, cfg)
        out["pass4_rows"] = pass4_zones_and_normalize(conn, task_id, cfg)
        out["pass5_rows"] = pass5_analytics(conn, task_id, cfg)

        out.update(_validate_rally_count(conn, task_id))

    return out


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse, json

    p = argparse.ArgumentParser(description="Silver point_detail v2 — 5-pass rewrite")
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument("--replace", action="store_true", help="delete existing rows before rebuild")
    args = p.parse_args()

    result = build_silver_v2(args.task_id, replace=args.replace)
    print(json.dumps(result, indent=2, default=str))