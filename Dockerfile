# If you’re using apache/superset base:
# FROM apache/superset:latest
# If you’re using python base + pip install superset, keep your current base:
FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

# Optional cache-buster so Render rebuilds fresh when needed
ARG BUILD_REV=dev
LABEL build_rev=${BUILD_REV}

# OS deps (no dos2unix)
USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates netbase tzdata curl \
        build-essential gcc \
        libpq-dev libssl-dev libffi-dev \
    ; rm -rf /var/lib/apt/lists/*

# Python deps (keep your pins)
RUN python -m pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
      apache-superset==3.1.0 \
      psycopg2-binary==2.9.9 \
      gunicorn==21.2.0 \
      gevent==24.2.1 \
      redis==5.0.8

# Files
WORKDIR /home/superset
RUN mkdir -p /home/superset/pythonpath /app
COPY superset_config.py /home/superset/pythonpath/superset_config.py
COPY start.sh /app/start.sh

# Normalize CRLF/BOM WITHOUT dos2unix; set exec bit; fix ownership
RUN sed -i '1s/^\xEF\xBB\xBF//' /app/start.sh \
 && sed -i 's/\r$//' /app/start.sh \
 && chmod +x /app/start.sh \
 && useradd -ms /bin/bash superset \
 && chown -R superset:superset /home/superset /app

USER superset
ENV SUPERSET_HOME=/home/superset PYTHONPATH=/home/superset/pythonpath
EXPOSE 8088
ENTRYPOINT ["/bin/bash", "/app/start.sh"]
