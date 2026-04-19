# tennis_coach/coach_views.py — Gold coach views for LLM Tennis Coach.
#
# Design decisions vs the spec:
#   - gold.coach_match_summary: NOT created. gold.match_kpi already has every
#     KPI we need. data_fetcher.py reads from match_kpi + match_serve_breakdown
#     + match_rally_breakdown + match_return_breakdown directly.
#   - gold.coach_serve_patterns: NOT created. gold.match_serve_breakdown already
#     has serve_side_d, serve_bucket_d, serve_count, serves_in, points_won at the
#     right granularity. Reused directly.
#   - gold.coach_rally_patterns: CREATED. Provides per-player × stroke × depth ×
#     aggression breakdown with error/winner rates — not available in existing views.
#   - gold.coach_pressure_points: PARTIAL STUB. Break point detection requires
#     inferring when a game score is 30-40 / 40-AD from score progression, which
#     is not stored in silver.point_detail. The view returns zero rows with the
#     correct column shape. Flagged below.
#
# All views use DROP + CREATE (not CREATE OR REPLACE) so column type changes on
# redeploy never silently fail. Each is in its own try/except so one failure
# cannot block the others. Dependencies: gold.vw_player must exist (created by
# gold_init.py which runs first on boot).

import logging
from sqlalchemy import text
from db_init import engine

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# gold.coach_rally_patterns
# Per (task_id, player_id, stroke_d, depth_d, aggression_d) rally shot stats.
# Feeds: weakness analysis + tactical adjustment prompts.
# ---------------------------------------------------------------------------
COACH_RALLY_PATTERNS_SQL = """
CREATE VIEW gold.coach_rally_patterns AS
SELECT
    pl.task_id,
    s.player_id,
    CASE WHEN s.player_id = pl.player_a_id THEN 'player_a' ELSE 'player_b' END AS player_role,
    s.stroke_d,
    s.depth_d,
    s.aggression_d,
    COUNT(*) AS shot_count,
    COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Error')   AS error_count,
    COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Winner')  AS winner_count,
    CASE WHEN COUNT(*) > 0
         THEN ROUND(100.0 * COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Error') / COUNT(*), 1)
         ELSE NULL END AS error_pct,
    CASE WHEN COUNT(*) > 0
         THEN ROUND(100.0 * COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Winner') / COUNT(*), 1)
         ELSE NULL END AS winner_pct
FROM silver.point_detail s
JOIN gold.vw_player pl ON pl.task_id = s.task_id
WHERE s.shot_phase_d IN ('Rally', 'Transition', 'Net')
  AND s.exclude_d IS NOT TRUE
  AND s.player_id IN (pl.player_a_id, pl.player_b_id)
  AND s.stroke_d IS NOT NULL
GROUP BY
    pl.task_id, s.player_id,
    pl.player_a_id, pl.player_b_id,
    s.stroke_d, s.depth_d, s.aggression_d
"""


# ---------------------------------------------------------------------------
# gold.coach_pressure_points — STUB (returns zero rows, correct column shape)
#
# Why stub: detecting break-point situations requires knowing the score at each
# point (0-0, 15-30, 30-40, etc.). silver.point_detail stores game_number,
# set_number, set_game_number, point_number, and point_winner_player_id, but
# NOT the running score within a game (e.g. "30-40"). Computing "was this a
# break point?" requires reconstructing per-game score progression via a
# running-count window function that is non-trivial and has known edge cases
# around deuce/advantage.
#
# TODO (needs work): implement pressure point detection by:
#   1. Using DISTINCT ON (task_id, game_number, point_number) to order points
#      within each game, then COUNT scored-by-each-player to derive game score.
#   2. A point is a break_point_faced (for server) when the returner needs 1 more
#      point to win the game AND server leads or it's deuce.
#   3. Once pressure detection is solid, populate this view with real data and
#      remove the WHERE 1=0 guard.
# ---------------------------------------------------------------------------
COACH_PRESSURE_POINTS_SQL = """
CREATE VIEW gold.coach_pressure_points AS
SELECT
    pl.task_id,
    pl.player_a_id  AS player_id,
    'player_a'::text AS player_role,
    0::bigint  AS bp_faced,
    0::bigint  AS bp_saved,
    NULL::numeric AS bp_saved_pct,
    0::bigint  AS bp_opportunities,
    0::bigint  AS bp_converted,
    NULL::numeric AS bp_converted_pct,
    NULL::numeric AS game_pts_won_pct,
    NULL::numeric AS set_pts_won_pct
FROM gold.vw_player pl
WHERE 1 = 0
"""


_COACH_VIEWS = [
    ("gold.coach_rally_patterns",  COACH_RALLY_PATTERNS_SQL),
    ("gold.coach_pressure_points", COACH_PRESSURE_POINTS_SQL),
]


def init_coach_views():
    """
    Idempotent recreation of gold coach views. Called from tennis_coach.init on boot.
    Depends on gold.vw_player existing (gold_init_presentation() must run first).

    All views are dropped + recreated inside a single transaction so concurrent
    readers block on DDL locks instead of seeing a missing view.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS gold"))
    except Exception:
        log.exception("[coach_views] failed to ensure gold schema")
        return

    created, failed = [], []
    try:
        with engine.begin() as conn:
            for name, sql in _COACH_VIEWS:
                try:
                    conn.execute(text(f"DROP VIEW IF EXISTS {name} CASCADE"))
                    conn.execute(text(sql))
                    created.append(name)
                    log.info("[coach_views] recreated %s", name)
                except Exception as e:
                    failed.append((name, str(e)))
                    log.error("[coach_views] failed to recreate %s: %s", name, e)
                    raise
    except Exception:
        log.exception("[coach_views] transaction rolled back — previous views retained")
        return {"created": [], "failed": failed or [("transaction", "rolled back")]}

    log.info("[coach_views] %d recreated atomically", len(created))
    return {"created": created, "failed": failed}
