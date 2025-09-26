# superset/ss_name_views/apply.py
import os, re, glob, time, importlib.util
import psycopg2

SCHEMA = "ss_"
VIEWS_DIR = os.path.join(os.path.dirname(__file__), "views")

# --- DB URL (normalize sqlalchemy-style) ---
raw = (
    os.getenv("SS_VIEWS_DB_URL")
    or os.getenv("DATA_DB_URL")
    or os.getenv("DATABASE_URL")
    or os.getenv("SQLALCHEMY_DATABASE_URI")
)

if not raw:
    raise SystemExit("[ss_] Set SQLALCHEMY_DATABASE_URI or DATABASE_URL")

# Replace prefixes like postgresql+psycopg2:// or postgresql+asyncpg:// with postgresql://
DB_URL = re.sub(r"^postgresql\+\w+://", "postgresql://", raw)

STRICT = os.getenv("SS_VIEWS_STRICT", "0").lower() in ("1", "true", "yes")

def run_sql(cur, sql: str, label: str):
    try:
        cur.execute(sql)
        print(f"[{SCHEMA}] applied {label}")
    except Exception as e:
        if STRICT:
            raise
        print(f"[{SCHEMA}] skipped {label}: {type(e).__name__}: {e}")

def run_dynamic_py(cur, path: str):
    spec = importlib.util.spec_from_file_location("dyn", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    sql = mod.make_sql(cur)       # expects make_sql(cursor)-> str
    run_sql(cur, sql, os.path.basename(path))

def main():
    # Simple retry so pre-deploy doesn’t flake if DB isn’t ready yet
    conn = None
    last_err = None
    for i in range(15):  # ~30s total
        try:
            conn = psycopg2.connect(DB_URL)
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
    if conn is None:
        raise SystemExit(f"[ss_] cannot connect to DB: {last_err}")

    with conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

            dyn_path = os.path.join(VIEWS_DIR, "010_vw_point_enriched.dynamic.py")
            if os.path.exists(dyn_path):
                run_dynamic_py(cur, dyn_path)

            for p in sorted(glob.glob(os.path.join(VIEWS_DIR, "*.sql"))):
                with open(p, "r", encoding="utf-8") as f:
                    sql = f.read()
                run_sql(cur, sql, os.path.basename(p))

    print(f"[{SCHEMA}] all view files applied")

if __name__ == "__main__":
    main()
