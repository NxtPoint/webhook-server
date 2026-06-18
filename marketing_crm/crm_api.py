# marketing_crm/crm_api.py — the Cowork-facing CRM pull API.
#
# A clean, read-only, key-authenticated surface for the growth partner ("Cowork") to pull our
# customer 360 + product events and build Klaviyo segments/flows. We do the data; Cowork does the
# marketing automation. Klaviyo is the only destination today (HubSpot deferred).
#
# Design:
#   - Auth: a dedicated CRM_API_KEY (header X-CRM-Key or Authorization: Bearer). If the key is not
#     set, every endpoint returns 401 (the API is closed until a key is provisioned for Cowork).
#   - Read-only: pulls from the live SoR via the cockpit views (billing-backed) + core.* — never
#     writes. Aggregation already lives in SQL (rule #2); this is a thin paginated passthrough.
#   - Privacy boundary (hard, signed-off policy): OWNER/account-level traits only. vw_customer_list
#     is owner-scoped — NO child/minor PII, NO biometric/pose/video data ever crosses this surface.
#   - Consent-aware: every customer row carries marketing_opt_in; ?opted_in=true filters to the
#     compliant marketing audience. Cowork must only MESSAGE opted-in contacts (its Klaviyo flows
#     gate on the same flag) — transactional data is exposed for analysis regardless.
#
# Endpoints (crm_api_bp, prefix /api/crm):
#   GET /api/crm/health             — {ok, configured, klaviyo_configured} (no key needed)
#   GET /api/crm/customers          — paginated marketing profiles (filters: stage, plan, opted_in, since, until)
#   GET /api/crm/events             — paginated product-event stream (filters: event_type, email, since, until)
#   GET /api/crm/cohort             — emails (+ key traits) matching a segment, for Klaviyo list import

import os

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from db_init import engine

crm_api_bp = Blueprint("crm_api", __name__)
_P = "/api/crm"

_MAX_LIMIT = 1000
_DEF_LIMIT = 200


def _configured() -> bool:
    return bool((os.getenv("CRM_API_KEY") or "").strip())


def _auth_ok() -> bool:
    expected = (os.getenv("CRM_API_KEY") or "").strip()
    if not expected:
        return False  # closed until a key is provisioned
    supplied = (request.headers.get("X-CRM-Key") or "").strip()
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    return bool(supplied) and supplied == expected


def _deny():
    return jsonify({"ok": False, "error": "unauthorized"}), 401


def _page():
    """(limit, offset) clamped from query params."""
    try:
        limit = min(int(request.args.get("limit", _DEF_LIMIT)), _MAX_LIMIT)
    except (TypeError, ValueError):
        limit = _DEF_LIMIT
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return max(limit, 1), offset


def _rows(sql, params):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


# ── GET /health (no key — lets Cowork detect availability) ───────────────────
@crm_api_bp.route(f"{_P}/health", methods=["GET", "OPTIONS"])
def health():
    if request.method == "OPTIONS":
        return ("", 204)
    return jsonify({
        "ok": True,
        "service": "crm_api",
        "configured": _configured(),
        "klaviyo_configured": bool((os.getenv("KLAVIYO_API_KEY") or "").strip()),
    })


# ── GET /customers — marketing profiles (owner-level, consent-aware) ─────────
_CUSTOMERS_SQL = """
    SELECT cl.email, cl.display_name, cl.role, cl.stage, cl.plan_code, cl.plan_type,
           cl.mrr_cents, cl.matches_remaining, cl.matches_uploaded, cl.last_activity,
           cl.nps_latest, cl.created_at, cl.public_id,
           COALESCE(u.marketing_opt_in, false) AS marketing_opt_in,
           acq.source AS signup_source, acq.medium AS signup_medium, acq.campaign AS signup_campaign
    FROM core.vw_customer_list cl
    LEFT JOIN core.account a  ON lower(a.email) = lower(cl.email) AND a.deleted_at IS NULL
    LEFT JOIN core.app_user u ON u.account_id = a.id AND u.is_account_owner
    LEFT JOIN core.acquisition acq ON acq.user_id = u.id
    WHERE 1 = 1
      {filters}
    ORDER BY cl.created_at DESC NULLS LAST
    LIMIT :limit OFFSET :offset
"""


