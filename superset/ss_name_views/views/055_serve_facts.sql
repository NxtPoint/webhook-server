CREATE SCHEMA IF NOT EXISTS ss_;

DROP VIEW IF EXISTS ss_.serve_facts CASCADE;

-- Pure pass-through from 010 + minimal serve facts
CREATE VIEW ss_.serve_facts AS
SELECT
  src.*,
  src.serving_side_d                 AS side,
  (src.is_serve_fault_d IS NOT TRUE) AS is_in
FROM ss_.vw_point_enriched AS src
WHERE src.serve_d IS TRUE;
