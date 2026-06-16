# auth_v2/principal.py — resolve an authenticated principal from a request.
#
# Two methods, tried in this order:
#   1. JWT (per-user IdP token) — ONLY when AUTH_V2_ENABLED=1. Verify the Bearer
#      token, resolve core.app_user by (auth_provider, sub), derive account/email
#      SERVER-SIDE. The client can no longer assert which account it is.
#   2. Legacy — the shared CLIENT_API_KEY (header or Bearer) + ?email param.
#      Identical to client_api._guard() today; touches NO database.
#
# Disambiguation: a Bearer value that structurally looks like a JWT is treated as
# a JWT (verified or rejected — never silently downgraded to the shared-key path);
# a Bearer value that is the shared key falls through to the legacy path. With
# AUTH_V2_ENABLED=0 the JWT branch is skipped entirely → behaviour is exactly as
# it is in production today.

import hmac
import logging
import os
from dataclasses import dataclass
from typing import Optional

from core_db.db import norm_email, session_scope
from core_db.repositories import accounts

from auth_v2 import verifier

log = logging.getLogger("auth_v2.principal")

# Mirror client_api's constants (kept here to avoid a circular import — client_api
# imports auth_v2 in the wiring commit, not the other way round). ADMIN_EMAILS is
# still the admin gate during the transition; moving admin to core.* roles is the
# later "harden" phase, not Phase 0.
ADMIN_EMAILS = {"info@ten-fifty5.com", "tomo.stojakovic@gmail.com"}


def _client_api_key():
    return os.environ.get("CLIENT_API_KEY", "").strip()


@dataclass
class Principal:
    email: Optional[str]
    method: str                       # "jwt" | "legacy"
    account_id: Optional[int] = None  # core.account.id — only resolved for jwt
    user_id: Optional[int] = None     # core.app_user.id — only resolved for jwt
    is_admin: bool = False

    @property
    def authenticated(self) -> bool:
        return self.method in ("jwt", "legacy")


def _bearer(request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


def resolve_principal(request) -> Optional[Principal]:
    """Return an authenticated Principal, or None if the request is unauthorized.
    Never raises — DB/verify failures fail CLOSED (None → caller returns 403)."""
    # 1) JWT path — only when explicitly enabled.
    if verifier.is_enabled():
        token = _bearer(request)
        if token and verifier.looks_like_jwt(token):
            claims = verifier.verify_jwt(token)
            if not claims:
                return None  # a JWT was presented but invalid — reject, don't downgrade
            try:
                return _principal_from_claims(claims)
            except Exception as e:
                log.warning("auth_v2: principal resolution failed: %s", e.__class__.__name__)
                return None

    # 2) Legacy shared-key path — unchanged from client_api._guard(). No DB.
    return _legacy_principal(request)


def _legacy_principal(request) -> Optional[Principal]:
    key = _client_api_key()
    if not key:
        return None
    supplied = request.headers.get("X-Client-Key") or ""
    bearer = _bearer(request)
    if bearer:
        supplied = bearer
    if not hmac.compare_digest(supplied.strip(), key):
        return None
    email = norm_email(request.args.get("email"))
    return Principal(email=email, method="legacy", is_admin=email in ADMIN_EMAILS)


def _principal_from_claims(claims) -> Optional[Principal]:
    uid = verifier.claim_uid(claims)
    email = verifier.claim_email(claims)
    email_verified = verifier.claim_email_verified(claims)
    prov = verifier.provider()
    if not uid:
        return None

    # Resolve (and, on first login, provision) the core identity. Capture
    # primitives inside the txn — ORM attributes expire after commit.
    with session_scope() as s:
        user = accounts.get_user_by_auth_provider_uid(s, prov, uid)
        if user is None:
            if not email:
                # No prior link and no email in the token → can't safely
                # link-or-create. (Fix: add `email` to the Clerk JWT template.)
                log.warning("auth_v2: token for unknown uid carries no email claim")
                return None
            user = ensure_user_for_claims(
                s, provider=prov, uid=uid, email=email, email_verified=email_verified,
            )
        else:
            accounts.touch_login(s, user.id)
        resolved_email = (user.email or email or "").strip().lower() or None
        account_id = user.account_id
        user_id = user.id

    return Principal(
        email=resolved_email,
        method="jwt",
        account_id=account_id,
        user_id=user_id,
        is_admin=(resolved_email in ADMIN_EMAILS) if resolved_email else False,
    )


def ensure_user_for_claims(session, *, provider, uid, email, email_verified=None):
    """First-login provisioning for a verified IdP token (also the Phase-1 signup
    landing). If a user already exists for this email (e.g. a wix-seeded account),
    LINK it to the new provider; otherwise create a fresh core.* identity via the
    existing consent/signup write-path (ensure_identity). Returns the AppUser."""
    existing = accounts.get_user_by_email(session, email)
    if existing is not None:
        return accounts.set_auth_provider(
            session, existing.id, auth_provider=provider, auth_provider_uid=uid,
            email_verified=email_verified,
        )
    # Brand-new signup → account + owner user + primary person (same path consent uses).
    acct, user, _person = accounts.ensure_identity(session, email=email)
    accounts.set_auth_provider(
        session, user.id, auth_provider=provider, auth_provider_uid=uid,
        email_verified=email_verified,
    )
    accounts.touch_login(session, user.id)
    return user
