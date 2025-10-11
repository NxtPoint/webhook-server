# 001: ss_.vw_point auto-detect source schema for vw_point_silver
def make_sql(cur):
    cur.execute("""
        with cte as (
          select table_schema from information_schema.views where table_name='vw_point_silver'
          union all
          select table_schema from information_schema.tables where table_name='vw_point_silver'
        )
        select table_schema from cte limit 1;
    """)
    row = cur.fetchone()
    if not row:
        raise RuntimeError("vw_point_silver not found")
    src = f"{row[0]}.vw_point_silver"

    return f"""
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
    FROM {src} p;
    """
