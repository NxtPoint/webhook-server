# gold_init.py — Gold layer presentation views for dashboards + LLM coach.
#
# Architecture:
#   bronze.*  → raw ingestion (SportAI + T5)
#   silver.*  → analytical atomic point_detail (built by build_silver_v2.py)
#   gold.*    → thin presentation views, one per chart/widget (THIS FILE)
#
# Rules:
#   - Python owns the view definitions. SQL is source-controlled here, not in pgAdmin.
#   - Every view is created idempotently via DROP + CREATE on boot.
#   - Every view carries both task_id (for joining) and session_id (for display).
#   - Each view wraps a single try/except so one failure can't block the others.
#
# Consumers:
#   - /api/client/match/* endpoints (thin passthrough, no aggregation in Python)
#   - LLM coach (reads same views as dashboards → zero hallucination)
#   - PowerBI / Superset (can still join gold.vw_point to gold.vw_player)
#
# DO NOT edit views in the database directly. Edit this file, commit, deploy.

import logging
from sqlalchemy import text

from db_init import engine

log = logging.getLogger(__name__)


# ============================================================================
# LAYER 1 — BASE DIMENSION + FACT (ported from ss_.vw_player / ss_.vw_point)
# ============================================================================

VW_PLAYER_SQL = """
CREATE VIEW gold.vw_player AS
WITH sc AS (
    SELECT
        sc.task_id::uuid AS task_id,
        sc.created_at,
        sc.email,
        sc.customer_name,
        sc.match_date,
        sc.start_time,
        sc.location,
        sc.player_a_name,
        sc.player_b_name,
        sc.player_a_utr,
        sc.player_b_utr,
        sc.first_server
    FROM bronze.submission_context sc
    WHERE sc.sport_type = 'tennis_singles'
),
players_per_task AS (
    SELECT DISTINCT pd.task_id, pd.player_id
    FROM silver.point_detail pd
    WHERE pd.player_id IS NOT NULL
),
first_detected_server AS (
    SELECT DISTINCT ON (pd.task_id)
        pd.task_id,
        pd.player_id AS first_server_player_id
    FROM silver.point_detail pd
    WHERE pd.serve_d = true AND pd.player_id IS NOT NULL
    ORDER BY pd.task_id, pd.point_number, pd.ball_hit_s, pd.id
),
player_pairs_raw AS (
    SELECT
        fds.task_id,
        fds.first_server_player_id,
        MAX(CASE WHEN ppt.player_id <> fds.first_server_player_id THEN ppt.player_id END) AS other_player_id
    FROM first_detected_server fds
    JOIN players_per_task ppt ON ppt.task_id = fds.task_id
    GROUP BY fds.task_id, fds.first_server_player_id
),
player_pairs AS (
    SELECT
        sc.task_id,
        CASE
            WHEN sc.first_server = 'S' THEN ppr.first_server_player_id
            WHEN sc.first_server = 'R' THEN ppr.other_player_id
            WHEN sc.first_server = 'player_a' THEN ppr.first_server_player_id
            WHEN sc.first_server = 'player_b' THEN ppr.other_player_id
            ELSE ppr.first_server_player_id
        END AS player_a_id,
        CASE
            WHEN sc.first_server = 'S' THEN ppr.other_player_id
            WHEN sc.first_server = 'R' THEN ppr.first_server_player_id
            WHEN sc.first_server = 'player_a' THEN ppr.other_player_id
            WHEN sc.first_server = 'player_b' THEN ppr.first_server_player_id
            ELSE ppr.other_player_id
        END AS player_b_id
    FROM sc
    LEFT JOIN player_pairs_raw ppr ON ppr.task_id = sc.task_id
),
sc_with_seq AS (
    SELECT
        sc.task_id,
        sc.created_at,
        sc.email,
        sc.customer_name,
        sc.match_date,
        sc.start_time,
        sc.location,
        sc.player_a_name,
        sc.player_b_name,
        sc.player_a_utr,
        sc.player_b_utr,
        sc.first_server,
        (ROW_NUMBER() OVER (ORDER BY sc.created_at, sc.task_id) + 99)::integer AS session_id
    FROM sc
)
SELECT
    s.task_id,
    s.session_id,
    s.created_at,
    s.email,
    s.customer_name,
    s.match_date,
    s.start_time,
    s.location,
    s.player_a_name,
    s.player_b_name,
    s.player_a_utr,
    s.player_b_utr,
    pp.player_a_id,
    pp.player_b_id,
    s.first_server
FROM sc_with_seq s
LEFT JOIN player_pairs pp ON pp.task_id = s.task_id
"""


