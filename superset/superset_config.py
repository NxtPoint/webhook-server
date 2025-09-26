# pyright: reportMissingImports=false
import os
from importlib import import_module

# Try to import the real AbstractEventLogger when available;
# otherwise provide a tiny fallback so editors don't complain.
try:
    AbstractEventLogger = import_module("superset.utils.log").AbstractEventLogger  # type: ignore[attr-defined]
except Exception:
    class AbstractEventLogger:  # fallback stub for local editing
        def log(self, *args, **kwargs):
            pass

# -------------------- Secrets / DB --------------------
SECRET_KEY = os.getenv("SUPERSET_SECRET_KEY", "change-me")

# Accept either env var; prefer SQLALCHEMY_DATABASE_URI if set
SQLALCHEMY_DATABASE_URI = os.getenv(
    "SQLALCHEMY_DATABASE_URI",
    os.getenv("DATABASE_URL"),
)

# Keep ORM search_path predictable
SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"options": "-csearch_path=public"}}

# -------------------- Redis (cache + celery) --------------------
# Set REDIS_URL in Render, e.g. rediss://:password@host:6379/0
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Celery broker & backend (for async tasks, alerts/reports)
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# Caches (dashboard + query data cache)
CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_REDIS_URL": REDIS_URL,
}
DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_REDIS_URL": REDIS_URL,
}
# Rate limiting storage: use the same Redis
RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", REDIS_URL)
# Optional tuning:
RATELIMIT_ENABLED = True
RATELIMIT_DEFAULT = "200 per minute"

# -------------------- Security & embedding --------------------
# TEMP public viewing (set env PUBLIC_ROLE_LIKE=Gamma only for testing; unset for prod)
PUBLIC_ROLE_LIKE = os.getenv("PUBLIC_ROLE_LIKE")

# Allow only your sites to embed (tune these)
ALLOWED_FRAME_DOMAINS = [
    "https://*.wixsite.com",
    "https://*.wix.com",
    "https://nextpointtennis.com",
    "https://www.nextpointtennis.com",
]

TALISMAN_ENABLED = True
CSP = {
    "default-src": ["'self'"],
    "img-src": ["'self'", "data:", "blob:"],
    "style-src": ["'self'", "'unsafe-inline'"],
    "script-src": ["'self'", "'unsafe-inline'"],
    "frame-ancestors": ["'self'"] + ALLOWED_FRAME_DOMAINS,
}
TALISMAN_CONFIG = {
    "content_security_policy": CSP,
    "force_https": True,
    "frame_options": None,
    "session_cookie_secure": True,
}
CONTENT_SECURITY_POLICY_WARNING = False

# -------------------- Feature flags --------------------
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
    "ALERT_REPORTS": False,  # enables Celery-driven alerts/reports
}

# Optional Mapbox
MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY", "")

# -------------------- Event logger --------------------
# No-op event logger (avoid writing to logs table)
class _NoOpEventLogger(AbstractEventLogger):
    def log(self, *args, **kwargs):
        return

EVENT_LOGGER = _NoOpEventLogger()

# Nice default
DEFAULT_HOME_PAGE = "/dashboard/list/"
