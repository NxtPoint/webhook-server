CREATE SCHEMA IF NOT EXISTS ss_;

CREATE OR REPLACE VIEW ss_.serve_loc_distribution AS
WITH binned AS (
  SELECT
    f.submission_customer_name,
    f.submission_match_date,
    f.submission_task_id,
    f.player_id,
    f.side,
    f.serve_try,
    floor(f.serve_loc_x / 0.5) * 0.5 AS x_bin,
    floor(f.serve_loc_y / 0.5) * 0.5 AS y_bin,
    COUNT(*)::bigint                                       AS attempts,
    (COUNT(*) FILTER (WHERE f.serve_in))::bigint           AS serves_in,
    (COUNT(*) FILTER (WHERE f.is_ace))::bigint             AS aces,
    (COUNT(*) FILTER (WHERE f.is_fault))::bigint           AS faults,
    (COUNT(*) FILTER (WHERE f.is_double_fault))::bigint    AS double_faults
  FROM ss_.serve_facts f
  WHERE f.serve_loc_x IS NOT NULL
    AND f.serve_loc_y IS NOT NULL
  GROUP BY 1,2,3,4,5,6,7,8
)
SELECT * FROM binned;
