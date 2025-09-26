# superset/superset_views.py
import os
import psycopg2

DB_URL = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ.get("DATABASE_URL")
if not DB_URL:
    raise SystemExit("[gold] No DB URL set (SQLALCHEMY_DATABASE_URI or DATABASE_URL)")

# Normalize SQLAlchemy-style URL for psycopg2
if DB_URL.startswith("postgresql+"):
    DB_URL = "postgresql://" + DB_URL.split("postgresql+")[1]

# ---- Configure your bronze meta here (adjust to your real table/columns!) ----
BRONZE_META_TABLE = os.environ.get("BRONZE_META_TABLE", "bronze.frontend_meta")
# Which key to join on (pick ONE you have): 'session_uid_d' or 'session_id'
BRONZE_JOIN_KEY   = os.environ.get("BRONZE_JOIN_KEY", "session_uid_d")  # or "session_id"
# Column names in bronze meta (adjust these to match your table)
BRONZE_COLS = {
    "player_name":  "player_name",
    "location":     "location",
    "date_of_play": "date_of_play",  # date or timestamp
    "utr":          "utr"
}

def exists(cur, schema_dot_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (schema_dot_name,))
    return bool(cur.fetchone()[0])

def has_col(cur, schema: str, name: str, col: str) -> bool:
    cur.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_schema=%s AND table_name=%s AND column_name=%s""",
        (schema, name, col),
    )
    return cur.fetchone() is not None

def create_point_enriched(cur):
    cur.execute("CREATE SCHEMA IF NOT EXISTS gold;")
    join_clause = ""
    select_meta = ""
    if exists(cur, BRONZE_META_TABLE):
        schema, table = BRONZE_META_TABLE.split(".")
        # Join only if the selected join key exists on both sides
        if has_col(cur, "silver", "vw_point", BRONZE_JOIN_KEY) and has_col(cur, schema, table, BRONZE_JOIN_KEY):
            join_clause = f"""
            LEFT JOIN {BRONZE_META_TABLE} bm
              ON bm.{BRONZE_JOIN_KEY} = p.{BRONZE_JOIN_KEY}
            """
            # Pull the four meta fields if present
            meta_bits = []
            for alias, col in BRONZE_COLS.items():
                if has_col(cur, schema, table, col):
                    meta_bits.append(f"bm.{col} AS {alias}")
            if meta_bits:
                select_meta = ", " + ", ".join(meta_bits)
    sql = f"""
    CREATE OR REPLACE VIEW gold.vw_point_enriched AS
    SELECT p.*{select_meta}
    FROM silver.vw_point p
    {join_clause};
    """
    cur.execute(sql)
    print("[gold] created gold.vw_point_enriched")

def create_point_agg(cur):
    # Needs these cols minimally
    needed = [
        ("gold","vw_point_enriched","session_uid_d"),
        ("gold","vw_point_enriched","point_number_d"),
        ("gold","vw_point_enriched","server_id"),
        ("gold","vw_point_enriched","point_winner_player_id_d"),
    ]
    if not all(has_col(cur, *t) for t in needed):
        print("[gold] skip point_agg (missing essential columns)")
        return
    # Optional columns
    has_valid = has_col(cur, "gold","vw_point_enriched","valid_swing_d")
    has_fault = has_col(cur, "gold","vw_point_enriched","is_serve_fault_d")
    has_try   = has_col(cur, "gold","vw_point_enriched","serve_try_ix_in_point")
    has_last  = has_col(cur, "gold","vw_point_enriched","is_last_in_point_d")
    has_side  = has_col(cur, "gold","vw_point_enriched","serving_side_d")

    swings   = "COUNT(*) FILTER (WHERE valid_swing_d) AS swings" if has_valid else "NULL::int AS swings"
    fs_fault = ("COUNT(*) FILTER (WHERE is_serve_fault_d AND serve_try_ix_in_point=1) AS first_serve_faults"
                if (has_fault and has_try) else "NULL::int AS first_serve_faults")
    ss_fault = ("COUNT(*) FILTER (WHERE is_serve_fault_d AND serve_try_ix_in_point=2) AS second_serve_faults"
                if (has_fault and has_try) else "NULL::int AS second_serve_faults")
    dbl_fault = ("BOOL_OR(is_serve_fault_d AND serve_try_ix_in_point=2 AND "
                 + ("is_last_in_point_d" if has_last else "TRUE")
                 + ") AS double_fault"
                 if (has_fault and has_try) else "NULL::bool AS double_fault")
    side_sel = "MAX(serving_side_d) AS serving_side_d" if has_side else "NULL::text AS serving_side_d"

    sql = f"""
    CREATE OR REPLACE VIEW gold.point_agg AS
    WITH base AS (
      SELECT
        session_uid_d,
        session_id,
        point_number_d,
        MAX(server_id) AS server_id,
        MAX(point_winner_player_id_d) AS winner_id,
        {side_sel},
        MIN(start_s) AS point_start_s,
        MAX(end_s)   AS point_end_s,
        {swings},
        {fs_fault},
        {ss_fault},
        {dbl_fault}
      FROM gold.vw_point_enriched
      GROUP BY session_uid_d, session_id, point_number_d
    )
    SELECT *,
      (point_end_s - point_start_s) AS point_duration_s
    FROM base;
    """
    cur.execute(sql)
    print("[gold] created gold.point_agg")

def create_point_roles(cur):
    # expand per point into two rows: server / returner
    # We infer returner by distinct players in the point minus server_id
    sql = """
    CREATE OR REPLACE VIEW gold.point_roles AS
    WITH players AS (
      SELECT
        session_uid_d,
        point_number_d,
        MAX(server_id) AS server_id,
        MAX(point_winner_player_id_d) AS winner_id,
        ARRAY_AGG(DISTINCT player_id) FILTER (WHERE player_id IS NOT NULL) AS players
      FROM gold.vw_point_enriched
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
    """
    cur.execute(sql)
    print("[gold] created gold.point_roles")

def create_player_day_summary(cur):
    # date_of_play is optional (from bronze). If not present, we fall back to NULL::date
    has_date = has_col(cur, "gold", "vw_point_enriched", "date_of_play")
    date_sel = "date_of_play" if has_date else "NULL::date"
    sql = f"""
    CREATE OR REPLACE VIEW gold.player_day_summary AS
    WITH roles AS (
      SELECT pr.*, pa.serving_side_d
      FROM gold.point_roles pr
      JOIN gold.point_agg   pa
        USING (session_uid_d, point_number_d)
    )
    SELECT
      {date_sel} AS day,
      player_id,
      COUNT(*)                                    AS points_played,
      SUM(won)                                    AS points_won,
      AVG(won::float)                             AS win_pct,
      AVG(CASE WHEN role='server'   THEN won::float END) AS srv_win_pct,
      AVG(CASE WHEN role='returner' THEN won::float END) AS rtn_win_pct
    FROM roles
    LEFT JOIN gold.vw_point_enriched p
      USING (session_uid_d, point_number_d)   -- safe even if date_of_play is NULL
    GROUP BY day, player_id
    ORDER BY day, player_id;
    """
    cur.execute(sql)
    print("[gold] created gold.player_day_summary")

def create_serve_faults_summary(cur):
    sql = """
    CREATE OR REPLACE VIEW gold.serve_faults_summary AS
    WITH serves AS (
      SELECT
        session_uid_d, point_number_d, server_id,
        SUM(CASE WHEN serve_try_ix_in_point=1 THEN 1 ELSE 0 END) FILTER (WHERE serve_d) AS first_serve_attempts,
        SUM(CASE WHEN is_serve_fault_d AND serve_try_ix_in_point=1 THEN 1 ELSE 0 END) AS first_serve_faults,
        SUM(CASE WHEN is_serve_fault_d AND serve_try_ix_in_point=2 THEN 1 ELSE 0 END) AS second_serve_faults
      FROM gold.vw_point_enriched
      GROUP BY session_uid_d, point_number_d, server_id
    )
    SELECT
      server_id AS player_id,
      SUM(first_serve_attempts) AS first_serve_attempts,
      SUM(first_serve_faults)   AS first_serve_faults,
      SUM(second_serve_faults)  AS second_serve_faults,
      CASE WHEN SUM(first_serve_attempts)>0
           THEN 1.0 - (SUM(first_serve_faults)::float / SUM(first_serve_attempts))
           ELSE NULL END AS first_serve_in_pct,
      SUM(CASE WHEN second_serve_faults>0 THEN 1 ELSE 0 END) AS points_with_df -- rough DF count
    FROM serves
    GROUP BY server_id;
    """
    cur.execute(sql)
    print("[gold] created gold.serve_faults_summary")

def create_serve_loc_distribution(cur):
    if not has_col(cur, "gold","vw_point_enriched","serve_loc_18_d"):
        print("[gold] skip serve_loc_distribution (no serve_loc_18_d)")
        return
    sql = """
    CREATE OR REPLACE VIEW gold.serve_loc_distribution AS
    SELECT
      server_id AS player_id,
      serve_loc_18_d AS zone,
      COUNT(*) FILTER (WHERE serve_d) AS serves
    FROM gold.vw_point_enriched
    GROUP BY player_id, zone
    ORDER BY player_id, zone;
    """
    cur.execute(sql)
    print("[gold] created gold.serve_loc_distribution")

def create_rally_length_distribution(cur):
    # Use count of valid swings as rally length proxy
    if not has_col(cur, "gold","vw_point_enriched","valid_swing_d"):
        print("[gold] skip rally_length_distribution (no valid_swing_d)")
        return
    sql = """
    CREATE OR REPLACE VIEW gold.rally_length_distribution AS
    WITH rl AS (
      SELECT
        session_uid_d,
        point_number_d,
        COUNT(*) FILTER (WHERE valid_swing_d) AS rally_len
      FROM gold.vw_point_enriched
      GROUP BY session_uid_d, point_number_d
    )
    SELECT
      pa.server_id AS player_id,
      rel.rally_len,
      COUNT(*) AS points
    FROM rl rel
    JOIN gold.point_agg pa USING (session_uid_d, point_number_d)
    GROUP BY player_id, rally_len
    ORDER BY player_id, rally_len;
    """
    cur.execute(sql)
    print("[gold] created gold.rally_length_distribution")

def create_score_state_summary(cur):
    if not has_col(cur, "gold","vw_point_enriched","point_score_text_d"):
        print("[gold] skip score_state_summary (no point_score_text_d)")
        return
    sql = """
    CREATE OR REPLACE VIEW gold.score_state_summary AS
    WITH pts AS (
      SELECT DISTINCT
        session_uid_d,
        point_number_d,
        point_score_text_d,
        MAX(server_id) OVER (PARTITION BY session_uid_d, point_number_d) AS server_id,
        MAX(point_winner_player_id_d) OVER (PARTITION BY session_uid_d, point_number_d) AS winner_id
      FROM gold.vw_point_enriched
    )
    SELECT
      server_id AS player_id,
      point_score_text_d AS score_state,
      COUNT(*) AS points_played,
      SUM(CASE WHEN winner_id=server_id THEN 1 ELSE 0 END) AS server_points_won,
      AVG(CASE WHEN winner_id=server_id THEN 1.0 ELSE 0.0 END) AS server_win_pct
    FROM pts
    GROUP BY player_id, score_state
    ORDER BY player_id, score_state;
    """
    cur.execute(sql)
    print("[gold] created gold.score_state_summary")

def main():
    with psycopg2.connect(DB_URL) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            create_point_enriched(cur)
            create_point_agg(cur)
            create_point_roles(cur)
            create_player_day_summary(cur)
            create_serve_faults_summary(cur)
            create_serve_loc_distribution(cur)
            create_rally_length_distribution(cur)
            create_score_state_summary(cur)
    print("[gold] all views done")

if __name__ == "__main__":
    main()
