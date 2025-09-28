CREATE OR REPLACE VIEW ss_.serve_loc_distribution AS
WITH serves AS (
  SELECT
    p.session_uid_d,
    p.serving_side_d,
    p.serve_loc_18_d                         AS serve_bucket_1_8,
    p.submission_customer_name,
    p.submission_email,
    p.is_serve_fault_d
  FROM ss_.vw_point_enriched p
  -- serve_loc_18_d is only populated on the actual serve swing
  WHERE p.serve_loc_18_d IS NOT NULL
)
SELECT
  session_uid_d,
  serving_side_d,
  serve_bucket_1_8,
  submission_customer_name,
  submission_email,
  COUNT(*)                                                   AS serves,
  SUM(CASE WHEN is_serve_fault_d THEN 1 ELSE 0 END)         AS faults,
  ROUND(
    100.0 * SUM(CASE WHEN is_serve_fault_d THEN 1 ELSE 0 END)::numeric
      / NULLIF(COUNT(*), 0),
    2
  )                                                          AS fault_pct
FROM serves
GROUP BY
  session_uid_d, serving_side_d, serve_bucket_1_8,
  submission_customer_name, submission_email;
