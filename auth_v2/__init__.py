# auth_v2 — per-user IdP (Clerk) token auth, replacing the shared CLIENT_API_KEY.
#
# DARK BY DEFAULT. Nothing here changes request handling unless AUTH_V2_ENABLED=1,
# and even importing the package is side-effect free. The Phase-0 contract:
#   - JWT (Bearer) tokens are verified and mapped onto core.app_user, with the
#     account/email derived server-side (the #1 security fix, ARCHITECTURE §6.1).
#   - The legacy shared-key + ?email path keeps working unchanged during the
#     transition, so the live Wix login is never broken.
#
# Wiring (done in the integration commit, not here): client_api._guard() delegates
# to resolve_principal(); upload_app boot calls init_auth_v2(app).

import logging

from auth_v2.principal import Principal, resolve_principal
from auth_v2.verifier import is_enabled, provider

__all__ = ["Principal", "resolve_principal", "is_enabled", "provider", "init_auth_v2"]

log = logging.getLogger("auth_v2")


def init_auth_v2(app):
    """Boot hook — logs the auth_v2 state so it's visible in Render logs. Safe to
    call unconditionally; does nothing observable when AUTH_V2_ENABLED!=1."""
    if is_enabled():
        app.logger.info("auth_v2 ENABLED (provider=%s) — per-user JWT auth active alongside legacy key",
                        provider())
    else:
        app.logger.info("auth_v2 dark (AUTH_V2_ENABLED!=1) — legacy CLIENT_API_KEY auth only")
    return app
