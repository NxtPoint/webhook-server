import os
from superset.utils.log import AbstractEventLogger

# Secrets/DB come from Render env
SECRET_KEY = os.getenv("SUPERSET_SECRET_KEY", "change-me")
SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")

# Keep ORM search_path predictable
SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"options": "-csearch_path=public"}}

# ---------- Security & embedding ----------
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

# Feature flags commonly useful
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
}

# Optional Mapbox
MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY", "")

# No-op event logger (avoid writing to logs table)
class _NoOpEventLogger(AbstractEventLogger):
    def log(self, *args, **kwargs):
        return
EVENT_LOGGER = _NoOpEventLogger()

# Nice default
DEFAULT_HOME_PAGE = "/dashboard/list/"
