#!/usr/bin/env python3
# superset/ss_name_views/apply.py
import os, sys, glob, re, importlib.util
import psycopg2

SCHEMA = "ss_"
VIEWS_DIR = os.path.join(os.path.dirname(__file__), "views")

raw = (
    os.getenv("SS_VIEWS_DB_URL")
    or os.getenv("DATA_DB_URL")
    or os.getenv("DATABASE_URL")
    or os.getenv("SQLALCHEMY_DATABASE_URI")
)
if not raw:
    print("[ss_] ERROR: set SS_VIEWS_DB_URL or DATABASE_URL", flush=True); sys.exit(0)
DB_URL = re.sub(r"^postgresql\+\w+://", "postgresql://", raw)

def log(msg): print(msg, flush=True)

def load_module(path):
    spec = importlib.util.spec_from_file_location("dyn", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def call_dynamic(mod, cur, conn, db_url):
    # Preferred
    if hasattr(mod, "make_sql"):
        try:    return mod.make_sql(cur)
        except TypeError: pass
    # Fallbacks
    for name in ("render", "build", "get_sql"):
        fn = getattr(mod, name, None)
        if callable(fn):
            for arg in (cur, conn, db_url, None):
                try:    return fn() if arg is None else fn(arg)
                except TypeError: continue
    # Module-level constant
    sql = getattr(mod, "SQL", None)
    return sql if isinstance(sql, str) else None

def run_sql(cur, sql, label):
    if sql and sql.strip():
        cur.execute(sql)
        log(f"[{SCHEMA}] applied {label}")
    else:
        log(f"[{SCHEMA}] skipped {label}: empty SQL")

def main():
    # connect
    conn = None; last = None
    for _ in range(15):
        try:
            conn = psycopg2.connect(DB_URL); break
        except Exception as e:
            last = e
    if conn is None:
        log(f"[ss_] cannot connect to DB: {last}"); return 0

    conn.autocommit = True
    with conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

        # dynamic files first
        for path in sorted(glob.glob(os.path.join(VIEWS_DIR, "*.dynamic.py"))):
            name = os.path.basename(path)
            try:
                mod = load_module(path)
                sql = call_dynamic(mod, cur, conn, DB_URL)
                if not sql:
                    log(f"[{SCHEMA}] skipped {name}: no make_sql/render/get_sql/SQL found")
                    continue
                run_sql(cur, sql, name)
            except Exception as e:
                log(f"[{SCHEMA}] skipped {name}: {type(e).__name__}: {e}")

        # then plain .sql files
        for path in sorted(glob.glob(os.path.join(VIEWS_DIR, "*.sql"))):
            name = os.path.basename(path)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    sql = f.read()
                run_sql(cur, sql, name)
            except Exception as e:
                log(f"[{SCHEMA}] skipped {name}: {type(e).__name__}: {e}")

    log("[ss_] all view files applied"); return 0

if __name__ == "__main__":
    sys.exit(main())
