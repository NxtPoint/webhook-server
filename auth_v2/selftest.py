# auth_v2/selftest.py — offline verification of the Phase-0 auth machinery.
#
# No real Clerk tenant needed: we mint our own RS256 tokens against a throwaway
# keypair and feed the verifier a stub JWKS client, so jwt.decode() does REAL
# signature + issuer + expiry verification. Proves verify_jwt() + resolve_principal()
# without any network call.
#
# Run:
#   .venv/Scripts/python -m auth_v2.selftest          # crypto + legacy checks (NO DB)
#   .venv/Scripts/python -m auth_v2.selftest --db      # also the DB provision/link path
#
# CAUTION: --db writes to whatever DATABASE_URL points at (on the dev box that is
# the LIVE Render Postgres). It uses a @seed.ten-fifty5.test email and PURGES every
# row it creates in a finally block — but it is opt-in for exactly that reason.

import os
import sys
import time
import uuid

SEED_EMAIL = "authv2-selftest@seed.ten-fifty5.test"


# ---- token + key helpers -------------------------------------------------

def _mint_keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _make_token(priv, *, iss, sub, email=None, exp_delta=300, aud=None, kid="test-kid"):
    import jwt
    now = int(time.time())
    claims = {"iss": iss, "sub": sub, "iat": now, "exp": now + exp_delta}
    if email is not None:
        claims["email"] = email
    if aud is not None:
        claims["aud"] = aud
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": kid})


class _StubJWKS:
    """Stands in for PyJWKClient — returns our public key for any token."""
    def __init__(self, pub):
        self._pub = pub

    def get_signing_key_from_jwt(self, token):
        class _K:
            key = self._pub
        return _K()


class _FakeRequest:
    def __init__(self, headers=None, args=None):
        self.headers = _CI(headers or {})
        self.args = dict(args or {})


class _CI(dict):
    """Minimal case-insensitive header map supporting .get(key, default)."""
    def __init__(self, d):
        super().__init__({k.lower(): v for k, v in d.items()})

    def get(self, k, default=None):
        return super().get(k.lower(), default)


# ---- assertions ----------------------------------------------------------

_passed = 0
_failed = 0


def _check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


def run_crypto_and_legacy():
    from auth_v2 import verifier, principal

    iss = "https://selftest.clerk.example"
    priv, pub = _mint_keypair()

    # point the verifier at a stub JWKS + configured issuer; enable auth_v2
    os.environ["AUTH_V2_ENABLED"] = "1"
    os.environ["AUTH_PROVIDER"] = "clerk"
    os.environ["AUTH_ISSUER"] = iss
    os.environ["AUTH_JWKS_URL"] = "https://selftest/.well-known/jwks.json"  # not fetched (stubbed)
    os.environ.pop("AUTH_AUDIENCE", None)
    os.environ.pop("AUTH_ISSUERS", None)
    os.environ.pop("AUTH_JWKS_URLS", None)
    verifier._client_for = lambda *_a, **_k: _StubJWKS(pub)

    print("verifier:")
    _check("structural: looks_like_jwt rejects shared key",
           not verifier.looks_like_jwt("plain-shared-key"))
    good = _make_token(priv, iss=iss, sub="user_abc", email="a@b.com")
    _check("structural: looks_like_jwt accepts a JWT", verifier.looks_like_jwt(good))
    _check("valid token verifies + carries claims",
           (verifier.verify_jwt(good) or {}).get("sub") == "user_abc")
    _check("email claim extracted", verifier.claim_email(verifier.verify_jwt(good)) == "a@b.com")

    bad_iss = _make_token(priv, iss="https://evil", sub="user_abc", email="a@b.com")
    _check("wrong issuer rejected", verifier.verify_jwt(bad_iss) is None)

    expired = _make_token(priv, iss=iss, sub="user_abc", email="a@b.com", exp_delta=-120)
    _check("expired token rejected (beyond leeway)", verifier.verify_jwt(expired) is None)

    other_priv, _ = _mint_keypair()
    forged = _make_token(other_priv, iss=iss, sub="user_abc", email="a@b.com")
    _check("bad signature rejected", verifier.verify_jwt(forged) is None)

    # ---- federation (multi-issuer allowlist) — accept a partner IdP's tokens too ----
    iss2 = "https://clerk.partner.example"
    os.environ["AUTH_ISSUERS"] = iss + "," + iss2
    os.environ["AUTH_JWKS_URLS"] = ("https://selftest/.well-known/jwks.json,"
                                    "https://partner/.well-known/jwks.json")
    tok1 = _make_token(priv, iss=iss, sub="user_own", email="a@b.com")
    tok2 = _make_token(priv, iss=iss2, sub="user_partner", email="c@d.com")
    _check("federation: own issuer still verifies",
           (verifier.verify_jwt(tok1) or {}).get("sub") == "user_own")
    _check("federation: partner issuer verifies",
           (verifier.verify_jwt(tok2) or {}).get("sub") == "user_partner")
    stranger = _make_token(priv, iss="https://clerk.stranger.example", sub="x", email="e@f.com")
    _check("federation: issuer outside allowlist rejected", verifier.verify_jwt(stranger) is None)
    os.environ.pop("AUTH_ISSUERS", None)      # restore single-issuer for the remaining checks
    os.environ.pop("AUTH_JWKS_URLS", None)

    # disabled → inert
    os.environ["AUTH_V2_ENABLED"] = "0"
    _check("AUTH_V2_ENABLED=0 -> verify_jwt is a no-op", verifier.verify_jwt(good) is None)
    os.environ["AUTH_V2_ENABLED"] = "1"

    print("legacy path:")
    os.environ["CLIENT_API_KEY"] = "legacy-test-key-123"
    req = _FakeRequest(headers={"X-Client-Key": "legacy-test-key-123"},
                       args={"email": "Legacy@User.com"})
    p = principal.resolve_principal(req)
    _check("legacy shared key resolves", p is not None and p.method == "legacy")
    _check("legacy email normalised", p is not None and p.email == "legacy@user.com")
    _check("legacy admin flag works",
           principal.resolve_principal(
               _FakeRequest(headers={"X-Client-Key": "legacy-test-key-123"},
                            args={"email": "info@ten-fifty5.com"})).is_admin)
    bad = principal.resolve_principal(
        _FakeRequest(headers={"X-Client-Key": "wrong"}, args={"email": "x@y.com"}))
    _check("wrong shared key rejected", bad is None)

    # a presented-but-invalid JWT must be REJECTED, never downgraded to legacy
    rej = principal.resolve_principal(
        _FakeRequest(headers={"Authorization": f"Bearer {bad_iss}",
                              "X-Client-Key": "legacy-test-key-123"}))
    _check("invalid JWT is rejected (not downgraded to shared key)", rej is None)

    return priv, pub, iss