# gold.vw_point — flattened silver.point_detail with vw_player context joined in.
# Every row gets session_id, player_role ('player_a'/'player_b'), player_name for free.
VW_POINT_SQL = """
CREATE VIEW gold.vw_point AS
SELECT
    p.*,
    pl.session_id,
    pl.match_date,
    pl.location,
    pl.player_a_name,
    pl.player_b_name,
    pl.player_a_id,
    pl.player_b_id,
    pl.player_a_utr,
    pl.player_b_utr,
    pl.first_server,
    CASE
        WHEN p.player_id = pl.player_a_id THEN 'player_a'
        WHEN p.player_id = pl.player_b_id THEN 'player_b'
    END AS player_role,
    CASE
        WHEN p.player_id = pl.player_a_id THEN pl.player_a_name
        WHEN p.player_id = pl.player_b_id THEN pl.player_b_name
    END AS player_name,
    (p.task_id::text || '|' || p.player_id) AS playerkey_point,
    -- Serve classification (mirrors ss_.vw_point derived fields)
    CASE
        WHEN p.serve_d = true AND p.serve_try_ix_in_point = '1st' THEN 'First'
        WHEN p.serve_d = true AND p.serve_try_ix_in_point = '2nd' THEN 'Second'
        WHEN p.serve_d = true AND p.serve_try_ix_in_point = 'Double' THEN 'Double Fault'
    END AS serve_point_type_d,
    CASE
        WHEN p.serve_d = true AND p.serve_try_ix_in_point = '1st' THEN 'In'
        WHEN p.serve_d = true AND p.serve_try_ix_in_point = '2nd' THEN 'In'
        WHEN p.serve_d = true AND p.serve_try_ix_in_point = 'Double' THEN 'Double Fault'
    END AS serve_result_d
FROM silver.point_detail p
LEFT JOIN gold.vw_player pl ON pl.task_id = p.task_id
"""


# ============================================================================
# LAYER 2 — PRESENTATION VIEWS (one per dashboard chart)
# ============================================================================

