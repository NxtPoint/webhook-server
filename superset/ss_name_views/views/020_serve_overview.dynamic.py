# -*- coding: utf-8 -*-
# 020: Serve Analytics dataset (derived from ss_.vw_point_enriched)
# Safe-by-default: only selects columns that actually exist on vw_point_enriched.
# Output: ss_.ds_serve_overview

REQUIRED_KEYS = [
    # Always good identifiers / slicers
    "session_uid_d", "game_number_d", "point_number_d", "server_player_id_d",
    "receiver_player_id_d", "point_start_ts_d"
]

OPTIONAL_KEYS = [
    # Submission / front-end meta
    "submission_created_at", "submission_email", "submission_customer_name",
    "submission_match_date", "submission_start_time", "submission_first_point_ts",
    "submission_location", "submission_player_a_name", "submission_player_a_utr",
    "submission_player_b_name", "submission_player_b_utr",

    # Serve facts (names may vary across your silver evolution; include broad set)
    "serve_try_d", "serve_side_d", "serve_loc_18_d", "serve_loc_ad_d",
    "serve_speed_kph_d", "is_ace_d", "is_double_fault_d",
    "serve_in_d", "serve_fault_d", "serve_won_d",

    # Point outcomes & rally context
    "point_winner_player_id_d", "point_won_by_server_d", "rally_length_d",
    "point_duration_s_d",

    # Helpful XY (if present at point-level)
    "serve_bounce_x_d", "serve_bounce_y_d",
    "return_contact_x_d", "return_contact_y_d"
]

def make_sql(cur):
    # Ensure source exists
    cur.execute("""
        select table_schema, table_name
        from information_schema.views
        where table_schema='ss_' and table_name='vw_point_enriched'
        limit 1
    """)
    row = cur.fetchone()
    if not row:
        raise RuntimeError("ss_.vw_point_enriched not found. Run 010 first.")

    # Discover available columns on vw_point_enriched
    cur.execute("""
        select column_name
        from information_schema.columns
        where table_schema='ss_' and table_name='vw_point_enriched'
    """)
    cols = {r[0] for r in cur.fetchall()}

    # Build projection list (only existing columns)
    project = []
    for k in REQUIRED_KEYS:
        if k not in cols:
            # Graceful fallback: if a required key is missing, we still proceed
            # but alias NULL to keep the dataset deployable.
            project.append(f"NULL::text as {k}")
        else:
            project.append(k)

    for k in OPTIONAL_KEYS:
        if k in cols:
            project.append(k)  # include if present

    # Derivations that are safe (depend on common names if present)
    derivations = []
    if {"serve_try_d", "serve_fault_d"}.issubset(cols):
        derivations.append("""
            case when serve_try_d = 1 and coalesce(serve_fault_d,false) then 1 else 0 end
                as is_first_serve_fault_d
        """)
    if {"serve_try_d", "is_double_fault_d"}.issubset(cols):
        derivations.append("""
            case when serve_try_d = 2 and coalesce(is_double_fault_d,false) then 1 else 0 end
                as is_double_fault_point_d
        """)
    if {"serve_speed_kph_d"}.issubset(cols):
        derivations.append("serve_speed_kph_d as serve_speed_kph")

    # Join everything
    select_list = ",\n      ".join(project + derivations) if (project or derivations) else "*"

    # Build final SQL
    sql = f"""
    create schema if not exists ss_;
    drop view if exists ss_.ds_serve_overview cascade;

    create view ss_.ds_serve_overview as
    select
      {select_list}
    from ss_.vw_point_enriched;
    """

    return sql
