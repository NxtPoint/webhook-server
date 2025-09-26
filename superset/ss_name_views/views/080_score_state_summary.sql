CREATE OR REPLACE VIEW ss_.score_state_summary AS
WITH pts AS (
  SELECT DISTINCT
    session_uid_d,
    point_number_d,
    point_score_text_d,
    MAX(server_id) OVER (PARTITION BY session_uid_d, point_number_d) AS server_id,
    MAX(point_winner_player_id_d) OVER (PARTITION BY session_uid_d, point_number_d) AS winner_id
  FROM ss_.vw_point_enriched
)
SELECT
  server_id AS player_id,
  point_score_text_d AS score_state,
  COUNT(*) AS points_played,
  SUM(CASE WHEN winner_id=server_id THEN 1 ELSE 0 END) AS server_points_won,
  AVG(CASE WHEN winner_id=server_id THEN 1.0 ELSE 0.0 END) AS server_win_pct
FROM pts
GROUP BY player_id, score_state
ORDER BY player_id, score_state;
