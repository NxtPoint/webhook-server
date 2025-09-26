#!/usr/bin/env python3
import os, sys, glob, importlib.util
import psycopg2

BASE_DIR = os.path.dirname(__file__)
VIEWS_DIR = os.path.join(BASE_DIR, "views")

DB_URL = os.environ.get("SS_VIEWS_DB_URL") or os.environ.get("DATABASE_URL")

def log(msg): print(msg, flush=True)

def run_sql(cur, sql):
    if sql and str(sql).strip():
        cur.execute(sql)

def load_module(path):
    spec = importlib.util.spec_from_file_location("dyn", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def call_dynamic(mod, cur, conn, db_url):
    # Try common function names/signatures in order
    for name in ("make_sql", "render", "build", "get_sql"):
        fn = getattr(mod, name, None)
        if callable(fn):
            for arg in (cur, conn, db_url, None):
                try:
                    return fn() if arg is None else fn(arg)
                except TypeError:
                    continue
    # Or allow module-level SQL = "...";
    sql_attr = getattr(mod, "SQL", None)
    if isinstance(sql_attr, str):
        return sql_attr
    return None

def main():
    if not DB_URL:
        log("[ss_] WARNING: SS_VIEWS_DB_URL/DATABASE_URL not set; nothing to do.")
        return 0

    py_dyn = sorted(glob.glob(os.path.join(VIEWS_DIR, "*.dynamic.py")))
    sql_files = sorted(glob.glob(os.path.join(VIEWS_DIR, "*.sql")))

    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    for path in py_dyn:
        name = os.path.basename(path)
        try:
            mod = load_module(path)
            sql = call_dynamic(mod, cur, conn, DB_URL)
            if not sql:
                log(f"[ss_] skipped {name}: no make_sql/render/get_sql/SQL found")
                continue
            run_sql(cur, sql)
            log(f"[ss_] applied {name}")
        except Exception as e:
            log(f"[ss_] skipped {name}: {e.__class__.__name__}: {e}")

    for path in sql_files:
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                sql = f.read()
            run_sql(cur, sql)
            log(f"[ss_] applied {name}")
        except Exception as e:
            log(f"[ss_] skipped {name}: {e.__class__.__name__}: {e}")

    cur.close()
    conn.close()
    log("[ss_] all view files applied")
    return 0

if __name__ == "__main__":
    sys.exit(main())
