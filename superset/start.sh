#!/usr/bin/env bash
set -euo pipefail

echo "[start] Superset bootstrap starting..."

# Prefer SQLALCHEMY_DATABASE_URI over DATABASE_URL for Superset itself
export SQLALCHEMY_DATABASE_URI="${SQLALCHEMY_DATABASE_URI:-${DATABASE_URL:-}}"
# Tell the CLI which Flask app to load
export FLASK_APP="superset.app:create_app()"

# --- sanity logs (mask secrets) ---
echo "[env] SQLALCHEMY_DATABASE_URI present: $([[ -n "${SQLALCHEMY_DATABASE_URI:-}" ]] && echo yes || echo no)"
echo "[env] SUPERSET_SECRET_KEY present: $([[ -n "${SUPERSET_SECRET_KEY:-}" ]] && echo yes || echo no)"
echo "[env] REDIS_URL present: $([[ -n "${REDIS_URL:-}" ]] && echo yes || echo no)"
echo "[env] RATELIMIT_STORAGE_URI present: $([[ -n "${RATELIMIT_STORAGE_URI:-}" ]] && echo yes || echo no)"
echo "[env] PYTHONPATH=${PYTHONPATH:-}"

if [[ -z "${SQLALCHEMY_DATABASE_URI:-}" ]]; then
  echo "[fatal] No SQLALCHEMY_DATABASE_URI or DATABASE_URL set. Aborting."
  exit 3
fi

# --- wait for Postgres up to ~60s (normalize SQLAlchemy URI for psycopg2) ---
python - <<'PY'
import os, time, sys, re, traceback
import psycopg2

raw = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ.get("DATABASE_URL") or ""
print(f"[wait] raw DB url present: {bool(raw)}")

# Convert "postgresql+psycopg2://..." -> "postgresql://"
dsn = re.sub(r"^postgresql\+[a-z0-9_]+://", "postgresql://", raw, flags=re.I)
print(f"[wait] connecting with psycopg2 DSN startswith: {dsn.split('?')[0][:60]}...")

last_err = None
for i in range(30):
    try:
        conn = psycopg2.connect(dsn)
        conn.close()
        print("[ok] Postgres reachable")
        sys.exit(0)
    except Exception as e:
        last_err = e
        print(f"[wait] attempt {i+1}/30: not ready yet: {e}")
        time.sleep(2)

print("[fatal] Postgres not reachable after 60s")
if last_err:
    traceback.print_exception(type(last_err), last_err, last_err.__traceback__)
sys.exit(3)
PY

# --- GOLDEN RULE: rebuild analytics views from Python before Superset starts ---
# We run db_views.py against the same DB so Silver/Gold views are always up to date.
export DATABASE_URL="${DATABASE_URL:-${SQLALCHEMY_DATABASE_URI:-}}"
if [[ -f "/app/db_views.py" ]]; then
  echo "[views] building SQL views via /app/db_views.py"
  python /app/db_views.py
else
  # Fallback if your repo mounts under /app/superset/ and code root is one level up
  if [[ -f "/app/superset/../db_views.py" ]]; then
    echo "[views] building SQL views via /app/db_views.py (relative)"
    python /app/superset/../db_views.py
  else
    echo "[warn] db_views.py not found — skipping view build"
  fi
fi

# --- upgrade DB & init (idempotent) ---
echo "[migrate] superset db upgrade"
superset db upgrade

echo "[init] superset init"
superset init

# --- create admin (idempotent: ignore 'already exists') ---
if [[ -n "${SUPERSET_ADMIN_USERNAME:-${SUPERSET_ADMIN_USER:-}}" ]]; then
  superset fab create-admin \
    --username "${SUPERSET_ADMIN_USERNAME:-${SUPERSET_ADMIN_USER}}" \
    --firstname "${SUPERSET_ADMIN_FIRSTNAME:-Admin}" \
    --lastname  "${SUPERSET_ADMIN_LASTNAME:-User}" \
    --email     "${SUPERSET_ADMIN_EMAIL:-admin@example.com}" \
    --password  "${SUPERSET_ADMIN_PASSWORD:-admin}" || true
fi

# --- run gunicorn ---
echo "[run] gunicorn starting..."
exec gunicorn \
  -w "${GUNICORN_WORKERS:-1}" \
  -k gevent \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --max-requests "${GUNICORN_MAX_REQUESTS:-200}" \
  --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-50}" \
  --bind 0.0.0.0:8088 \
  "superset.app:create_app()"
