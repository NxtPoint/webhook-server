"""silver_analytics — SportAI insight tables beyond point_detail.

Three new silver grains, built from bronze data that `build_silver_v2` never
reads (`player`, `player_position`, `session_confidences`, `team_session`):

  silver.match_player_summary   one row per (task_id, player_id)
                                fitness (distance / sprint / activity), shot mix,
                                movement summary (avg position, court coverage,
                                depth thirds), near/far end.
  silver.player_movement_grid   one row per (task_id, player_id, cell)
                                a PRE-AGGREGATED court occupancy grid — the
                                heatmap source. ~60-150 rows per player, NOT the
                                ~3000 raw position rows, so a chart never crunches
                                the raw stream (the performance guard).
  silver.match_quality          one row per task_id
                                SportAI's own ball/pose/swing/final confidence +
                                a derived reliability tier — the "is this match's
                                analytics trustworthy" gate.

Design rules (mirror the filter contract):
  - Bronze owns the facts; these tables inherit/aggregate, they do not re-derive
    point/game structure. They never touch silver.point_detail (the 18/18 layer).
  - Player identity reuses build_silver_v2._resolve_two_players, so movement and
    fitness map to the SAME two players as point_detail (ghosts fold into p2).
  - Aggregation lives in SQL, not Python (architecture rule #2).
  - Idempotent: DELETE (task_id, model) then INSERT — safe to re-run.
  - `model` column ('sportai' now) leaves room for a T5 build later.

NOT wired into the prod ingest yet — build + validate first, wire second.
Run per task: `build_all(engine, task_id)` (see harness / ops wiring, TBD).
"""
import logging
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

log = logging.getLogger(__name__)

SILVER = "silver"
HALF_Y = 11.885            # net line (court_y metres)
HALF_LEN = 11.885          # half-court length, net -> baseline
# Movement grid resolution (metres). 1.0 m cells collapse ~3000 raw positions to
# ~60-150 occupied cells per player — heatmap-ready and fast. Tunable.
GRID_M = 1.0


# ============================================================
# SCHEMA (idempotent)
# ============================================================
def ensure_schema(conn: Connection) -> None:
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SILVER}"))

    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {SILVER}.match_player_summary (
            task_id           uuid    NOT NULL,
            player_id         text    NOT NULL,
            model             text    NOT NULL DEFAULT 'sportai',
            player_end        text,               -- 'near' | 'far'
            distance_m        double precision,   -- SportAI covered_distance
            top_sprint_kmh    double precision,   -- SportAI fastest_sprint
            activity_score    double precision,
            swing_count       integer,
            fh_pct            double precision,   -- shot mix (fractions 0..1)
            backhand_pct      double precision,
            overhead_pct      double precision,   -- incl. serves (SportAI lumps them)
            slice_pct         double precision,
            other_pct         double precision,
            swing_mix         jsonb,              -- raw distribution, for flexibility
            avg_court_x       double precision,
            avg_court_y       double precision,
            pct_forecourt     double precision,   -- own-half depth thirds (dist from net)
            pct_midcourt      double precision,
            pct_backcourt     double precision,
            coverage_cells    integer,            -- distinct occupied grid cells
            position_samples  integer,
            created_at        timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (task_id, player_id, model)
        )
    """))

    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {SILVER}.player_movement_grid (
            task_id     uuid    NOT NULL,
            player_id   text    NOT NULL,
            model       text    NOT NULL DEFAULT 'sportai',
            cell_x      double precision NOT NULL,  -- cell CENTRE, court_x metres
            cell_y      double precision NOT NULL,  -- cell CENTRE, court_y metres
            samples     integer,                    -- position samples in cell
            seconds     double precision,           -- approx dwell time (samples / rate)
            PRIMARY KEY (task_id, player_id, model, cell_x, cell_y)
        )
    """))
    conn.execute(text(f"""
        CREATE INDEX IF NOT EXISTS ix_pmg_task ON {SILVER}.player_movement_grid(task_id, model)
    """))

    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {SILVER}.match_quality (
            task_id       uuid  NOT NULL,
            model         text  NOT NULL DEFAULT 'sportai',
            ball_conf     double precision,
            pose_conf     double precision,
            swing_conf    double precision,
            final_conf    double precision,
            quality_tier  text,               -- 'high' | 'medium' | 'low'
            created_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (task_id, model)
        )
    """))


# ============================================================
# BUILDERS
# ============================================================
def _players(conn: Connection, task_id: str):
    """Reuse build_silver_v2's de-ghosting so identity matches point_detail."""
    from build_silver_v2 import _resolve_two_players
    pf = _resolve_two_players(conn, task_id)
    return pf["p1"], pf["p2"]


