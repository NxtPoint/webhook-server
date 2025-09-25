import os

SECRET_KEY = os.getenv("SUPERSET_SECRET_KEY", "CHANGE_ME_IN_RENDER")
SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")

# 1) Force Alembic/SQLAlchemy to use the 'public' schema
SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"options": "-csearch_path=public"}}

# 2) Minimal CSP via Talisman (allow your Wix domains to embed)
ALLOWED_FRAME_DOMAINS = [
    "https://*.wixsite.com",
    "https://*.editorx.io",
    "https://*.nextpointtennis.com",
    "https://nextpointtennis.com",
]
TALISMAN_ENABLED = True
CSP = {
    "default-src": ["'self'"],
    "img-src": ["'self'", "data:", "blob:"],
    "font-src": ["'self'", "data:"],
    "style-src": ["'self'", "'unsafe-inline'"],
    "script-src": ["'self'", "'unsafe-eval'"],
    "connect-src": ["'self'"],
    "frame-ancestors": ["'self'"] + ALLOWED_FRAME_DOMAINS,
    "object-src": ["'none'"],
}
TALISMAN_CONFIG = {
    "content_security_policy": CSP,
    "force_https": True,
    "frame_options": None,
    "session_cookie_secure": True,
}
CONTENT_SECURITY_POLICY_WARNING = False  # hide CSP warning now that we set one

# 3) TEMP: turn off DB event logger so 'logs' table writes don't crash
EVENT_LOGGER = None

# Feature flags you wanted
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
}

# (Optional) Mapbox key if you use maps
MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY", "")# ---- Disable DB event logging with a no-op logger (avoids writing to "logs" table)
from superset.utils.log import AbstractEventLogger

class _NoOpEventLogger(AbstractEventLogger):
    def log(self, **kwargs):
        # swallow all events
        return

EVENT_LOGGER = _NoOpEventLogger()
from superset.utils.log import AbstractEventLogger

class _NoOpEventLogger(AbstractEventLogger):
    def log(self, *args, **kwargs):
        # swallow all events regardless of signature
        return

EVENT_LOGGER = _NoOpEventLogger()

# Optional: skip welcome page to avoid touching logs during first visits
DEFAULT_HOME_PAGE = "/dashboard/list/"
# --- EMBEDDING FOR WIX (iframe) ---
ENABLE_CORS = True
CORS_OPTIONS = {
    "supports_credentials": True,
    "origins": [
        "https://*.wixsite.com",
        "https://*.editorx.io",
        "https://*.wix.com",
        "https://webhook-server-4nsr.onrender.com"  # this service itself
    ],
}

# Use CSP to allow Wix to host the iframe
TALISMAN_ENABLED = True
TALISMAN_CONFIG = {
    "content_security_policy": {
        "default-src": ["'self'"],
        "img-src": ["'self'", "data:", "blob:"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "script-src": ["'self'", "'unsafe-inline'"],
        "frame-src": ["'self'"],
        # <-- IMPORTANT: pages allowed to embed Superset
        "frame-ancestors": [
            "https://*.wixsite.com",
            "https://*.editorx.io",
            "https://*.wix.com"
            # add your custom domain here later, e.g. "https://www.yourdomain.com"
        ],
    }
}
CONTENT_SECURITY_POLICY_WARNING = False

# (Optional) make content readable without login (public view). Comment out if you want login.
# PUBLIC_ROLE_LIKE = "Gamma"
