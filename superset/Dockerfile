FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

# OS deps (note the "\" line continuations)
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates netbase tzdata curl \
        build-essential gcc \
        libpq-dev libssl-dev libffi-dev \
    ; rm -rf /var/lib/apt/lists/*

# Python deps (pinned)
RUN pip install --no-cache-dir \
      apache-superset==3.1.0 \
      psycopg2-binary==2.9.9 \
      gunicorn==22.0.0 \
      gevent==24.2.1

# Non-root user
RUN useradd -ms /bin/bash superset
USER superset
WORKDIR /home/superset

# Put config on PYTHONPATH and copy entrypoint
RUN mkdir -p /home/superset/pythonpath /app
ENV PYTHONPATH="/home/superset/pythonpath:"
COPY --chown=superset:superset superset_config.py /home/superset/pythonpath/superset_config.py
COPY --chown=superset:superset entrypoint.sh /app/entrypoint.sh

# Ensure entrypoint is executable at build time
USER root
RUN chmod +x /app/entrypoint.sh
USER superset

EXPOSE 8088
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]