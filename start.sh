#!/usr/bin/env bash
set -euo pipefail

echo "[start] webhook-server boot"

# Ensure DATABASE_URL is available (fallback from SQLALCHEMY_DATABASE_URI if needed)
export DATABASE_URL="${DATABASE_URL:-${SQLALCHEMY_DATABASE_URI:-}}"
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "[fatal] No DATABASE_URL (or SQLALCHEMY_DATABASE_URI) set. Aborting."
  exit 3
fi

# Wait for Postgres (normalizes postgresql+psycopg2:// to postgresql://)
python - <<'PY'
import os, re, time, sys
import psycopg2
dsn = os.environ["DATABASE_URL"]
dsn = re.sub(r"^postgresql\+[a-z0-9_]+://", "postgresql://", dsn, flags=re.I)
for i in range(30):
    try:
        psycopg2.connect(dsn).close()
        print("[ok] Postgres reachable")
        break
    except Exception as e:
        print(f"[wait] attempt {i+1}/30: {e}")
        time.sleep(2)
else:
    sys.exit("[fatal] Postgres not reachable")
PY

echo "[views] building SQL views via db_views.py (golden rule)"
python db_views.py

echo "[run] gunicorn starting..."
exec gunicorn wsgi:app --bind 0.0.0.0:${PORT:-8000} --workers ${GUNICORN_WORKERS:-2} --threads 8 --timeout 120