# gold.match_kpi — single row per match with every top-level KPI for both players.
# Feeds: Summary tab (head-to-head card, score box, KPI strip).
MATCH_KPI_SQL = """
CREATE VIEW gold.match_kpi AS
WITH points_dedup AS (
    -- One row per unique point (last shot in the point)
    SELECT DISTINCT ON (task_id, game_number, point_number)
        task_id,
        game_number,
        point_number,
        point_winner_player_id,
        server_id,
        rally_length_point
    FROM silver.point_detail
    WHERE exclude_d IS NOT TRUE AND point_number IS NOT NULL
    ORDER BY task_id, game_number, point_number, shot_ix_in_point DESC NULLS LAST
),
point_stats AS (
    SELECT
        pl.task_id,
        COUNT(p.point_number) AS total_points,
        COUNT(*) FILTER (WHERE p.point_winner_player_id = pl.player_a_id) AS pa_points_won,
        COUNT(*) FILTER (WHERE p.point_winner_player_id = pl.player_b_id) AS pb_points_won,
        COUNT(*) FILTER (WHERE p.server_id = pl.player_a_id) AS pa_service_points,
        COUNT(*) FILTER (WHERE p.server_id = pl.player_b_id) AS pb_service_points,
        COUNT(*) FILTER (WHERE p.server_id = pl.player_a_id AND p.point_winner_player_id = pl.player_a_id) AS pa_svc_pts_won,
        COUNT(*) FILTER (WHERE p.server_id = pl.player_b_id AND p.point_winner_player_id = pl.player_b_id) AS pb_svc_pts_won,
        COUNT(*) FILTER (WHERE p.server_id = pl.player_b_id AND p.point_winner_player_id = pl.player_a_id) AS pa_ret_pts_won,
        COUNT(*) FILTER (WHERE p.server_id = pl.player_a_id AND p.point_winner_player_id = pl.player_b_id) AS pb_ret_pts_won,
        COUNT(*) FILTER (WHERE p.rally_length_point >= 5) AS total_rally_points,
        COUNT(*) FILTER (WHERE p.rally_length_point >= 5 AND p.point_winner_player_id = pl.player_a_id) AS pa_rally_pts_won,
        COUNT(*) FILTER (WHERE p.rally_length_point >= 5 AND p.point_winner_player_id = pl.player_b_id) AS pb_rally_pts_won,
        AVG(p.rally_length_point)::numeric(5,1) AS avg_rally_length,
        MAX(p.rally_length_point) AS max_rally_length
    FROM gold.vw_player pl
    LEFT JOIN points_dedup p ON p.task_id = pl.task_id
    GROUP BY pl.task_id, pl.player_a_id, pl.player_b_id
),
shot_stats AS (
    SELECT
        pl.task_id,
        COUNT(*) FILTER (WHERE s.ace_d = true AND s.player_id = pl.player_a_id) AS pa_aces,
        COUNT(*) FILTER (WHERE s.ace_d = true AND s.player_id = pl.player_b_id) AS pb_aces,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = 'Double' AND s.player_id = pl.player_a_id) AS pa_double_faults,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = 'Double' AND s.player_id = pl.player_b_id) AS pb_double_faults,
        COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Winner' AND s.player_id = pl.player_a_id) AS pa_winners,
        COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Winner' AND s.player_id = pl.player_b_id) AS pb_winners,
        COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Error' AND s.player_id = pl.player_a_id) AS pa_errors,
        COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Error' AND s.player_id = pl.player_b_id) AS pb_errors,
        -- First serve % (denominator = 1st serve attempts)
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = '1st' AND s.player_id = pl.player_a_id) AS pa_first_serves_total,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = '1st' AND s.shot_outcome_d <> 'Error' AND s.player_id = pl.player_a_id) AS pa_first_serves_in,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = '1st' AND s.player_id = pl.player_b_id) AS pb_first_serves_total,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = '1st' AND s.shot_outcome_d <> 'Error' AND s.player_id = pl.player_b_id) AS pb_first_serves_in,
        -- Serve speed
        AVG(s.ball_speed) FILTER (WHERE s.serve_d = true AND s.ball_speed > 0 AND s.player_id = pl.player_a_id)::numeric(5,1) AS pa_serve_speed_avg,
        MAX(s.ball_speed) FILTER (WHERE s.serve_d = true AND s.ball_speed > 0 AND s.player_id = pl.player_a_id)::numeric(5,1) AS pa_serve_speed_max,
        AVG(s.ball_speed) FILTER (WHERE s.serve_d = true AND s.ball_speed > 0 AND s.player_id = pl.player_b_id)::numeric(5,1) AS pb_serve_speed_avg,
        MAX(s.ball_speed) FILTER (WHERE s.serve_d = true AND s.ball_speed > 0 AND s.player_id = pl.player_b_id)::numeric(5,1) AS pb_serve_speed_max,
        -- Forehand speed
        AVG(s.ball_speed) FILTER (WHERE s.stroke_d = 'Forehand' AND s.ball_speed > 0 AND s.player_id = pl.player_a_id)::numeric(5,1) AS pa_fh_speed_avg,
        MAX(s.ball_speed) FILTER (WHERE s.stroke_d = 'Forehand' AND s.ball_speed > 0 AND s.player_id = pl.player_a_id)::numeric(5,1) AS pa_fh_speed_max,
        AVG(s.ball_speed) FILTER (WHERE s.stroke_d = 'Forehand' AND s.ball_speed > 0 AND s.player_id = pl.player_b_id)::numeric(5,1) AS pb_fh_speed_avg,
        MAX(s.ball_speed) FILTER (WHERE s.stroke_d = 'Forehand' AND s.ball_speed > 0 AND s.player_id = pl.player_b_id)::numeric(5,1) AS pb_fh_speed_max,
        -- Backhand speed
        AVG(s.ball_speed) FILTER (WHERE s.stroke_d = 'Backhand' AND s.ball_speed > 0 AND s.player_id = pl.player_a_id)::numeric(5,1) AS pa_bh_speed_avg,
        MAX(s.ball_speed) FILTER (WHERE s.stroke_d = 'Backhand' AND s.ball_speed > 0 AND s.player_id = pl.player_a_id)::numeric(5,1) AS pa_bh_speed_max,
        AVG(s.ball_speed) FILTER (WHERE s.stroke_d = 'Backhand' AND s.ball_speed > 0 AND s.player_id = pl.player_b_id)::numeric(5,1) AS pb_bh_speed_avg,
        MAX(s.ball_speed) FILTER (WHERE s.stroke_d = 'Backhand' AND s.ball_speed > 0 AND s.player_id = pl.player_b_id)::numeric(5,1) AS pb_bh_speed_max,
        -- Total serves (all serve_d shots per player)
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.player_id = pl.player_a_id) AS pa_total_serves,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.player_id = pl.player_b_id) AS pb_total_serves,
        -- Unreturned serves (service winners)
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.service_winner_d = true AND s.player_id = pl.player_a_id) AS pa_unreturned_serves,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.service_winner_d = true AND s.player_id = pl.player_b_id) AS pb_unreturned_serves,
        -- Second serve attempts and in
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = '2nd' AND s.player_id = pl.player_a_id) AS pa_second_serves_total,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = '2nd' AND s.shot_outcome_d <> 'Error' AND s.player_id = pl.player_a_id) AS pa_second_serves_in,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = '2nd' AND s.player_id = pl.player_b_id) AS pb_second_serves_total,
        COUNT(*) FILTER (WHERE s.serve_d = true AND s.serve_try_ix_in_point = '2nd' AND s.shot_outcome_d <> 'Error' AND s.player_id = pl.player_b_id) AS pb_second_serves_in,
        -- Return errors
        COUNT(*) FILTER (WHERE s.shot_ix_in_point = 2 AND s.shot_outcome_d = 'Error' AND s.player_id = pl.player_a_id) AS pa_return_errors,
        COUNT(*) FILTER (WHERE s.shot_ix_in_point = 2 AND s.shot_outcome_d = 'Error' AND s.player_id = pl.player_b_id) AS pb_return_errors,
        -- Rally outcomes (Rally/Transition/Net phases)
        COUNT(*) FILTER (WHERE s.shot_phase_d IN ('Rally','Transition','Net') AND s.shot_outcome_d = 'Winner' AND s.player_id = pl.player_a_id) AS pa_rally_winners,
        COUNT(*) FILTER (WHERE s.shot_phase_d IN ('Rally','Transition','Net') AND s.shot_outcome_d = 'Winner' AND s.player_id = pl.player_b_id) AS pb_rally_winners,
        COUNT(*) FILTER (WHERE s.shot_phase_d IN ('Rally','Transition','Net') AND s.shot_outcome_d = 'Error' AND s.player_id = pl.player_a_id) AS pa_rally_errors,
        COUNT(*) FILTER (WHERE s.shot_phase_d IN ('Rally','Transition','Net') AND s.shot_outcome_d = 'Error' AND s.player_id = pl.player_b_id) AS pb_rally_errors
    FROM gold.vw_player pl
    LEFT JOIN silver.point_detail s
        ON s.task_id = pl.task_id AND s.exclude_d IS NOT TRUE
    GROUP BY pl.task_id, pl.player_a_id, pl.player_b_id
),
serve_win_stats AS (
    WITH serve_points AS (
        SELECT DISTINCT ON (s.task_id, s.point_key)
            s.task_id, s.point_key, s.player_id AS server_id,
            s.serve_try_ix_in_point,
            s.point_winner_player_id
        FROM silver.point_detail s
        WHERE s.serve_d = true AND s.shot_ix_in_point = 1
          AND s.exclude_d IS NOT TRUE AND s.point_key IS NOT NULL
        ORDER BY s.task_id, s.point_key
    )
    SELECT
        pl.task_id,
        COUNT(*) FILTER (WHERE sp.serve_try_ix_in_point = '1st' AND sp.server_id = pl.player_a_id) AS pa_first_serve_pts_played,
        COUNT(*) FILTER (WHERE sp.serve_try_ix_in_point = '1st' AND sp.server_id = pl.player_a_id AND sp.point_winner_player_id = pl.player_a_id) AS pa_first_serve_pts_won,
        COUNT(*) FILTER (WHERE sp.serve_try_ix_in_point = '1st' AND sp.server_id = pl.player_b_id) AS pb_first_serve_pts_played,
        COUNT(*) FILTER (WHERE sp.serve_try_ix_in_point = '1st' AND sp.server_id = pl.player_b_id AND sp.point_winner_player_id = pl.player_b_id) AS pb_first_serve_pts_won,
        COUNT(*) FILTER (WHERE sp.serve_try_ix_in_point IN ('2nd','Double') AND sp.server_id = pl.player_a_id) AS pa_second_serve_pts_played,
        COUNT(*) FILTER (WHERE sp.serve_try_ix_in_point IN ('2nd','Double') AND sp.server_id = pl.player_a_id AND sp.point_winner_player_id = pl.player_a_id) AS pa_second_serve_pts_won,
        COUNT(*) FILTER (WHERE sp.serve_try_ix_in_point IN ('2nd','Double') AND sp.server_id = pl.player_b_id) AS pb_second_serve_pts_played,
        COUNT(*) FILTER (WHERE sp.serve_try_ix_in_point IN ('2nd','Double') AND sp.server_id = pl.player_b_id AND sp.point_winner_player_id = pl.player_b_id) AS pb_second_serve_pts_won
    FROM gold.vw_player pl
    LEFT JOIN serve_points sp ON sp.task_id = pl.task_id
    GROUP BY pl.task_id, pl.player_a_id, pl.player_b_id
)
SELECT
    pl.task_id,
    pl.session_id,
    pl.match_date,
    pl.location,
    pl.player_a_name,
    pl.player_b_name,
    pl.player_a_id,
    pl.player_b_id,
    pl.player_a_utr,
    pl.player_b_utr,
    -- Point totals
    ps.total_points,
    ps.pa_points_won,
    ps.pb_points_won,
    -- Service points
    ps.pa_service_points,
    ps.pb_service_points,
    ps.pa_svc_pts_won,
    ps.pb_svc_pts_won,
    CASE WHEN ps.pa_service_points > 0
         THEN ROUND(100.0 * ps.pa_svc_pts_won / ps.pa_service_points, 1) ELSE NULL END AS pa_svc_pts_won_pct,
    CASE WHEN ps.pb_service_points > 0
         THEN ROUND(100.0 * ps.pb_svc_pts_won / ps.pb_service_points, 1) ELSE NULL END AS pb_svc_pts_won_pct,
    -- Return points
    ps.pa_ret_pts_won,
    ps.pb_ret_pts_won,
    CASE WHEN ps.pb_service_points > 0
         THEN ROUND(100.0 * ps.pa_ret_pts_won / ps.pb_service_points, 1) ELSE NULL END AS pa_ret_pts_won_pct,
    CASE WHEN ps.pa_service_points > 0
         THEN ROUND(100.0 * ps.pb_ret_pts_won / ps.pa_service_points, 1) ELSE NULL END AS pb_ret_pts_won_pct,
    -- Rally points (5+ shots)
    ps.total_rally_points,
    ps.pa_rally_pts_won,
    ps.pb_rally_pts_won,
    CASE WHEN ps.total_rally_points > 0
         THEN ROUND(100.0 * ps.pa_rally_pts_won / ps.total_rally_points, 1) ELSE NULL END AS pa_rally_pts_won_pct,
    CASE WHEN ps.total_rally_points > 0
         THEN ROUND(100.0 * ps.pb_rally_pts_won / ps.total_rally_points, 1) ELSE NULL END AS pb_rally_pts_won_pct,
    -- Rally length
    ps.avg_rally_length,
    ps.max_rally_length,
    -- Shot-level totals
    ss.pa_aces,
    ss.pb_aces,
    ss.pa_double_faults,
    ss.pb_double_faults,
    ss.pa_winners,
    ss.pb_winners,
    ss.pa_errors,
    ss.pb_errors,
    -- 1st serve %
    ss.pa_first_serves_total,
    ss.pa_first_serves_in,
    ss.pb_first_serves_total,
    ss.pb_first_serves_in,
    CASE WHEN ss.pa_first_serves_total > 0
         THEN ROUND(100.0 * ss.pa_first_serves_in / ss.pa_first_serves_total, 1) ELSE NULL END AS pa_first_serve_pct,
    CASE WHEN ss.pb_first_serves_total > 0
         THEN ROUND(100.0 * ss.pb_first_serves_in / ss.pb_first_serves_total, 1) ELSE NULL END AS pb_first_serve_pct,
    -- Speeds
    ss.pa_serve_speed_avg,
    ss.pa_serve_speed_max,
    ss.pb_serve_speed_avg,
    ss.pb_serve_speed_max,
    ss.pa_fh_speed_avg,
    ss.pa_fh_speed_max,
    ss.pb_fh_speed_avg,
    ss.pb_fh_speed_max,
    ss.pa_bh_speed_avg,
    ss.pa_bh_speed_max,
    ss.pb_bh_speed_avg,
    ss.pb_bh_speed_max,
    -- Total serves
    ss.pa_total_serves,
    ss.pb_total_serves,
    -- Unreturned serves
    ss.pa_unreturned_serves,
    ss.pb_unreturned_serves,
    -- Second serve %
    ss.pa_second_serves_total,
    ss.pa_second_serves_in,
    ss.pb_second_serves_total,
    ss.pb_second_serves_in,
    CASE WHEN ss.pa_second_serves_total > 0
         THEN ROUND(100.0 * ss.pa_second_serves_in / ss.pa_second_serves_total, 1) ELSE NULL END AS pa_second_serve_pct,
    CASE WHEN ss.pb_second_serves_total > 0
         THEN ROUND(100.0 * ss.pb_second_serves_in / ss.pb_second_serves_total, 1) ELSE NULL END AS pb_second_serve_pct,
    -- First serve win %
    sws.pa_first_serve_pts_played,
    sws.pa_first_serve_pts_won,
    sws.pb_first_serve_pts_played,
    sws.pb_first_serve_pts_won,
    CASE WHEN sws.pa_first_serve_pts_played > 0
         THEN ROUND(100.0 * sws.pa_first_serve_pts_won / sws.pa_first_serve_pts_played, 1) ELSE NULL END AS pa_first_serve_won_pct,
    CASE WHEN sws.pb_first_serve_pts_played > 0
         THEN ROUND(100.0 * sws.pb_first_serve_pts_won / sws.pb_first_serve_pts_played, 1) ELSE NULL END AS pb_first_serve_won_pct,
    -- Second serve win %
    sws.pa_second_serve_pts_played,
    sws.pa_second_serve_pts_won,
    sws.pb_second_serve_pts_played,
    sws.pb_second_serve_pts_won,
    CASE WHEN sws.pa_second_serve_pts_played > 0
         THEN ROUND(100.0 * sws.pa_second_serve_pts_won / sws.pa_second_serve_pts_played, 1) ELSE NULL END AS pa_second_serve_won_pct,
    CASE WHEN sws.pb_second_serve_pts_played > 0
         THEN ROUND(100.0 * sws.pb_second_serve_pts_won / sws.pb_second_serve_pts_played, 1) ELSE NULL END AS pb_second_serve_won_pct,
    -- Return errors
    ss.pa_return_errors,
    ss.pb_return_errors,
    -- Rally outcomes
    ss.pa_rally_winners,
    ss.pb_rally_winners,
    ss.pa_rally_errors,
    ss.pb_rally_errors
FROM gold.vw_player pl
LEFT JOIN point_stats ps ON ps.task_id = pl.task_id
LEFT JOIN shot_stats ss ON ss.task_id = pl.task_id
LEFT JOIN serve_win_stats sws ON sws.task_id = pl.task_id
"""


