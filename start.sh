#!/usr/bin/env bash
set -euo pipefail

echo "[start] booting webhook-server (preserve wsgi.py)"

# Use the same DB as your app
export DATABASE_URL="${DATABASE_URL:-${SQLALCHEMY_DATABASE_URI:-}}"
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "[fatal] No DATABASE_URL (or SQLALCHEMY_DATABASE_URI)."
  exit 3
fi

# Wait for Postgres (handles postgresql+psycopg2://)
python - <<'PY'
import os, re, time, sys
import psycopg2
dsn = os.environ["DATABASE_URL"]
dsn = re.sub(r"^postgresql\+[a-z0-9_]+://", "postgresql://", dsn, flags=re.I)
for i in range(30):
    try:
        psycopg2.connect(dsn).close()
        print("[ok] Postgres reachable"); break
    except Exception as e:
        print(f"[wait] attempt {i+1}/30: {e}"); time.sleep(2)
else:
    sys.exit("[fatal] Postgres not reachable")
PY

echo "[views] building SQL views via db_views.py (golden rule)"
python db_views.py

echo "[run] starting app via python wsgi.py (unchanged)"
exec python wsgi.py
