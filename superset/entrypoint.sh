#!/usr/bin/env bash
set -e
PORT_TO_BIND="${PORT:-8088}"

superset db upgrade

if [ ! -f /home/superset/.superset-initialized ]; then
  superset fab create-admin \
    --username "${SUPERSET_ADMIN_USER:-admin}" \
    --firstname Admin --lastname User \
    --email "${SUPERSET_ADMIN_EMAIL:-admin@nextpointtennis.com}" \
    --password "${SUPERSET_ADMIN_PASSWORD:-ChangeMe123!}" || true

  superset init
  touch /home/superset/.superset-initialized
fi

exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:"$PORT_TO_BIND" "superset.app:create_app()"