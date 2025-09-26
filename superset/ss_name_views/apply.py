#!/usr/bin/env python3
import os, sys, glob, re, importlib.util, psycopg2

SCHEMA="ss_"
VIEWS_DIR=os.path.join(os.path.dirname(__file__),"views")
raw=os.getenv("SS_VIEWS_DB_URL") or os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URI")
if not raw:
    print("[ss_] no DB URL; skipping", flush=True); sys.exit(0)
DB_URL=re.sub(r"^postgresql\+\w+://","postgresql://", raw)

def run_sql(cur, sql, label):
    try:
        if sql and str(sql).strip():
            cur.execute(sql); print(f"[{SCHEMA}] applied {label}", flush=True)
        else:
            print(f"[{SCHEMA}] skipped {label}: empty", flush=True)
    except Exception as e:
        print(f"[{SCHEMA}] skipped {label}: {type(e).__name__}: {e}", flush=True)

def dyn_sql(path, cur):
    spec=importlib.util.spec_from_file_location("dyn", path)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    for name in ("make_sql","render","build","get_sql"):
        fn=getattr(m,name,None)
        if callable(fn):
            for arg in (cur, None):
                try: return fn() if arg is None else fn(arg)
                except TypeError: pass
    s=getattr(m,"SQL",None)
    return s if isinstance(s,str) else None

def main():
    with psycopg2.connect(DB_URL) as conn:
        conn.autocommit=True
        with conn.cursor() as cur:
            cur.execute(f"create schema if not exists {SCHEMA};")
            for p in sorted(glob.glob(os.path.join(VIEWS_DIR,"*.dynamic.py"))):
                run_sql(cur, dyn_sql(p,cur), os.path.basename(p))
            for p in sorted(glob.glob(os.path.join(VIEWS_DIR,"*.sql"))):
                run_sql(cur, open(p,encoding="utf-8").read(), os.path.basename(p))
    print("[ss_] all view files applied", flush=True); return 0

if __name__=="__main__":
    try: sys.exit(main())
    except Exception as e:
        print(f"[ss_] fatal: {e}", flush=True); sys.exit(0)
