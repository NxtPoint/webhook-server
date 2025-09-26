# superset/ss_name_views/views/010_vw_point_enriched.dynamic.py
import os

SCHEMA = "ss_"
BASE_SCHEMA = "silver"
BASE_VIEW = "vw_point"

# Optional bronze meta config (override via env if names differ)
BRONZE_META_TABLE = os.getenv("BRONZE_META_TABLE", "bronze.frontend_meta")
BRONZE_JOIN_KEY   = os.getenv("BRONZE_JOIN_KEY", "session_uid_d")  # or "session_id"
BRONZE_COLS = {
    "player_name":  "player_name",
    "location":     "location",
    "date_of_play": "date_of_play",
    "utr":          "utr",
}

def _exists(cur, schema_dot_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (schema_dot_name,))
    return bool(cur.fetchone()[0])

def _has_col(cur, schema: str, name: str, col: str) -> bool:
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_schema=%s AND table_name=%s AND column_name=%s""",
                (schema, name, col))
    return cur.fetchone() is not None

def make_sql(cur):
    # Always start from silver.vw_point
    join_clause = ""
    select_meta = ""

    if "." in BRONZE_META_TABLE and _exists(cur, BRONZE_META_TABLE):
        b_schema, b_table = BRONZE_META_TABLE.split(".")
        if _has_col(cur, BASE_SCHEMA, BASE_VIEW, BRONZE_JOIN_KEY) and _has_col(cur, b_schema, b_table, BRONZE_JOIN_KEY):
            join_clause = f"LEFT JOIN {BRONZE_META_TABLE} bm ON bm.{BRONZE_JOIN_KEY} = p.{BRONZE_JOIN_KEY}"
            bits = []
            for alias, col in BRONZE_COLS.items():
                if _has_col(cur, b_schema, b_table, col):
                    bits.append(f"bm.{col} AS {alias}")
            if bits:
                select_meta = ", " + ", ".join(bits)

    return f"""
    CREATE OR REPLACE VIEW {SCHEMA}.vw_point_enriched AS
    SELECT p.*{select_meta}
    FROM {BASE_SCHEMA}.{BASE_VIEW} p
    {join_clause};
    """
