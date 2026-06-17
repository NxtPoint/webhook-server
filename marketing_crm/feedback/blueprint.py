# marketing_crm/feedback/blueprint.py — customer-facing feedback + NPS endpoints.
#
# Auth: X-Client-Key == CLIENT_API_KEY (same as the rest of /api/client/*) + an email identifying
# the account. Routes under /api/client/feedback/* so they inherit the existing CORS allowlist.
# Writes to core.* via repositories; emits tracking events. DARK unless FEEDBACK_ENABLED=1.

import os

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from core_db.db import session_scope, norm_email
from core_db.repositories import accounts, feedback as fb

feedback_bp = Blueprint("mc_feedback", __name__)
_P = "/api/client/feedback"

NPS_TRIGGER_N = int(os.getenv("NPS_TRIGGER_N", "3"))        # show after this many reports viewed
NPS_COOLDOWN_DAYS = int(os.getenv("NPS_COOLDOWN_DAYS", "90"))


def _key_ok():
    expected = os.getenv("CLIENT_API_KEY") or os.getenv("CORE_API_KEY")
    if not expected:
        return False
    supplied = request.headers.get("X-Client-Key")
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    return bool(supplied) and supplied == expected


def _email():
    body = request.get_json(silent=True) or {}
    return norm_email(body.get("email") or request.args.get("email"))


def _resolve_account_id(session, email):
    if not email:
        return None, None
    acct = accounts.get_account_by_email(session, email)
    user = accounts.get_user_by_email(session, email)
    return (acct.id if acct else None), (user.id if user else None)


@feedback_bp.route(f"{_P}/nps-eligibility", methods=["GET", "OPTIONS"])
def nps_eligibility():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    email = _email()
    out = {"ok": True, "show": False, "reports_viewed": 0, "threshold": NPS_TRIGGER_N}
    if not email:
        return jsonify(out)
    try:
        with session_scope() as s:
            acct_id, _ = _resolve_account_id(s, email)
            if acct_id is None:
                return jsonify(out)
            reports = s.execute(text(
                "SELECT count(*) FROM core.usage_event WHERE account_id=:a "
                "AND event_type IN ('report_view','report_viewed','dashboard_view')"
            ), {"a": acct_id}).scalar() or 0
            recent_nps = s.execute(text(
                "SELECT count(*) FROM core.nps_response WHERE account_id=:a "
                "AND submitted_at > now() - (:d || ' days')::interval"
            ), {"a": acct_id, "d": NPS_COOLDOWN_DAYS}).scalar() or 0
            out["reports_viewed"] = int(reports)
            out["show"] = bool(reports >= NPS_TRIGGER_N and recent_nps == 0)
    except Exception:
        # never block the UI on a feedback error
        pass
    return jsonify(out)


@feedback_bp.route(f"{_P}/nps", methods=["POST", "OPTIONS"])
def submit_nps():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    score = body.get("score")
    if not isinstance(score, int) or not (0 <= score <= 10):
        return jsonify({"ok": False, "error": "score must be int 0-10"}), 400
    email = _email()
    with session_scope() as s:
        acct_id, user_id = _resolve_account_id(s, email)
        resp = fb.record_nps(s, score=score, account_id=acct_id, user_id=user_id,
                             comment=(body.get("comment") or None))
        bucket = resp.bucket
    _track("nps_submitted", email, {"score": score, "bucket": bucket})
    return jsonify({"ok": True, "bucket": bucket})


@feedback_bp.route(f"{_P}/widget", methods=["POST", "OPTIONS"])
def submit_widget():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400
    email = _email()
    responses = {"sentiment": body.get("sentiment"), "area": body.get("area"),
                 "message": message, "page": body.get("page")}
    with session_scope() as s:
        acct_id, user_id = _resolve_account_id(s, email)
        fb.record_survey(s, survey_key="in_app_feedback", responses=responses,
                         account_id=acct_id, user_id=user_id)
    _track("feedback_submitted", email, {"sentiment": body.get("sentiment"), "area": body.get("area")})
    return jsonify({"ok": True})


@feedback_bp.route(f"{_P}/cancellation", methods=["POST", "OPTIONS"])
def submit_cancellation():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()
    if not reason:
        return jsonify({"ok": False, "error": "reason required"}), 400
    email = _email()
    responses = {"reason": reason, "comment": (body.get("comment") or None)}
    with session_scope() as s:
        acct_id, user_id = _resolve_account_id(s, email)
        fb.record_survey(s, survey_key="cancellation", responses=responses,
                         account_id=acct_id, user_id=user_id)
    _track("cancellation_reason_submitted", email, {"reason": reason})
    return jsonify({"ok": True})


def _track(event, email, props):
    try:
        from marketing_crm.tracking import track
        track(event, email=email, properties=props)
    except Exception:
        pass


def register(app):
    """Register feedback/NPS endpoints. Always on (de-gated 2026-06-17, post go-live)."""
    app.register_blueprint(feedback_bp)
    return True
