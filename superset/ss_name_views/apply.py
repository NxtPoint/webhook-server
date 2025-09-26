# superset/ss_name_views/apply.py
import os, glob, importlib.util
import psycopg2

SCHEMA = "ss_name"
DB_URL = os.getenv("SQLALCHEMY_DATABASE_URI") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise SystemExit("[ss_name] Set SQLALCHEMY_DATABASE_URI or DATABASE_URL")
if DB_URL.startswith("postgresql+"):
    DB_URL = "postgresql://" + DB_URL.split("postgresql+")[1]

VIEWS_DIR = os.path.join(os.path.dirname(__file__), "views")

def run_sql(cur, sql: str, label: str):
    cur.execute(sql)
    print(f"[{SCHEMA}] applied {label}")

def run_dynamic_py(cur, path: str):
    spec = importlib.util.spec_from_file_location("dyn", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    sql = mod.make_sql(cur)       # expects a make_sql(cursor)-> str
    run_sql(cur, sql, os.path.basename(path))

def main():
    with psycopg2.connect(DB_URL) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

            # 1) dynamic base (if present)
            dyn_path = os.path.join(VIEWS_DIR, "010_vw_point_enriched.dynamic.py")
            if os.path.exists(dyn_path):
                run_dynamic_py(cur, dyn_path)

            # 2) all .sql files in order (except dynamic)
            for p in sorted(glob.glob(os.path.join(VIEWS_DIR, "*.sql"))):
                with open(p, "r", encoding="utf-8") as f:
                    sql = f.read()
                run_sql(cur, sql, os.path.basename(p))

    print(f"[{SCHEMA}] all view files applied")

if __name__ == "__main__":
    main()
