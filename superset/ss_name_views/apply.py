#!/usr/bin/env python3
import os, sys, re, importlib.util, time, glob
import psycopg2

SCHEMA = "ss_"
VIEWS_DIR = os.path.join(os.path.dirname(__file__), "views")

# Pick a Postgres URL to run the views against (prefer the sportai_db)
DB_URL = (
    os.getenv("SS_VIEWS_DB_URL")
    or os.getenv("DATA_DB_URL")
    or os.getenv("DATABASE_URL")
    or os.getenv("SQLALCHEMY_DATABASE_URI")
)
if not DB_URL:
    print("[ss_] no DB URL; skipping", flush=True)
    sys.exit(0)

# Normalise driver prefixes e.g. postgresql+psycopg2:// -> postgresql://
DB_URL = re.sub(r"^postgresql\+\w+://", "postgresql://", DB_URL)

def log(msg): print(msg, flush=True)

def load_module(path: str):
    spec = importlib.util.spec_from_file_location("dyn", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def call_dynamic(mod, cur, conn, db_url):
    # Preferred API
    if hasattr(mod, "make_sql"):
        try:
            return mod.make_sql(cur)
        except TypeError:
            pass
    # Compatible fallbacks
    for name in ("render", "build", "get_sql"):
        fn = getattr(mod, name, None)
        if callable(fn):
            for arg in (cur, conn, db_url, None):
                try:
                    return fn() if arg is None else fn(arg)
                except TypeError:
                    continue
    sql = getattr(mod, "SQL", None)
    return sql if isinstance(sql, str) else None

def run_sql(cur, sql, label):
    if sql and str(sql).strip():
        cur.execute(sql)
        log(f"[{SCHEMA}] applied {label}")
    else:
        log(f"[{SCHEMA}] skipped {label}: no SQL")

def main():
    # connect with small retry so pre-deploy never flakes
    last_err = None
    conn = None
    for _ in range(15):
        try:
            conn = psycopg2.connect(DB_URL)
            conn.autocommit = True
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
    if conn is None:
        print(f"[ss_] cannot connect to DB: {last_err}", flush=True)
        sys.exit(1)

    with conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")

        files = []
        # Dynamic first (005_, 010_, …), then .sql (020_, 030_, …)
        files += sorted(glob.glob(os.path.join(VIEWS_DIR, "*.dynamic.py")))
        files += sorted(glob.glob(os.path.join(VIEWS_DIR, "*.sql")))

        for path in files:
            label = os.path.basename(path)
            try:
                if path.endswith(".dynamic.py"):
                    mod = load_module(path)
                    sql = call_dynamic(mod, cur, conn, DB_URL)
                else:
                    with open(path, "r", encoding="utf-8") as f:
                        sql = f.read()
                run_sql(cur, sql, label)
            except Exception as e:
                log(f"[{SCHEMA}] skipped {label}: {type(e).__name__}: {e}")
                # Clear any aborted-transaction state so next files still run
                try:
                    conn.rollback()
                except Exception:
                    pass

    log(f"[{SCHEMA}] all view files applied")

if __name__ == "__main__":
    main()
