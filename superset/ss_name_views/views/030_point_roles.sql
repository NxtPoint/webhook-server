CREATE OR REPLACE VIEW ss_name.point_roles AS
WITH players AS (
  SELECT
    session_uid_d,
    point_number_d,
    MAX(server_id) AS server_id,
    MAX(point_winner_player_id_d) AS winner_id,
    ARRAY_AGG(DISTINCT player_id) FILTER (WHERE player_id IS NOT NULL) AS players
  FROM ss_name.vw_point_enriched
  GROUP BY session_uid_d, point_number_d
),
expanded AS (
  SELECT
    session_uid_d,
    point_number_d,
    server_id,
    winner_id,
    unnest(players) AS player_id
  FROM players
)
SELECT
  e.*,
  CASE WHEN player_id=server_id THEN 'server' ELSE 'returner' END AS role,
  CASE WHEN player_id=winner_id THEN 1 ELSE 0 END AS won
FROM expanded e;