def _customer_filters():
    """Build the shared WHERE fragment + params from query args (used by /customers + /cohort)."""
    clauses, params = [], {}
    stage = (request.args.get("stage") or "").strip().lower()
    if stage:
        clauses.append("lower(cl.stage) = :stage")
        params["stage"] = stage
    plan = (request.args.get("plan") or "").strip()
    if plan:
        clauses.append("cl.plan_code = :plan")
        params["plan"] = plan
    role = (request.args.get("role") or "").strip().lower()
    if role:
        clauses.append("lower(cl.role) = :role")
        params["role"] = role
    if (request.args.get("opted_in") or "").strip().lower() in ("1", "true", "yes"):
        clauses.append("COALESCE(u.marketing_opt_in, false) = true")
    since = (request.args.get("since") or "").strip()
    if since:
        clauses.append("cl.created_at >= :since")
        params["since"] = since
    until = (request.args.get("until") or "").strip()
    if until:
        clauses.append("cl.created_at < :until")
        params["until"] = until
    return ((" AND " + " AND ".join(clauses)) if clauses else ""), params


@crm_api_bp.route(f"{_P}/customers", methods=["GET", "OPTIONS"])
def customers():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _auth_ok():
        return _deny()
    limit, offset = _page()
    filt, params = _customer_filters()
    params.update({"limit": limit, "offset": offset})
    rows = _rows(_CUSTOMERS_SQL.format(filters=filt), params)
    return jsonify({"ok": True, "count": len(rows), "limit": limit, "offset": offset,
                    "customers": rows})


# ── GET /events — product-event stream (for Klaviyo flow triggers / analysis) ─
@crm_api_bp.route(f"{_P}/events", methods=["GET", "OPTIONS"])
def events():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _auth_ok():
        return _deny()
    limit, offset = _page()
    clauses, params = [], {}
    et = (request.args.get("event_type") or "").strip()
    if et:
        clauses.append("ue.event_type = :et")
        params["et"] = et
    em = (request.args.get("email") or "").strip().lower()
    if em:
        clauses.append("(lower(a.email) = :em OR lower(ue.metadata->>'email_unmatched') = :em)")
        params["em"] = em
    since = (request.args.get("since") or "").strip()
    if since:
        clauses.append("ue.occurred_at >= :since")
        params["since"] = since
    until = (request.args.get("until") or "").strip()
    if until:
        clauses.append("ue.occurred_at < :until")
        params["until"] = until
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    params.update({"limit": limit, "offset": offset})
    rows = _rows(
        f"""
        SELECT COALESCE(a.email, ue.metadata->>'email_unmatched') AS email,
               ue.event_type, ue.occurred_at, ue.ref_type, ue.ref_id, ue.metadata
        FROM core.usage_event ue
        LEFT JOIN core.account a ON a.id = ue.account_id
        WHERE 1 = 1 {where}
        ORDER BY ue.occurred_at DESC
        LIMIT :limit OFFSET :offset
        """, params)
    return jsonify({"ok": True, "count": len(rows), "limit": limit, "offset": offset,
                    "events": rows})


# ── GET /cohort — emails matching a segment (Klaviyo list import) ────────────
@crm_api_bp.route(f"{_P}/cohort", methods=["GET", "OPTIONS"])
def cohort():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _auth_ok():
        return _deny()
    limit, offset = _page()
    filt, params = _customer_filters()
    if not filt:
        return jsonify({"ok": False, "error": "cohort requires at least one filter "
                        "(stage, plan, role, opted_in, since, until)"}), 400
    params.update({"limit": limit, "offset": offset})
    rows = _rows(
        f"""
        SELECT cl.email, cl.display_name, cl.stage, cl.plan_code, cl.mrr_cents,
               COALESCE(u.marketing_opt_in, false) AS marketing_opt_in
        FROM core.vw_customer_list cl
        LEFT JOIN core.account a  ON lower(a.email) = lower(cl.email) AND a.deleted_at IS NULL
        LEFT JOIN core.app_user u ON u.account_id = a.id AND u.is_account_owner
        WHERE 1 = 1 {filt}
        ORDER BY cl.created_at DESC NULLS LAST
        LIMIT :limit OFFSET :offset
        """, params)
    return jsonify({"ok": True, "count": len(rows), "limit": limit, "offset": offset,
                    "emails": [r["email"] for r in rows], "cohort": rows})


def register(app):
    """Register the Cowork CRM pull API. Always registered; every data endpoint is closed
    (401) until CRM_API_KEY is set, so this is safe to wire on boot."""
    app.register_blueprint(crm_api_bp)
    return True
