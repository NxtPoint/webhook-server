FROM apache/superset:3.1.0

# small tools
USER root
RUN mkdir -p /app /app/pythonpath \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata dos2unix \
 && rm -rf /var/lib/apt/lists/*

# marker so we can see this file was used
RUN echo "USING COPY VARIANT"

# config + start script
USER superset
WORKDIR /home/superset
COPY superset_config.py /app/pythonpath/superset_config.py
COPY start.sh /app/start.sh

# normalize & make executable
USER root
RUN dos2unix /app/start.sh && chmod +x /app/start.sh && chown superset:superset /app/start.sh
USER superset

# run our script
ENTRYPOINT ["/bin/bash","-lc","/app/start.sh"]