# gold.match_serve_breakdown — per player × serve side × direction.
# Feeds: Serve Detail tab (the strategy table).
MATCH_SERVE_BREAKDOWN_SQL = """
CREATE VIEW gold.match_serve_breakdown AS
SELECT
    pl.task_id,
    pl.session_id,
    s.player_id,
    CASE WHEN s.player_id = pl.player_a_id THEN 'player_a' ELSE 'player_b' END AS player_role,
    CASE WHEN s.player_id = pl.player_a_id THEN pl.player_a_name ELSE pl.player_b_name END AS player_name,
    s.serve_side_d,
    s.serve_bucket_d,
    s.serve_try_ix_in_point,
    COUNT(*) AS serve_count,
    COUNT(*) FILTER (WHERE s.shot_outcome_d <> 'Error') AS serves_in,
    COUNT(DISTINCT s.point_key) AS points_played,
    COUNT(DISTINCT s.point_key) FILTER (WHERE s.point_winner_player_id = s.player_id) AS points_won,
    COUNT(*) FILTER (WHERE s.service_winner_d = true) AS unreturned
FROM silver.point_detail s
JOIN gold.vw_player pl ON pl.task_id = s.task_id
WHERE s.serve_d = true
  AND s.exclude_d IS NOT TRUE
  AND s.serve_side_d IS NOT NULL
  AND s.serve_bucket_d IS NOT NULL
  AND s.player_id IN (pl.player_a_id, pl.player_b_id)
GROUP BY
    pl.task_id, pl.session_id, s.player_id,
    pl.player_a_id, pl.player_b_id,
    pl.player_a_name, pl.player_b_name,
    s.serve_side_d, s.serve_bucket_d,
    s.serve_try_ix_in_point
"""


