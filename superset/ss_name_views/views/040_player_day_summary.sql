CREATE OR REPLACE VIEW ss_.player_day_summary AS
WITH roles AS (
  SELECT pr.*, pa.serving_side_d
  FROM ss_.point_roles pr
  JOIN ss_.point_agg   pa
    USING (session_uid_d, point_number_d)
)
SELECT
  p.date_of_play::date AS day,         -- may be NULL if bronze not present
  r.player_id,
  COUNT(*)                              AS points_played,
  SUM(r.won)                            AS points_won,
  AVG(r.won::float)                     AS win_pct,
  AVG(CASE WHEN r.role='server'   THEN r.won::float END) AS srv_win_pct,
  AVG(CASE WHEN r.role='returner' THEN r.won::float END) AS rtn_win_pct
FROM roles r
LEFT JOIN ss_.vw_point_enriched p
  USING (session_uid_d, point_number_d)
GROUP BY day, r.player_id
ORDER BY day, r.player_id;