def build_match_quality(conn: Connection, task_id: str, model: str = "sportai") -> int:
    """One row per match from bronze.session_confidences.final_confidences.
    quality_tier: high if final>=0.6 AND ball>=0.4; low if final<0.45 OR ball<0.25;
    else medium. Ball confidence is the load-bearing signal — a badly-tracked
    match (0336b82b) has low ball_conf and should read 'low'."""
    conn.execute(text(f"DELETE FROM {SILVER}.match_quality WHERE task_id=:t AND model=:m"),
                 {"t": task_id, "m": model})
    rows = conn.execute(text(f"""
        INSERT INTO {SILVER}.match_quality (task_id, model, ball_conf, pose_conf, swing_conf, final_conf, quality_tier)
        SELECT
          CAST(:t AS uuid), :m,
          (d->'final_confidences'->>'ball')::float,
          (d->'final_confidences'->>'pose')::float,
          (d->'final_confidences'->>'swing')::float,
          (d->'final_confidences'->>'final')::float,
          CASE
            WHEN (d->'final_confidences'->>'final')::float >= 0.60
             AND (d->'final_confidences'->>'ball')::float  >= 0.40 THEN 'high'
            WHEN (d->'final_confidences'->>'final')::float <  0.45
              OR (d->'final_confidences'->>'ball')::float  <  0.25 THEN 'low'
            ELSE 'medium'
          END
        FROM (SELECT data AS d FROM bronze.session_confidences WHERE task_id::text=:t) s
        WHERE d ? 'final_confidences'
    """), {"t": task_id, "m": model}).rowcount or 0
    return rows


def build_player_movement_grid(conn: Connection, task_id: str, model: str = "sportai") -> int:
    """PRE-AGGREGATED court occupancy grid from bronze.player_position.
    Bins court_x/court_y into GRID_M cells (centre-stored), counts samples, and
    converts to approximate dwell seconds using each player's own sample rate
    (samples / active duration). Raw ~3000 rows/player -> ~100 grid rows."""
    p1, p2 = _players(conn, task_id)
    conn.execute(text(f"DELETE FROM {SILVER}.player_movement_grid WHERE task_id=:t AND model=:m"),
                 {"t": task_id, "m": model})
    return conn.execute(text(f"""
        WITH pos AS (
          SELECT player_id, court_x, court_y, timestamp
          FROM bronze.player_position
          WHERE task_id::text = :t
            AND player_id IN (:p1, :p2)
            AND court_x IS NOT NULL AND court_y IS NOT NULL
        ),
        rate AS (   -- per-player samples/second over their active span
          SELECT player_id,
                 COUNT(*)::float
                   / NULLIF(GREATEST(MAX(timestamp) - MIN(timestamp), 0.001), 0) AS sps
          FROM pos GROUP BY player_id
        ),
        cells AS (
          SELECT player_id,
                 floor(court_x / :g) * :g + :g/2.0 AS cell_x,
                 floor(court_y / :g) * :g + :g/2.0 AS cell_y,
                 COUNT(*) AS samples
          FROM pos GROUP BY 1,2,3
        )
        INSERT INTO {SILVER}.player_movement_grid
          (task_id, player_id, model, cell_x, cell_y, samples, seconds)
        SELECT CAST(:t AS uuid), c.player_id, :m, c.cell_x, c.cell_y, c.samples,
               c.samples / NULLIF(r.sps, 0)
        FROM cells c JOIN rate r USING (player_id)
    """), {"t": task_id, "m": model, "p1": p1, "p2": p2, "g": GRID_M}).rowcount or 0


