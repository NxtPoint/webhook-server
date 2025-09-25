FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

# OS deps
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates netbase tzdata curl \
        build-essential gcc \
        libpq-dev libssl-dev libffi-dev \
        dos2unix \
    ; rm -rf /var/lib/apt/lists/*

# Python deps (pinned; let Superset resolve Flask-Limiter)
RUN python -m pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
      apache-superset==3.1.0 \
      psycopg2-binary==2.9.9 \
      gunicorn==21.2.0 \
      gevent==24.2.1

# Workdir & files
WORKDIR /home/superset
RUN mkdir -p /home/superset/pythonpath /app
COPY superset_config.py /home/superset/pythonpath/superset_config.py
COPY entrypoint.sh /app/entrypoint.sh

# Normalize script (strip BOM/CRLF) and make executable
RUN sed -i '1s/^\xEF\xBB\xBF//' /app/entrypoint.sh && \
    dos2unix /app/entrypoint.sh && \
    chmod +x /app/entrypoint.sh

# Create non-root, then chown files
RUN useradd -ms /bin/bash superset && chown -R superset:superset /home/superset /app
USER superset

EXPOSE 8088
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]