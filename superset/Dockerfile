FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8
ENV SUPERSET_HOME=/home/superset PYTHONPATH=/home/superset/pythonpath FLASK_APP="superset.app:create_app()"

# Cache-buster: bump this value any time to force a full rebuild on Render
LABEL build_rev="2025-09-26a"

# ---- OS deps (no dos2unix) ----
USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates netbase tzdata curl \
        build-essential gcc \
        libpq-dev libssl-dev libffi-dev \
    ; rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
RUN python -m pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
      apache-superset==3.1.0 \
      Flask-Limiter==3.7.0 \
      marshmallow==3.20.1 \
      psycopg2-binary==2.9.9 \
      gunicorn==21.2.0 \
      gevent==24.2.1


# ---- Files ----
WORKDIR /home/superset
RUN mkdir -p /home/superset/pythonpath /app
COPY superset_config.py /home/superset/pythonpath/superset_config.py
COPY start.sh /app/start.sh
COPY ss_name_views /home/superset/ss_name_views

RUN sed -i '1s/^\xEF\xBB\xBF//' /app/start.sh \
 && sed -i 's/\r$//' /app/start.sh \
 && chmod +x /app/start.sh \
 && useradd -ms /bin/bash superset \
 && chown -R superset:superset /home/superset /app

USER superset
ENV SUPERSET_HOME=/home/superset PYTHONPATH=/home/superset/pythonpath
EXPOSE 8088
ENTRYPOINT ["/bin/bash", "/app/start.sh"]

