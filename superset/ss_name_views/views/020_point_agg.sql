CREATE OR REPLACE VIEW ss_name.point_agg AS
WITH base AS (
  SELECT
    session_uid_d,
    session_id,
    point_number_d,
    MAX(server_id) AS server_id,
    MAX(point_winner_player_id_d) AS winner_id,
    COALESCE(MAX(serving_side_d), NULL) AS serving_side_d,
    MIN(start_s) AS point_start_s,
    MAX(end_s)   AS point_end_s,
    COUNT(*) FILTER (WHERE valid_swing_d) AS swings,
    COUNT(*) FILTER (WHERE is_serve_fault_d AND serve_try_ix_in_point=1) AS first_serve_faults,
    COUNT(*) FILTER (WHERE is_serve_fault_d AND serve_try_ix_in_point=2) AS second_serve_faults,
    BOOL_OR(is_serve_fault_d AND serve_try_ix_in_point=2 AND COALESCE(is_last_in_point_d, TRUE)) AS double_fault
  FROM ss_name.vw_point_enriched
  GROUP BY session_uid_d, session_id, point_number_d
)
SELECT *,
  (point_end_s - point_start_s) AS point_duration_s
FROM base;
