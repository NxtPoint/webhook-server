CREATE OR REPLACE VIEW ss_.serve_faults_summary AS
WITH serves AS (
  SELECT
    session_uid_d, point_number_d, server_id,
    SUM(CASE WHEN serve_try_ix_in_point=1 AND serve_d THEN 1 ELSE 0 END) AS first_serve_attempts,
    SUM(CASE WHEN is_serve_fault_d AND serve_try_ix_in_point=1 THEN 1 ELSE 0 END) AS first_serve_faults,
    SUM(CASE WHEN is_serve_fault_d AND serve_try_ix_in_point=2 THEN 1 ELSE 0 END) AS second_serve_faults
  FROM ss_.vw_point_enriched
  GROUP BY session_uid_d, point_number_d, server_id
)
SELECT
  server_id AS player_id,
  SUM(first_serve_attempts) AS first_serve_attempts,
  SUM(first_serve_faults)   AS first_serve_faults,
  SUM(second_serve_faults)  AS second_serve_faults,
  CASE WHEN SUM(first_serve_attempts)>0
       THEN 1.0 - (SUM(first_serve_faults)::float / SUM(first_serve_attempts))
       ELSE NULL END AS first_serve_in_pct,
  SUM(CASE WHEN second_serve_faults>0 THEN 1 ELSE 0 END) AS points_with_df
FROM serves
GROUP BY server_id;
