CREATE SCHEMA IF NOT EXISTS ss_;

-- Distribution of serve landing locations by side (deuce/ad) and attempt (1/2)
CREATE OR REPLACE VIEW ss_.serve_loc_distribution AS
WITH binned AS (
  SELECT
    f.player_id,
    f.side,
    f.serve_try,
    -- bin to 0.5 grid; tweak if you prefer a different granularity
    floor(f.serve_loc_x / 0.5) * 0.5 AS x_bin,
    floor(f.serve_loc_y / 0.5) * 0.5 AS y_bin,
    -- outcomes
    COUNT(*)                                              AS attempts,
    SUM(CASE WHEN f.is_ace          IS TRUE THEN 1 ELSE 0 END) AS aces,
    SUM(CASE WHEN f.is_fault        IS TRUE THEN 1 ELSE 0 END) AS faults,
    SUM(CASE WHEN f.is_double_fault IS TRUE THEN 1 ELSE 0 END) AS double_faults,
    SUM(CASE WHEN f.serve_in        IS TRUE THEN 1 ELSE 0 END) AS serves_in
  FROM ss_.serve_facts f
  WHERE f.serve_loc_x IS NOT NULL
    AND f.serve_loc_y IS NOT NULL
  GROUP BY 1,2,3,4,5
)
SELECT * FROM binned;
