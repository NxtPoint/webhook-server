# ss_.vw_point_enriched built from vw_point_silver (+ optional frontend meta)
# - Finds vw_point_silver in views or tables across schemas.
# - Optional overrides via env:
#     SS_POINT_BASE="schema.table_or_view"
#     SS_META_TABLE="schema.table"
# - If not found, creates a pass-through with meta columns = NULL (no hard fail).
#
# NOTE: apply.py expects: make_sql(cur) -> str  (cur is a psycopg2 cursor)

import os

def _row(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()

def _find_point_base(cur):
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
    row = _row(cur, sql)
    return (row[0], row[1]) if row else None

def _base_has_task_id(cur, schema: str, name: str) -> bool:
    sql = """
    select 1
    from information_schema.columns
    where table_schema=%s and table_name=%s and column_name='task_id'
    """
    return _row(cur, sql, (schema, name)) is not None

def _find_meta_table(cur):
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
    for sch, tab in candidates:
        if _row(cur, sql, (sch, tab)):
            return sch, tab
    return None

def make_sql(cur):
    base = _find_point_base(cur)
    meta = _find_meta_table(cur)

    parts = ["create schema if not exists ss_;"]

    if not base:
        # No base found—create an empty view shape (deploy stays green)
        parts.append("""
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
        return "\n".join(parts)

    base_schema, base_name = base
    has_task = _base_has_task_id(cur, base_schema, base_name)

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
        parts.append(f"""
        create or replace view ss_.vw_point_enriched as
        select p.*, {meta_cols}
        from {base_schema}.{base_name} p
        left join {m_schema}.{m_table} m
          on p.task_id = m.task_id;
        """)
    else:
        parts.append(f"""
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

    return "\n".join(parts)
