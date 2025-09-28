CREATE SCHEMA IF NOT EXISTS ss_;

DROP VIEW IF EXISTS ss_.serve_loc_distribution CASCADE;

-- Pure downstream of 010 via ss_.serve_facts:
-- one row per session/server/side/bucket with in/out counts.
CREATE VIEW ss_.serve_loc_distribution AS
SELECT
  f.session_id,
  f.session_uid_d,
  f.server_id,
  f.side,                               -- from 055 (serving_side_d passthrough)
  f.serve_loc_18_d AS serve_bucket_1_8, -- from 010
  f.customer_name,                      -- exact name from 010
  f.email,                              -- exact name from 010
  COUNT(*) AS n_serves,
  COUNT(*) FILTER (WHERE f.is_in)       AS n_in,
  COUNT(*) FILTER (WHERE NOT f.is_in)   AS n_out
FROM ss_.serve_facts AS f
WHERE f.serve_loc_18_d IS NOT NULL
GROUP BY
  f.session_id, f.session_uid_d, f.server_id, f.side,
  f.serve_loc_18_d, f.customer_name, f.email;
