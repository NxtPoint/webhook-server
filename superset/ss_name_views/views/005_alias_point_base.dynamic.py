def make_sql(cur):
    # already present in silver?
    cur.execute("""
      select 1
      from information_schema.views
      where table_schema='silver' and table_name='vw_point_silver'
      limit 1
    """)
    if cur.fetchone():
        return "select 1;"

    # find a vw_point_silver elsewhere and alias it into silver
    cur.execute("""
      with c as (
        select table_schema, 0 as prio
          from information_schema.views
         where table_name='vw_point_silver'
        union all
        select table_schema, 1 as prio
          from information_schema.tables
         where table_name='vw_point_silver'
      )
      select table_schema
        from c
       where table_schema <> 'silver'
       order by case when table_schema='public' then 0
                     when table_schema='bronze' then 1 else 2 end,
                prio, table_schema
       limit 1
    """)
    row = cur.fetchone()
    if not row:
        # last-ditch stub so downstream views still compile
        return """
          create schema if not exists silver;
          create or replace view silver.vw_point_silver as
          select now() as _no_data where false;
        """
    src_schema = row[0]
    return f"""
      create schema if not exists silver;
      create or replace view silver.vw_point_silver as
      select * from {src_schema}.vw_point_silver;
    """
