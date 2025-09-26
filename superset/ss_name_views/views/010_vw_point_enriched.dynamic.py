# Build ss_.vw_point_enriched from vw_point_silver and attach front-end submission metadata.
# - Finds vw_point_silver in the most likely schema (silver/public/any).
# - Attempts to find a bronze meta table with task_id + email.
# - Joins on task_id when both sides have it; otherwise leaves points as-is.

import psycopg2

def _find_one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def _find_point_base(db_url: str) -> tuple[str, str]:
    sql = """
    with c as (
      select table_schema, table_name,
             case when table_schema='silver' then 0
                  when table_schema='public' then 1 else 2 end as prio
      from information_schema.views
      where table_name='vw_point_silver'
    )
    select table_schema, table_name
    from c order by prio, table_schema limit 1;
    """
    conn = psycopg2.connect(db_url)
    try:
        row = _find_one(conn, sql)
        if not row:
            raise RuntimeError("vw_point_silver not found; please create it.")
        return row[0], row[1]
    finally:
        conn.close()

def _base_has_task_id(conn, schema: str, view: str) -> bool:
    sql = """
    select 1
    from information_schema.columns
    where table_schema=%s and table_name=%s and column_name='task_id'
    """
    return _find_one(conn, sql, (schema, view)) is not None

# candidate meta tables and the minimum columns we expect
META_CANDIDATES = [
    ("bronze", "frontend_submissions"),
    ("bronze", "uploads_meta"),
    ("bronze", "ingest_jobs"),
    ("public", "frontend_submissions"),
    ("public", "uploads_meta"),
]

def _find_meta_table(db_url: str) -> tuple[str, str] | None:
    conn = psycopg2.connect(db_url)
    try:
        for sch, tab in META_CANDIDATES:
            sql = """
            select 1
            from information_schema.columns
            where table_schema=%s and table_name=%s and column_name in ('task_id','email')
            group by 1
            """
            if _find_one(conn, sql, (sch, tab)):
                return sch, tab
        return None
    finally:
        conn.close()

def render(db_url: str) -> str:
    base_schema, base_view = _find_point_base(db_url)

    # figure out join feasibility
    conn = psycopg2.connect(db_url)
    try:
        base_has_task = _base_has_task_id(conn, base_schema, base_view)
    finally:
        conn.close()

    meta = _find_meta_table(db_url)

    # build SQL
    sql = ["create schema if not exists ss_;"]

    # Columns we’ll project from the meta table (add/rename here if needed)
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

    if meta and base_has_task:
        meta_schema, meta_table = meta
        sql.append(f"""
        create or replace view ss_.vw_point_enriched as
        select
          p.*,
          {meta_cols}
        from {base_schema}.{base_view} p
        left join {meta_schema}.{meta_table} m
          on p.task_id = m.task_id;
        """)
    else:
        # no usable meta join -> keep a pass-through view (safe fallback)
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
        from {base_schema}.{base_view} p;
        """)

    return "\n".join(sql)
