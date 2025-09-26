CREATE OR REPLACE VIEW ss_name.rally_length_distribution AS
WITH rl AS (
  SELECT
    session_uid_d,
    point_number_d,
    COUNT(*) FILTER (WHERE valid_swing_d) AS rally_len
  FROM ss_name.vw_point_enriched
  GROUP BY session_uid_d, point_number_d
)
SELECT
  pa.server_id AS player_id,
  rl.rally_len,
  COUNT(*) AS points
FROM rl
JOIN ss_name.point_agg pa USING (session_uid_d, point_number_d)
GROUP BY player_id, rally_len
ORDER BY player_id, rally_len;
