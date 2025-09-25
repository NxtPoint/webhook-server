#!/usr/bin/env bash
set -e
PORT_TO_BIND=""
superset db upgrade
if [ ! -f /home/superset/.superset-initialized ]; then
  superset fab create-admin \
    --username "" \
    --firstname Admin --lastname User \
    --email "" \
    --password "" || true
  superset init
  touch /home/superset/.superset-initialized
fi
exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:"" "superset.app:create_app()"