def run_db(priv, pub, iss):
    """Exercise the provision + link path, then purge. Opt-in (--db)."""
    from auth_v2 import verifier, principal
    from core_db.db import session_scope
    from core_db.repositories import accounts

    verifier._get_jwks_client = lambda: _StubJWKS(pub)
    os.environ["AUTH_V2_ENABLED"] = "1"
    os.environ["AUTH_ISSUER"] = iss

    sub = f"user_selftest_{uuid.uuid4().hex[:12]}"
    print("db provision/link path:")
    try:
        # first login for a brand-new email → provisions account+user+person
        tok = _make_token(priv, iss=iss, sub=sub, email=SEED_EMAIL)
        p1 = principal.resolve_principal(_FakeRequest(headers={"Authorization": f"Bearer {tok}"}))
        _check("new uid provisions core identity (jwt)", p1 is not None and p1.method == "jwt")
        _check("account_id derived server-side", p1 is not None and p1.account_id is not None)
        _check("email derived from token", p1 is not None and p1.email == SEED_EMAIL)

        # second login same uid → resolves the same user (no duplicate)
        p2 = principal.resolve_principal(_FakeRequest(headers={"Authorization": f"Bearer {tok}"}))
        _check("repeat login resolves same user", p2 is not None and p2.user_id == p1.user_id)

        # link path: a token with a NEW uid but the SAME email links the existing user
        sub2 = f"user_relink_{uuid.uuid4().hex[:12]}"
        tok2 = _make_token(priv, iss=iss, sub=sub2, email=SEED_EMAIL)
        p3 = principal.resolve_principal(_FakeRequest(headers={"Authorization": f"Bearer {tok2}"}))
        _check("same email + new uid links existing user (no dup)",
               p3 is not None and p3.user_id == p1.user_id)
    finally:
        _purge_seed()


def _purge_seed():
    """Delete every row the --db test created. Account cascade removes the owner
    user + primary person (FK ondelete=CASCADE in the model)."""
    from sqlalchemy import text
    from core_db.db import session_scope
    try:
        with session_scope() as s:
            n = s.execute(
                text("DELETE FROM core.account WHERE lower(email) = :e"),
                {"e": SEED_EMAIL},
            ).rowcount
        print(f"  purge: removed {n} seed account row(s) ({SEED_EMAIL})")
    except Exception as e:
        print(f"  purge WARNING: {e!r} — verify no {SEED_EMAIL} rows remain in core.account")


def main():
    do_db = "--db" in sys.argv
    priv, pub, iss = run_crypto_and_legacy()
    if do_db:
        run_db(priv, pub, iss)
    else:
        print("db: skipped (pass --db to exercise the provision/link path against DATABASE_URL)")

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
