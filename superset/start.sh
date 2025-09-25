#!/usr/bin/env bash
set -e

PORT_TO_BIND="${PORT:-8088}"

echo "==> Running Superset DB upgrade..."
# If the DB is momentarily unavailable, retry a few times
for i in 1 2 3 4 5; do
  if superset db upgrade; then
    echo "==> DB upgrade successful"
    break
  fi
  echo "==> DB upgrade failed (attempt $i); sleeping 5s and retrying..."
  sleep 5
done

# Create admin from env if not present (idempotent)
if [ -n "$SUPERSET_ADMIN_USER" ] && [ -n "$SUPERSET_ADMIN_PASSWORD" ] && [ -n "$SUPERSET_ADMIN_EMAIL" ]; then
  superset fab create-admin \
    --username "$SUPERSET_ADMIN_USER" \
    --firstname Admin \
    --lastname User \
    --email "$SUPERSET_ADMIN_EMAIL" \
    --password "$SUPERSET_ADMIN_PASSWORD" || true
fi

echo "==> Running superset init..."
superset init || true

echo "==> Starting Gunicorn..."
exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:"$PORT_TO_BIND" "superset.app:create_app()"