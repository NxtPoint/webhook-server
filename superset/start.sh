#!/usr/bin/env bash
set -e

PORT_TO_BIND="${PORT:-8088}"

echo "==> Running Superset DB upgrade (with retries)..."
for i in 1 2 3 4 5; do
  if superset db upgrade; then
    echo "==> DB upgrade successful"
    break
  fi
  echo "==> DB upgrade failed (attempt $i); sleeping 5s and retrying..."
  sleep 5
done

# Ensure admin user from env exists; if it exists, reset the password
if [ -n "$SUPERSET_ADMIN_USER" ] && [ -n "$SUPERSET_ADMIN_PASSWORD" ] && [ -n "$SUPERSET_ADMIN_EMAIL" ]; then
  echo "==> Ensuring admin user $SUPERSET_ADMIN_USER"
  superset fab create-admin \
    --username "$SUPERSET_ADMIN_USER" \
    --firstname Admin \
    --lastname User \
    --email "$SUPERSET_ADMIN_EMAIL" \
    --password "$SUPERSET_ADMIN_PASSWORD" || true

  # Reset password in case user already existed
  superset fab reset-password \
    --username "$SUPERSET_ADMIN_USER" \
    --password "$SUPERSET_ADMIN_PASSWORD" || true
fi

echo "==> Running superset init..."
superset init || true

echo "==> Starting Gunicorn..."
exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:"$PORT_TO_BIND" "superset.app:create_app()"