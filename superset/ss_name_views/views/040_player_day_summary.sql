DROP VIEW IF EXISTS ss_.player_day_summary;

CREATE VIEW ss_.player_day_summary AS
SELECT
  COALESCE(p.match_date_meta::date, CURRENT_DATE)            AS day,
  /* pick a display name if present */
  COALESCE(NULLIF(p.player_a_name, ''), NULLIF(p.player_b_name, '')) AS player_name,
  p.email,                                                   -- keep original column name
  COUNT(*)                                                   AS points_played,
  /* server win% */
  (SUM(CASE WHEN p.role = 'server'   AND p.won THEN 1 ELSE 0 END)::float
   / NULLIF(SUM(CASE WHEN p.role = 'server'   THEN 1 ELSE 0 END), 0)) AS srv_win_pct,
  /* returner win% */
  (SUM(CASE WHEN p.role = 'returner' AND p.won THEN 1 ELSE 0 END)::float
   / NULLIF(SUM(CASE WHEN p.role = 'returner' THEN 1 ELSE 0 END), 0)) AS rtn_win_pct
FROM ss_.vw_point_enriched p
GROUP BY 1,2,3;
