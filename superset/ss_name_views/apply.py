#!/usr/bin/env python3
import os, sys, glob, importlib.util
import psycopg2

BASE_DIR = os.path.dirname(__file__)
VIEWS_DIR = os.path.join(BASE_DIR, "views")

DB_URL = (
    os.environ.get("SS_VIEWS_DB_URL")
    or os.environ.get("DATABASE_URL")
)

def _log(msg):
    print(msg, flush=True)

def _run_sql(cur, sql):
    if sql and sql.strip():
        cur.execute(sql)

def _load_module(path):
    spec = importlib.util.spec_from_file_location("dyn", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def _call_dynamic(mod, cur, conn, db_url):
    # Try common function names and signatures
    for name in ("make_sql", "render", "build", "get_sql"):
        fn = getattr(mod, name, None)
        if callable(fn):
            # Try with different parameters
            for arg in (cur, conn, db_url, None):
                try:
                    if arg is None:
                        return fn()
                    else:
                        return fn(arg)
                except TypeError:
                    continue
    # Or allow a module-level constant/variable
    sql = getattr(mod, "SQL", None)
    return sql if isinstance(sql, str) else None

def main():
    if not DB_URL:
        _log("[ss_] WARNING: SS_VIEWS_DB_URL/DATABASE_URL not set; nothing to do.")
        return 0

    py_dyn = sorted(glob.glob(os.path.join(VIEWS_DIR, "*.dynamic.py")))
    sql_files = sorted(glob.glob(os.path.join(VIEWS_DIR, "*.sql")))

    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    # Dynamic files first
    for path in py_dyn:
        name = os.path.basename(path)
        try:
            mod = _load_module(path)
            sql = _call_dynamic(mod, cur, conn, DB_URL)
            if not sql:
                _log(f"[ss_] skipped {name}: no make_sql/render/get_sql/SQL found")
                continue
            _run_sql(cur, sql)
            _log(f"[ss_] applied {name}")
        except Exception as e:
            _log(f"[ss_] skipped {name}: {e.__class__.__name__}: {e}")

    # Then plain .sql files
    for path in sql_files:
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                sql = f.read()
            _run_sql(cur, sql)
            _log(f"[ss_] applied {name}")
        except Exception as e:
            _log(f"[ss_] skipped {name}: {e.__class__.__name__}: {e}")

    cur.close()
    conn.close()
    _log("[ss_] all view files applied")
    return 0

if __name__ == "__main__":
    sys.exit(main())
