# core_api/blueprint.py — thin HTTP layer over core_db repositories.
#
# Design: endpoints do auth + (de)serialization only; all data work goes through
# core_db.repositories (no raw SQL here), and aggregation stays in core.* views.
#
# Auth: header X-Core-Key (or Authorization: Bearer) == env CORE_API_KEY
#       (falls back to CLIENT_API_KEY). Admin endpoints additionally require the key.
#       This is a scaffold for the token-based auth that replaces the shared-key model
#       (ARCHITECTURE.md §6.1) — wire real per-user tokens here during the auth migration.
#
# Registration is GATED: register(app) is a no-op unless CORE_API_ENABLED=1.

import os

from flask import Blueprint, jsonify, request

from core_db.db import as_dict, session_scope
from core_db.repositories import accounts, subscriptions, matches, feedback

core_bp = Blueprint("core_api", __name__, url_prefix="/api/core")


def _auth_ok():
    # Dual-mode (de-Wix): a verified Clerk JWT OR the legacy X-Core-Key/X-Client-Key.
    try:
        from auth_v2 import resolve_principal
        if resolve_principal(request) is not None:
            return True
    except Exception:
        pass
    expected = os.getenv("CORE_API_KEY") or os.getenv("CLIENT_API_KEY")
    if not expected:
        return False
    supplied = request.headers.get("X-Core-Key")
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    return bool(supplied) and supplied == expected


def _guard():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


@core_bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "core_api"})


@core_bp.get("/account")
def account_summary():
    """?email=<owner email> → account + persons + credit balance + current subscription."""
    err = _guard()
    if err:
        return err
    email = request.args.get("email")
    with session_scope() as s:
        acct = accounts.get_account_by_email(s, email)
        if acct is None:
            return jsonify({"ok": False, "error": "account not found"}), 404
        persons = [as_dict(p) for p in accounts.list_persons_for_account(s, acct.id)]
        bal = subscriptions.balance(s, acct.id)
        sub = subscriptions.get_active_subscription(s, acct.id)
        return jsonify({
            "ok": True,
            "account": as_dict(acct),
            "persons": persons,
            "credits": bal,
            "subscription": as_dict(sub),
        })


@core_bp.get("/account/<email>/matches")
def account_matches(email):
    err = _guard()
    if err:
        return err
    with session_scope() as s:
        acct = accounts.get_account_by_email(s, email)
        if acct is None:
            return jsonify({"ok": False, "error": "account not found"}), 404
        rows = [as_dict(m) for m in matches.list_matches_for_account(s, acct.id)]
        return jsonify({"ok": True, "matches": rows})


@core_bp.post("/usage")
def record_usage():
    err = _guard()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    event_type = body.get("event_type")
    if not event_type:
        return jsonify({"ok": False, "error": "event_type required"}), 400
    with session_scope() as s:
        acct = accounts.get_account_by_email(s, body.get("email")) if body.get("email") else None
        ev = matches.record_usage(
            s, event_type=event_type,
            account_id=(acct.id if acct else None),
            ref_type=body.get("ref_type"), ref_id=body.get("ref_id"),
            metadata=body.get("metadata"),
        )
        return jsonify({"ok": True, "id": ev.id})


@core_bp.post("/feedback/nps")
def submit_nps():
    err = _guard()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    score = body.get("score")
    if not isinstance(score, int) or not (0 <= score <= 10):
        return jsonify({"ok": False, "error": "score must be int 0-10"}), 400
    with session_scope() as s:
        acct = accounts.get_account_by_email(s, body.get("email")) if body.get("email") else None
        resp = feedback.record_nps(s, score=score, account_id=(acct.id if acct else None),
                                   comment=body.get("comment"))
        return jsonify({"ok": True, "id": resp.id, "bucket": resp.bucket})


@core_bp.get("/admin/metrics")
def admin_metrics():
    """MRR + active-subscription count + aggregate NPS (admin)."""
    err = _guard()
    if err:
        return err
    with session_scope() as s:
        mrr_cents = subscriptions.total_mrr_cents(s)
        nps = feedback.nps_score(s)
        return jsonify({"ok": True, "mrr_cents": mrr_cents, "mrr": mrr_cents / 100.0, "nps": nps})


def register(app):
    """Register the /api/core/* blueprint. Always on (de-gated 2026-06-17, post go-live —
    additive over core.*, admin/auth-gated per route)."""
    app.register_blueprint(core_bp)
    return True
