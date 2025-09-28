CREATE OR REPLACE VIEW ss_.serve_time_trend AS
WITH serves AS (
  SELECT
    COALESCE(p.submission_match_date, p.submission_first_point_ts::date) AS d,
    p.is_serve_fault_d
  FROM ss_.vw_point_enriched p
  WHERE p.serve_loc_18_d IS NOT NULL   -- restrict to the actual serve swing
)
SELECT
  d,
  COUNT(*)                                                   AS serves,
  SUM(CASE WHEN is_serve_fault_d THEN 1 ELSE 0 END)         AS faults,
  (SUM(CASE WHEN is_serve_fault_d THEN 1 ELSE 0 END)::numeric
     / NULLIF(COUNT(*), 0))                                  AS fault_rate
FROM serves
GROUP BY d
ORDER BY d;
