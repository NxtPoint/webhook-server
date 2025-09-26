# Superset service (Render)

- Dockerfile builds from apache/superset:3.1.0
- start.sh runs migrations, creates admin **only if missing**, and launches gunicorn
- superset_config.py keeps CSP, feature flags, and no-op logger in code

## Environment (Render)
DATABASE_URL=postgresql+psycopg2://.../superset_meta
SUPERSET_SECRET_KEY=<long random>
SUPERSET_ADMIN_USERNAME=NxtPoint
SUPERSET_ADMIN_EMAIL=info@nextpointtennis.com
SUPERSET_ADMIN_PASSWORD=<set once; not reset on deploy>
# For temporary public testing:
PUBLIC_ROLE_LIKE=Gamma
