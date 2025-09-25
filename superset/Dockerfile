FROM apache/superset:3.1.0

# Tools & dirs
USER root
RUN mkdir -p /app /app/pythonpath \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata dos2unix \
 && rm -rf /var/lib/apt/lists/*

# Your Superset config (keeps Wix/CSP/etc.)
USER superset
WORKDIR /home/superset
COPY superset_config.py /app/pythonpath/superset_config.py

# --- Write start.sh straight into the image ---
USER root
RUN bash -lc 'cat > /app/start.sh <<\"SH\" \n\
#!/usr/bin/env bash\n\
set -e\n\
echo \"==================== START.SH EXECUTING ====================\"\n\
PORT_TO_BIND=\"\\"\n\
echo \"==================== RUNNING SUPERSET DB UPGRADE (with retries) ====================\"\n\
for i in 1 2 3 4 5; do\n\
  if superset db upgrade; then echo \"DB upgrade successful\"; break; fi\n\
  echo \"DB upgrade failed (attempt \); sleeping 5s...\"; sleep 5\n\
done\n\
ensure_admin () {\n\
  local USERNAME=\"\\"; local EMAIL=\"\\"; local PASSWORD=\"\\"\n\
  echo \"==================== UPSERT ADMIN USER: \ ====================\"\n\
  superset fab create-admin --username \"\\" --firstname Admin --lastname User --email \"\\" --password \"\\" || true\n\
  superset fab reset-password --username \"\\" --password \"\\" || true\n\
  (superset fab list-users || superset fab users list || true) 2>/dev/null\n\
}\n\
PREF_USER=\"\\"; PREF_EMAIL=\"\\"; PREF_PASS=\"\\"\n\
ensure_admin \"\\" \"\\" \"\\"\n\
ensure_admin \"admin\" \"admin@nextpointtennis.com\" \"ChangeMe123!\"\n\
echo \"==================== superset init ====================\"\n\
superset init || true\n\
echo \"==================== starting gunicorn on \ ====================\"\n\
exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:\"\\" \"superset.app:create_app()\"\n\
SH' \
 && dos2unix /app/start.sh \
 && chmod +x /app/start.sh \
 && chown superset:superset /app/start.sh

USER superset

# Force container to run our script
ENTRYPOINT ["/bin/bash","-lc","/app/start.sh"]