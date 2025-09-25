import os
SECRET_KEY = os.getenv("SUPERSET_SECRET_KEY", "CHANGE_ME_IN_RENDER")
SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")  # postgresql+psycopg2://...
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
}
TALISMAN_ENABLED = False
ALLOW_IFRAME = True
ALLOWED_FRAME_DOMAINS = [
    "https://*.wixsite.com",
    "https://*.editorx.io",
    "https://*.nextpointtennis.com",
    "https://nextpointtennis.com",
]
ENABLE_CORS = True
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "expose_headers": ["*"],
    "resources": ["/*"],
    "origins": [
        "https://*.wixsite.com",
        "https://*.editorx.io",
        "https://*.nextpointtennis.com",
        "https://nextpointtennis.com",
    ],
}
MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY", "")# Ensure migrations target the public schema
SQLALCHEMY_ENGINE_OPTIONS = {
    "connect_args": {
        "options": "-csearch_path=public"
    }
}
# Ensure migrations use the public schema
SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"options": "-csearch_path=public"}}
