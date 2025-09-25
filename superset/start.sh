#!/usr/bin/env bash
set -e

PORT_TO_BIND="${PORT:-8088}"

banner() { echo "==================== $* ===================="; }

banner "RUNNING SUPERSET DB UPGRADE (with retries)"
for i in 1 2 3 4 5; do
  if superset db upgrade; then
    echo "DB upgrade successful"
    break
  fi
  echo "DB upgrade failed (attempt $i); sleeping 5s..."
  sleep 5
done

ensure_admin () {
  local USERNAME="$1"
  local EMAIL="$2"
  local PASSWORD="$3"

  banner "UPSERT ADMIN USER: $USERNAME"
  # Try to create; ignore error if exists
  superset fab create-admin \
    --username "$USERNAME" \
    --firstname Admin \
    --lastname User \
    --email "$EMAIL" \
    --password "$PASSWORD" || true

  # Always reset password + ensure active
  superset fab reset-password --username "$USERNAME" --password "$PASSWORD" || true

  # Print user list (FAB 4 prints a table)
  echo "Listing users after upsert for $USERNAME:"
  superset fab list-users || superset fab users list || true
}

# Preferred (from env) ? default values if env vars missing
PREF_USER="${SUPERSET_ADMIN_USER:-NxtPoint}"
PREF_EMAIL="${SUPERSET_ADMIN_EMAIL:-info@nextpointtennis.com}"
PREF_PASS="${SUPERSET_ADMIN_PASSWORD:-ChangeMe123!}"

ensure_admin "$PREF_USER" "$PREF_EMAIL" "$PREF_PASS"

# Fallback admin
ensure_admin "admin" "admin@nextpointtennis.com" "ChangeMe123!"

banner "RUNNING SUPERSET INIT"
superset init || true

banner "STARTING GUNICORN on $PORT_TO_BIND"
exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:"$PORT_TO_BIND" "superset.app:create_app()"