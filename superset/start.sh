#!/usr/bin/env bash
set -euo pipefail

echo "=== START.SH ==="

# Respect Render's PORT, default to 8088 locally
PORT_TO_BIND="${PORT:-8088}"

# 1) DB migrations (retry a few times in case DB is slow to wake)
echo "== DB upgrade =="
for i in 1 2 3 4 5; do
  if superset db upgrade; then break; fi
  echo "upgrade failed (attempt $i), sleeping 5s..." && sleep 5
done

# 2) Create admin only if NOT exists (no more forced resets)
ensure_admin () {
  local USERNAME="$1"; local EMAIL="$2"; local PASSWORD="$3"
  if ! superset fab list-users 2>/dev/null | grep -qiE "\\b${USERNAME}\\b"; then
    echo "== creating admin user ${USERNAME} =="
    superset fab create-admin --username "$USERNAME" --firstname Admin --lastname User \
      --email "$EMAIL" --password "$PASSWORD" || true
  else
    echo "== admin ${USERNAME} already exists; leaving password unchanged =="
  fi
}

if [[ -n "${SUPERSET_ADMIN_USERNAME:-}" && -n "${SUPERSET_ADMIN_EMAIL:-}" && -n "${SUPERSET_ADMIN_PASSWORD:-}" ]]; then
  ensure_admin "$SUPERSET_ADMIN_USERNAME" "$SUPERSET_ADMIN_EMAIL" "$SUPERSET_ADMIN_PASSWORD"
fi

# 3) Init (creates roles, defaults)
echo "== superset init =="
superset init || true

# 4) Run
echo "== starting gunicorn on ${PORT_TO_BIND} =="
exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:${PORT_TO_BIND} "superset.app:create_app()"
