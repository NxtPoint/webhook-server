FROM apache/superset:3.1.0

COPY superset_config.py /app/pythonpath/superset_config.py
COPY start.sh            /app/start.sh

RUN apt-get update && apt-get install -y --no-install-recommends dos2unix && rm -rf /var/lib/apt/lists/* \
 && dos2unix /app/start.sh \
 && chmod +x /app/start.sh \
 && chown superset:superset /app/start.sh

USER superset
EXPOSE 8088
ENTRYPOINT ["/app/start.sh"]