# gold.match_return_breakdown — per player return stats with vs-1st/vs-2nd split.
# Feeds: Return Detail tab.
MATCH_RETURN_BREAKDOWN_SQL = """
CREATE VIEW gold.match_return_breakdown AS
WITH returns AS (
    SELECT
        pl.task_id,
        pl.session_id,
        pl.player_a_id,
        pl.player_b_id,
        pl.player_a_name,
        pl.player_b_name,
        r.player_id AS returner_id,
        r.depth_d,
        r.stroke_d,
        r.shot_outcome_d,
        r.point_winner_player_id,
        r.point_key,
        srv.serve_try_ix_in_point AS serve_type
    FROM silver.point_detail r
    JOIN gold.vw_player pl ON pl.task_id = r.task_id
    LEFT JOIN silver.point_detail srv
        ON srv.task_id = r.task_id
       AND srv.point_key = r.point_key
       AND srv.shot_ix_in_point = 1
    WHERE r.shot_ix_in_point = 2
      AND r.exclude_d IS NOT TRUE
)
SELECT
    task_id,
    session_id,
    returner_id AS player_id,
    CASE WHEN returner_id = player_a_id THEN 'player_a' ELSE 'player_b' END AS player_role,
    CASE WHEN returner_id = player_a_id THEN player_a_name ELSE player_b_name END AS player_name,
    COUNT(*) AS returns_played,
    COUNT(*) FILTER (WHERE shot_outcome_d <> 'Error') AS returns_made,
    COUNT(*) FILTER (WHERE point_winner_player_id = returner_id) AS return_pts_won,
    COUNT(*) FILTER (WHERE shot_outcome_d = 'Winner') AS return_winners,
    COUNT(*) FILTER (WHERE shot_outcome_d = 'Error') AS return_errors,
    COUNT(*) FILTER (WHERE depth_d = 'Deep') AS returns_deep,
    COUNT(*) FILTER (WHERE depth_d = 'Middle') AS returns_middle,
    COUNT(*) FILTER (WHERE depth_d = 'Short') AS returns_short,
    COUNT(*) FILTER (WHERE stroke_d = 'Forehand') AS returns_forehand,
    COUNT(*) FILTER (WHERE stroke_d = 'Backhand') AS returns_backhand,
    COUNT(*) FILTER (WHERE serve_type = '1st') AS vs_first_serve_played,
    COUNT(*) FILTER (WHERE serve_type = '1st' AND point_winner_player_id = returner_id) AS vs_first_serve_won,
    COUNT(*) FILTER (WHERE serve_type IN ('2nd', 'Double')) AS vs_second_serve_played,
    COUNT(*) FILTER (WHERE serve_type IN ('2nd', 'Double') AND point_winner_player_id = returner_id) AS vs_second_serve_won
FROM returns
GROUP BY task_id, session_id, returner_id, player_a_id, player_b_id, player_a_name, player_b_name
"""