def build_match_player_summary(conn: Connection, task_id: str, model: str = "sportai") -> int:
    """One row per real player: fitness (bronze.player) + shot mix + movement
    summary (bronze.player_position). Near/far classified from position median
    court_y (end-agnostic, no dependency on team_session). Depth thirds are
    measured as distance-from-net so they mean the same thing for both ends."""
    p1, p2 = _players(conn, task_id)
    conn.execute(text(f"DELETE FROM {SILVER}.match_player_summary WHERE task_id=:t AND model=:m"),
                 {"t": task_id, "m": model})
    return conn.execute(text(f"""
        WITH pl AS (
          SELECT player_id::text AS pid, covered_distance, fastest_sprint,
                 activity_score, swing_count, swing_type_distribution AS mix
          FROM bronze.player
          WHERE task_id::text = :t AND player_id::text IN (:p1, :p2)
        ),
        pos AS (
          SELECT player_id AS pid, court_x, court_y
          FROM bronze.player_position
          WHERE task_id::text = :t AND player_id IN (:p1, :p2)
            AND court_x IS NOT NULL AND court_y IS NOT NULL
        ),
        posagg AS (
          SELECT pid,
                 COUNT(*) AS n,
                 AVG(court_x) AS avg_x,
                 AVG(court_y) AS avg_y,
                 percentile_cont(0.5) WITHIN GROUP (ORDER BY court_y) AS med_y,
                 COUNT(DISTINCT (floor(court_x/:g), floor(court_y/:g))) AS cells,
                 -- depth thirds by distance from net over the own half length
                 COUNT(*) FILTER (WHERE abs(court_y - :half) <= :third)                              AS fore,
                 COUNT(*) FILTER (WHERE abs(court_y - :half) >  :third AND abs(court_y - :half) <= :two_third) AS mid,
                 COUNT(*) FILTER (WHERE abs(court_y - :half) >  :two_third)                           AS back
          FROM pos GROUP BY pid
        )
        INSERT INTO {SILVER}.match_player_summary (
          task_id, player_id, model, player_end,
          distance_m, top_sprint_kmh, activity_score, swing_count,
          fh_pct, backhand_pct, overhead_pct, slice_pct, other_pct, swing_mix,
          avg_court_x, avg_court_y, pct_forecourt, pct_midcourt, pct_backcourt,
          coverage_cells, position_samples
        )
        SELECT
          CAST(:t AS uuid), pl.pid, :m,
          CASE WHEN pa.med_y > :half THEN 'near' ELSE 'far' END,
          pl.covered_distance, pl.fastest_sprint, pl.activity_score, pl.swing_count,
          COALESCE((pl.mix->>'fh')::float, 0),
          COALESCE((pl.mix->>'1h_bh')::float,0) + COALESCE((pl.mix->>'2h_bh')::float,0) + COALESCE((pl.mix->>'bh')::float,0),
          COALESCE((pl.mix->>'fh_overhead')::float,0) + COALESCE((pl.mix->>'bh_overhead')::float,0) + COALESCE((pl.mix->>'overhead')::float,0),
          COALESCE((pl.mix->>'fh_slice')::float,0) + COALESCE((pl.mix->>'bh_slice')::float,0) + COALESCE((pl.mix->>'slice')::float,0),
          COALESCE((pl.mix->>'other')::float, 0),
          pl.mix,
          pa.avg_x, pa.avg_y,
          pa.fore::float / NULLIF(pa.n,0), pa.mid::float / NULLIF(pa.n,0), pa.back::float / NULLIF(pa.n,0),
          pa.cells, pa.n
        FROM pl LEFT JOIN posagg pa ON pa.pid = pl.pid
    """), {"t": task_id, "m": model, "p1": p1, "p2": p2, "g": GRID_M,
           "half": HALF_Y, "third": HALF_LEN/3.0, "two_third": 2.0*HALF_LEN/3.0}).rowcount or 0


def build_all(engine: Engine, task_id: str, model: str = "sportai") -> dict:
    """Build all three analytics tables for one task. Each is best-effort — a
    missing bronze source yields 0 rows, not an error."""
    out = {}
    with engine.begin() as conn:
        ensure_schema(conn)
    # Each builder in its OWN transaction — a Postgres error aborts the whole
    # transaction, so sharing one would let a single failure sink the rest.
    for name, fn in (("match_quality", build_match_quality),
                     ("player_movement_grid", build_player_movement_grid),
                     ("match_player_summary", build_match_player_summary)):
        try:
            with engine.begin() as conn:
                out[name] = fn(conn, task_id, model)
        except Exception as ex:  # noqa: BLE001 — one table failing must not sink the rest
            log.warning("silver_analytics %s failed for %s: %s", name, task_id, ex)
            out[name] = f"ERROR: {ex.__class__.__name__}"
    return out
