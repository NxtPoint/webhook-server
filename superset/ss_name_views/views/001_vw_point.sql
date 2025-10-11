-- ss_/views/001_vw_point.sql
CREATE OR REPLACE VIEW ss_.vw_point AS
SELECT
  p.*,
  CASE
    WHEN p.player_id = MIN(p.player_id) OVER (PARTITION BY p.session_id)
      THEN 'Player A'::text
      ELSE 'Player B'::text
  END AS player_label,
  (p.session_id::text || '|' ||
   CASE
     WHEN p.player_id = MIN(p.player_id) OVER (PARTITION BY p.session_id)
       THEN 'Player A'::text
       ELSE 'Player B'::text
   END) AS session_player_key
FROM silver.vw_point_silver p;