# gold.match_rally_breakdown — per player rally shot stats: aggression/depth/stroke + speeds.
# Feeds: Rally Detail tab (per-player breakdowns).
MATCH_RALLY_BREAKDOWN_SQL = """
CREATE VIEW gold.match_rally_breakdown AS
SELECT
    pl.task_id,
    pl.session_id,
    s.player_id,
    CASE WHEN s.player_id = pl.player_a_id THEN 'player_a' ELSE 'player_b' END AS player_role,
    CASE WHEN s.player_id = pl.player_a_id THEN pl.player_a_name ELSE pl.player_b_name END AS player_name,
    COUNT(*) AS rally_shots,
    -- Aggression
    COUNT(*) FILTER (WHERE s.aggression_d = 'Attack') AS aggression_attack,
    COUNT(*) FILTER (WHERE s.aggression_d = 'Neutral') AS aggression_neutral,
    COUNT(*) FILTER (WHERE s.aggression_d = 'Defence') AS aggression_defence,
    -- Depth
    COUNT(*) FILTER (WHERE s.depth_d = 'Deep') AS depth_deep,
    COUNT(*) FILTER (WHERE s.depth_d = 'Middle') AS depth_middle,
    COUNT(*) FILTER (WHERE s.depth_d = 'Short') AS depth_short,
    -- Stroke
    COUNT(*) FILTER (WHERE s.stroke_d = 'Forehand') AS stroke_forehand,
    COUNT(*) FILTER (WHERE s.stroke_d = 'Backhand') AS stroke_backhand,
    COUNT(*) FILTER (WHERE s.stroke_d = 'Slice') AS stroke_slice,
    COUNT(*) FILTER (WHERE s.stroke_d = 'Volley') AS stroke_volley,
    -- Outcomes
    COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Winner') AS winners,
    COUNT(*) FILTER (WHERE s.shot_outcome_d = 'Error') AS errors,
    -- Speeds
    AVG(s.ball_speed) FILTER (WHERE s.stroke_d = 'Forehand' AND s.ball_speed > 0)::numeric(5,1) AS fh_speed_avg,
    MAX(s.ball_speed) FILTER (WHERE s.stroke_d = 'Forehand' AND s.ball_speed > 0)::numeric(5,1) AS fh_speed_max,
    AVG(s.ball_speed) FILTER (WHERE s.stroke_d = 'Backhand' AND s.ball_speed > 0)::numeric(5,1) AS bh_speed_avg,
    MAX(s.ball_speed) FILTER (WHERE s.stroke_d = 'Backhand' AND s.ball_speed > 0)::numeric(5,1) AS bh_speed_max
FROM silver.point_detail s
JOIN gold.vw_player pl ON pl.task_id = s.task_id
WHERE s.shot_phase_d IN ('Rally', 'Transition', 'Net')
  AND s.exclude_d IS NOT TRUE
  AND s.player_id IN (pl.player_a_id, pl.player_b_id)
GROUP BY
    pl.task_id, pl.session_id, s.player_id,
    pl.player_a_id, pl.player_b_id,
    pl.player_a_name, pl.player_b_name
"""


