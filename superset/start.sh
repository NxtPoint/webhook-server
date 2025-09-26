#!/usr/bin/env bash
set -euo pipefail

echo "[start] Superset bootstrap starting..."

# --- sanity: show key envs (mask secrets) ---
echo "[env] SQLALCHEMY_DATABASE_URI present: $([[ -n "${SQLALCHEMY_DATABASE_URI:-${DATABASE_URL:-}}" ]] && echo yes || echo no)"
echo "[env] SUPERSET_SECRET_KEY present: $([[ -n "${SUPERSET_SECRET_KEY:-}" ]] && echo yes || echo no)"
echo "[env] REDIS_URL present: $([[ -n "${REDIS_URL:-}" ]] && echo yes || echo no))"
echo "[env] PYTHONPATH=$PYTHONPATH"
echo "[env] SUPERSET_HOME=${SUPERSET_HOME:-}"

# --- prefer SQLALCHEMY_DATABASE_URI over DATABASE_URL for Superset ---
export SQLALCHEMY_DATABASE_URI="${SQLALCHEMY_DATABASE_URI:-${DATABASE_URL:-}}"

if [[ -z "${SQLALCHEMY_DATABASE_URI:-}" ]]; then
  echo "[fatal] No SQLALCHEMY_DATABASE_URI or DATABASE_URL set. Aborting."
  exit 3
fi

# --- wait for Postgres up to ~60s ---
python - <<'PY'
import os, time, sys
import psycopg2
uri = os.environ["SQLALCHEMY_DATABASE_URI"]
for i in range(30):
    try:
        psycopg2.connect(uri).close()
        print("[ok] Postgres reachable")
        sys.exit(0)
    except Exception as e:
        print(f"[wait] Postgres not ready yet: {e}")
        time.sleep(2)
print("[fatal] Postgres not reachable after 60s"); sys.exit(3)
PY

# --- upgrade DB & init (idempotent) ---
echo "[migrate] superset db upgrade"
superset db upgrade

echo "[init] superset init"
superset init

# --- optional: create admin if envs provided ---
if [[ -n "${SUPERSET_ADMIN_USERNAME:-}" ]]; then
  echo "[admin] ensuring admin user exists"
  superset fab create-admin \
    --username "${SUPERSET_ADMIN_USERNAME}" \
    --firstname "${SUPERSET_ADMIN_FIRSTNAME:-Admin}" \
    --lastname  "${SUPERSET_ADMIN_LASTNAME:-User}" \
    --email     "${SUPERSET_ADMIN_EMAIL:-admin@example.com}" \
    --password  "${SUPERSET_ADMIN_PASSWORD:-admin}" || true
fi

# --- run gunicorn ---
echo "[run] gunicorn starting..."
exec gunicorn \
  -w "${GUNICORN_WORKERS:-3}" \
  -k gevent \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --bind 0.0.0.0:8088 \
  "superset.app:create_app()"
