# ss_.vw_point_enriched built from vw_point_silver (+ optional frontend meta)
# - Finds vw_point_silver in views or tables across schemas.
# - Optional overrides via env:
#     SS_POINT_BASE="schema.table_or_view"
#     SS_META_TABLE="schema.table"
# - If not found, creates a pass-through with meta columns = NULL (no hard fail).

import os
import psycopg2

def _row(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def _find_point_base(db_url: str) -> tuple[str, str] | None:
    override = os.getenv("SS_POINT_BASE")
    if override and "." in override:
        sch, name = override.split(".", 1)
        return sch, name

    sql = """
    with c as (
      -- views named vw_point_silver
      select table_schema, table_name, 0 as prio
      from information_schema.views
      where table_name='vw_point_silver'
      union all
      -- tables named vw_point_silver
      select table_schema, table_name, 1 as prio
      from information_schema.tables
      where table_name='vw_point_silver'
    )
    select table_schema, table_name
    from c
    order by
      case when table_schema='silver' then 0
           when table_schema='public' then 1
           else 2 end,
      prio, table_schema
    limit 1;
    """
    conn = psycopg2.connect(db_url)
    try:
        row = _row(conn, sql)
        return (row[0], row[1]) if row else None
    finally:
        conn.close()

def _base_has_task_id(db_url: str, schema: str, name: str) -> bool:
    sql = """
    select 1
    from information_schema.columns
    where table_schema=%s and table_name=%s and column_name='task_id'
    """
    conn = psycopg2.connect(db_url)
    try:
        return _row(conn, sql, (schema, name)) is not None
    finally:
        conn.close()

def _find_meta_table(db_url: str) -> tuple[str, str] | None:
    override = os.getenv("SS_META_TABLE")
    if override and "." in override:
        sch, tab = override.split(".", 1)
        return sch, tab

    candidates = [
        ("bronze", "frontend_submissions"),
        ("bronze", "uploads_meta"),
        ("bronze", "ingest_jobs"),
        ("public", "frontend_submissions"),
        ("public", "uploads_meta"),
    ]
    sql = """
    select 1
    from information_schema.columns
    where table_schema=%s and table_name=%s and column_name in ('task_id','email')
    limit 1;
    """
    conn = psycopg2.connect(db_url)
    try:
        for sch, tab in candidates:
            if _row(conn, sql, (sch, tab)):
                return sch, tab
        return None
    finally:
        conn.close()

def render(db_url: str) -> str:
    base = _find_point_base(db_url)
    meta = _find_meta_table(db_url)

    sql = ["create schema if not exists ss_;"]

    if not base:
        # No base found—create an empty view shape (so downstream CREATE VIEWs still succeed)
        sql.append("""
        create or replace view ss_.vw_point_enriched as
        select
          null::text as placeholder,
          null::text  as customer_name,
          null::text  as email,
          null::text  as first_point_time,
          null::date  as match_date_meta,
          null::text  as location,
          null::text  as player_a_name,
          null::numeric as player_a_utr,
          null::text  as player_b_name,
          null::numeric as player_b_utr,
          null::boolean as accept_terms
        where false;
        """)
        return "\n".join(sql)

    base_schema, base_name = base
    has_task = _base_has_task_id(db_url, base_schema, base_name)

    meta_cols = """
      m.customer_name,
      m.email,
      m.first_point_time,
      m.match_date as match_date_meta,
      m.location,
      m.player_a_name,
      m.player_a_utr,
      m.player_b_name,
      m.player_b_utr,
      m.accept_terms
    """

    if meta and has_task:
        m_schema, m_table = meta
        sql.append(f"""
        create or replace view ss_.vw_point_enriched as
        select p.*, {meta_cols}
        from {base_schema}.{base_name} p
        left join {m_schema}.{m_table} m
          on p.task_id = m.task_id;
        """)
    else:
        sql.append(f"""
        create or replace view ss_.vw_point_enriched as
        select
          p.*,
          null::text  as customer_name,
          null::text  as email,
          null::text  as first_point_time,
          null::date  as match_date_meta,
          null::text  as location,
          null::text  as player_a_name,
          null::numeric as player_a_utr,
          null::text  as player_b_name,
          null::numeric as player_b_utr,
          null::boolean as accept_terms
        from {base_schema}.{base_name} p;
        """)

    return "\n".join(sql)