# gold.match_rally_length — rally length distribution with per-player wins.
# Feeds: Rally Detail tab (length distribution chart).
MATCH_RALLY_LENGTH_SQL = """
CREATE VIEW gold.match_rally_length AS
WITH points_dedup AS (
    SELECT DISTINCT ON (task_id, game_number, point_number)
        task_id,
        rally_length_point,
        point_winner_player_id
    FROM silver.point_detail
    WHERE exclude_d IS NOT TRUE
      AND rally_length_point IS NOT NULL
      AND point_number IS NOT NULL
    ORDER BY task_id, game_number, point_number, shot_ix_in_point DESC NULLS LAST
)
SELECT
    pl.task_id,
    pl.session_id,
    p.rally_length_point,
    CASE
        WHEN p.rally_length_point BETWEEN 1 AND 4 THEN 'Short (1-4)'
        WHEN p.rally_length_point BETWEEN 5 AND 8 THEN 'Medium (5-8)'
        WHEN p.rally_length_point >= 9 THEN 'Long (9+)'
    END AS length_bucket,
    COUNT(*) AS points,
    COUNT(*) FILTER (WHERE p.point_winner_player_id = pl.player_a_id) AS pa_points_won,
    COUNT(*) FILTER (WHERE p.point_winner_player_id = pl.player_b_id) AS pb_points_won
FROM points_dedup p
JOIN gold.vw_player pl ON pl.task_id = p.task_id
GROUP BY pl.task_id, pl.session_id, p.rally_length_point, pl.player_a_id, pl.player_b_id
"""


