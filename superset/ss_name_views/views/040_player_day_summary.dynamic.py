def make_sql(cur) -> str:
    cur.execute("""
        select column_name
        from information_schema.columns
        where table_schema='ss_' and table_name='vw_point_enriched'
    """)
    cols = {r[0] for r in cur.fetchall()}
    def has(c): return c in cols

    # day
    if has("match_date_meta"):
        day_expr = "p.match_date_meta::date"
    elif has("date_of_play"):
        day_expr = "p.date_of_play::date"
    else:
        day_expr = "CURRENT_DATE"

    # email
    email_expr = "p.email" if has("email") else "NULL::text"

    # player_name
    if has("player_name"):
        player_name_expr = "p.player_name"
    elif has("player_a_name") or has("player_b_name"):
        left = "NULLIF(p.player_a_name,'')" if has("player_a_name") else "NULL"
        right = "NULLIF(p.player_b_name,'')" if has("player_b_name") else "NULL"
        player_name_expr = f"COALESCE({left}, {right})"
    else:
        player_name_expr = "NULL::text"

    # won flag
    won_expr = "p.won" if has("won") else "FALSE"

    # server/returner split
    if has("role"):
        srv_pts  = "CASE WHEN p.role='server' THEN 1 ELSE 0 END"
        srv_wins = f"CASE WHEN p.role='server' AND {won_expr} THEN 1 ELSE 0 END"
        rtn_pts  = "CASE WHEN p.role='returner' THEN 1 ELSE 0 END"
        rtn_wins = f"CASE WHEN p.role='returner' AND {won_expr} THEN 1 ELSE 0 END"
    elif has("is_server"):
        srv_pts  = "CASE WHEN p.is_server THEN 1 ELSE 0 END"
        srv_wins = f"CASE WHEN p.is_server AND {won_expr} THEN 1 ELSE 0 END"
        rtn_pts  = "CASE WHEN NOT p.is_server THEN 1 ELSE 0 END"
        rtn_wins = f"CASE WHEN NOT p.is_server AND {won_expr} THEN 1 ELSE 0 END"
    else:
        srv_pts  = "0"; srv_wins = "0"; rtn_pts = "0"; rtn_wins = "0"

    sql = f"""
    DROP VIEW IF EXISTS ss_.player_day_summary;
    CREATE VIEW ss_.player_day_summary AS
    SELECT
      {day_expr}                                AS day,
      {player_name_expr}                        AS player_name,
      {email_expr}                              AS email,
      COUNT(*)                                  AS points_played,
      CASE WHEN SUM({srv_pts})=0 THEN NULL
           ELSE SUM({srv_wins})::float / SUM({srv_pts}) END AS srv_win_pct,
      CASE WHEN SUM({rtn_pts})=0 THEN NULL
           ELSE SUM({rtn_wins})::float / SUM({rtn_pts}) END AS rtn_win_pct
    FROM ss_.vw_point_enriched p
    GROUP BY 1,2,3;
    """
    return sql
