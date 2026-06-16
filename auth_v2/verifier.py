# auth_v2/verifier.py — provider-agnostic JWT (OIDC) verification via JWKS.
#
# Verifies a per-user IdP session token (Clerk by default; Auth0/Cognito work the
# same way — they all publish a JWKS and sign RS256 OIDC tokens). The result is a
# claims dict the caller maps onto core.app_user. This module NEVER touches the DB
# and is fully inert unless AUTH_V2_ENABLED=1.
#
# Clerk specifics (documented because they're non-obvious):
#   - Clerk's default session token sets `iss` to your Frontend API URL and does
#     NOT set `aud`. So we verify signature + iss + exp, and treat audience as
#     OPTIONAL (only enforced when AUTH_AUDIENCE is configured).
#   - The stable user id is the `sub` claim (e.g. "user_2ab..."). Map it to
#     core.app_user.auth_provider_uid with auth_provider="clerk".
#   - Email is not in the default token; add it via a Clerk JWT template
#     (custom claim `email`). We read `email` (falling back to a few common keys).
#
# Env (all blank/dark until the Clerk app exists):
#   AUTH_V2_ENABLED   "1" to enable any of this (else verify_jwt() returns None)
#   AUTH_PROVIDER     clerk | auth0 | cognito   (informational; default "clerk")
#   AUTH_JWKS_URL     JWKS endpoint, e.g. https://<frontend-api>/.well-known/jwks.json
#   AUTH_ISSUER       expected `iss`, e.g. https://<frontend-api>
#   AUTH_AUDIENCE     expected `aud` (OPTIONAL — leave blank for Clerk default tokens)
#   AUTH_JWT_LEEWAY   clock-skew tolerance in seconds (default 30)

import logging
import os

log = logging.getLogger("auth_v2.verifier")

# Lazily-built singletons (a JWKS client caches signing keys + does its own HTTP).
_jwks_client = None
_jwks_url_cached = None


def is_enabled():
    return os.getenv("AUTH_V2_ENABLED", "0") == "1"


def provider():
    return (os.getenv("AUTH_PROVIDER") or "clerk").strip().lower()


def _leeway():
    try:
        return int(os.getenv("AUTH_JWT_LEEWAY", "30"))
    except ValueError:
        return 30


def looks_like_jwt(token):
    """Cheap structural check so a Bearer value can be disambiguated from the
    legacy shared CLIENT_API_KEY without importing jwt. A compact JWS has three
    base64url segments separated by dots; the shared key does not."""
    if not token or token.count(".") != 2:
        return False
    return all(seg for seg in token.split("."))


def _get_jwks_client():
    """Build (once) a PyJWKClient for AUTH_JWKS_URL. Rebuilt if the env URL
    changes. Imports PyJWT lazily so this module imports even if the dep is
    somehow missing on a box that never enables auth_v2."""
    global _jwks_client, _jwks_url_cached
    jwks_url = (os.getenv("AUTH_JWKS_URL") or "").strip()
    if not jwks_url:
        return None
    if _jwks_client is not None and _jwks_url_cached == jwks_url:
        return _jwks_client
    from jwt import PyJWKClient  # lazy
    # lifespan: cache signing keys for an hour; PyJWKClient refetches on cache miss
    _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
    _jwks_url_cached = jwks_url
    return _jwks_client


def verify_jwt(token):
    """Verify a compact JWS and return its claims dict, or None on any failure.
    Returns None (never raises) so callers can cleanly fall back to legacy auth.
    No-op (None) unless AUTH_V2_ENABLED=1."""
    if not is_enabled():
        return None
    if not looks_like_jwt(token):
        return None

    issuer = (os.getenv("AUTH_ISSUER") or "").strip() or None
    audience = (os.getenv("AUTH_AUDIENCE") or "").strip() or None

    try:
        import jwt  # lazy import (PyJWT)
        client = _get_jwks_client()
        if client is None:
            log.warning("auth_v2: AUTH_JWKS_URL not set; cannot verify JWT")
            return None
        signing_key = client.get_signing_key_from_jwt(token).key
        options = {
            # aud is optional for Clerk default tokens — only require it when configured
            "require": ["exp", "iss"],
            "verify_aud": audience is not None,
        }
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            leeway=_leeway(),
            options=options,
        )
        return claims
    except Exception as e:  # InvalidTokenError, JWKS fetch failure, etc.
        log.info("auth_v2: JWT verification failed: %s", e.__class__.__name__)
        return None


def claim_uid(claims):
    """The stable external user id → core.app_user.auth_provider_uid. `sub` for
    all three providers (Clerk user_*, Auth0 sub, Cognito sub)."""
    return claims.get("sub") if claims else None


def claim_email(claims):
    """Best-effort email from the token. Clerk needs a JWT template exposing
    `email`; Auth0/Cognito commonly include `email` directly."""
    if not claims:
        return None
    for k in ("email", "email_address", "primary_email", "https://email"):
        v = claims.get(k)
        if v:
            return str(v).strip().lower()
    return None


def claim_email_verified(claims):
    if not claims:
        return None
    v = claims.get("email_verified")
    if v is None:
        return None
    return bool(v) if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")