# gold.match_shot_placement — thin shot-level data for heatmaps + video overlays.
# Feeds: Placement Heatmaps module.
MATCH_SHOT_PLACEMENT_SQL = """
CREATE VIEW gold.match_shot_placement AS
SELECT
    pl.task_id,
    pl.session_id,
    s.id,
    s.point_number,
    s.game_number,
    s.set_number,
    s.point_key,
    s.shot_ix_in_point,
    s.player_id,
    CASE WHEN s.player_id = pl.player_a_id THEN 'player_a' ELSE 'player_b' END AS player_role,
    CASE WHEN s.player_id = pl.player_a_id THEN pl.player_a_name ELSE pl.player_b_name END AS player_name,
    s.shot_phase_d,
    s.stroke_d,
    s.shot_outcome_d,
    s.serve_d,
    s.serve_try_ix_in_point,
    s.serve_bucket_d,
    s.serve_side_d,
    s.depth_d,
    s.aggression_d,
    s.ball_speed,
    s.ball_hit_location_x,
    s.ball_hit_location_y,
    s.ball_hit_x_norm,
    s.ball_hit_y_norm,
    s.ball_bounce_x_norm,
    s.ball_bounce_y_norm,
    s.court_x,
    s.court_y,
    s.point_winner_player_id,
    s.rally_location_hit,
    s.rally_location_bounce
FROM silver.point_detail s
JOIN gold.vw_player pl ON pl.task_id = s.task_id
WHERE s.exclude_d IS NOT TRUE
  AND s.player_id IN (pl.player_a_id, pl.player_b_id)
"""


# ============================================================================
# ORCHESTRATION
# ============================================================================

_VIEWS = [
    # Base dim + fact
    ("gold.vw_player", VW_PLAYER_SQL),
    ("gold.vw_point", VW_POINT_SQL),
    # Presentation (5 + rally_length)
    ("gold.match_kpi", MATCH_KPI_SQL),
    ("gold.match_serve_breakdown", MATCH_SERVE_BREAKDOWN_SQL),
    ("gold.match_return_breakdown", MATCH_RETURN_BREAKDOWN_SQL),
    ("gold.match_rally_breakdown", MATCH_RALLY_BREAKDOWN_SQL),
    ("gold.match_rally_length", MATCH_RALLY_LENGTH_SQL),
    ("gold.match_shot_placement", MATCH_SHOT_PLACEMENT_SQL),
]


def gold_init_presentation():
    """
    Idempotent recreation of gold presentation views. Safe to call on every boot.

    Each view is DROP + CREATE (not CREATE OR REPLACE) to avoid column-type
    replacement errors when schemas evolve. Each view is wrapped in try/except
    so a single failure doesn't block the rest.

    Dependencies matter: vw_player must exist before vw_point, which must exist
    before any match_* view that references it. The _VIEWS list is ordered
    accordingly.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS gold"))
    except Exception:
        log.exception("[gold_init] failed to ensure gold schema")
        return

    created = []
    failed = []
    for name, sql in _VIEWS:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
                conn.execute(text(sql))
            created.append(name)
            log.info("[gold_init] created %s", name)
        except Exception as e:
            failed.append((name, str(e)))
            log.error("[gold_init] failed to create %s: %s", name, e)

    log.info(
        "[gold_init] presentation views: %d created, %d failed",
        len(created),
        len(failed),
    )
    return {"created": created, "failed": failed}
