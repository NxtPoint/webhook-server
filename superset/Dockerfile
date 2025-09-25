FROM apache/superset:3.1.0

# (Optional) add tiny tools; also make sure pythonpath exists
USER root
RUN mkdir -p /app/pythonpath \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata dos2unix \
 && rm -rf /var/lib/apt/lists/*

# Back to app user
USER superset
WORKDIR /home/superset

# Put your config where Superset looks for it
# (apache image already sets PYTHONPATH to /app/pythonpath)
COPY superset_config.py /app/pythonpath/superset_config.py