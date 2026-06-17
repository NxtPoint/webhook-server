# marketing_crm/backoffice/blueprint.py — cockpit HTTP surface (admin-only, read-only).
#
# Routes sit under /api/client/backoffice/cockpit/* so they inherit the existing /api/client/*
# CORS allowlist and the frontend's auth pattern (X-Client-Key header + ?email=). Auth reuses
# CLIENT_API_KEY + ADMIN_EMAILS (single source — imported from client_api). Endpoints are thin
# passthroughs over core.* views (aggregation stays in SQL, rule #2).
#
# DARK by default: register(app) is a no-op unless COCKPIT_ENABLED=1.

import os

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from core_db.db import get_engine

cockpit_bp = Blueprint("cockpit", __name__)

_PREFIX = "/api/client/backoffice/cockpit"


def _admin_ok():
    # Dual-mode (de-Wix): a verified Clerk JWT (admin derived server-side) OR the
    # legacy shared key + ?email. resolve_principal handles BOTH and exposes is_admin.
    try:
        from auth_v2 import resolve_principal
        p = resolve_principal(request)
        if p is not None:
            return bool(getattr(p, "is_admin", False))
    except Exception:
        pass
    # Fallback only if auth_v2 is unavailable: original shared-key + ?email check.
    expected = os.getenv("CLIENT_API_KEY") or os.getenv("CORE_API_KEY")
    if not expected:
        return False
    supplied = request.headers.get("X-Client-Key")
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied or supplied != expected:
        return False
    email = (request.args.get("email") or request.headers.get("X-User-Email") or "").strip().lower()
    try:
        from client_api import ADMIN_EMAILS  # single source of truth for the admin list
        admins = {e.lower() for e in ADMIN_EMAILS}
    except Exception:
        admins = {"info@ten-fifty5.com", "tomo.stojakovic@gmail.com"}
    return email in admins


def _rows(sql, params=None):
    with get_engine().connect() as c:
        return [dict(r) for r in c.execute(text(sql), params or {}).mappings()]


def _one(sql, params=None):
    rows = _rows(sql, params)
    return rows[0] if rows else {}


@cockpit_bp.route(f"{_PREFIX}/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "cockpit"})


@cockpit_bp.route(f"{_PREFIX}/business-health", methods=["GET", "OPTIONS"])
def business_health():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({
        "ok": True,
        "health": _one("SELECT * FROM core.vw_business_health"),
        "by_plan": _rows("SELECT * FROM core.vw_subs_by_plan"),
    })


@cockpit_bp.route(f"{_PREFIX}/customers", methods=["GET", "OPTIONS"])
def customers():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    search = (request.args.get("search") or "").strip().lower()
    stage = (request.args.get("stage") or "").strip().lower()
    where, params = [], {}
    if search:
        where.append("(lower(email) LIKE :q OR lower(COALESCE(display_name,'')) LIKE :q)")
        params["q"] = f"%{search}%"
    if stage:
        where.append("stage = :stage")
        params["stage"] = stage
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = _rows(
        f"SELECT * FROM core.vw_customer_list{clause} ORDER BY last_activity DESC NULLS LAST LIMIT 500",
        params,
    )
    return jsonify({"ok": True, "customers": rows, "count": len(rows)})


@cockpit_bp.route(f"{_PREFIX}/at-risk", methods=["GET", "OPTIONS"])
def at_risk():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    rows = _rows("SELECT * FROM core.vw_at_risk ORDER BY category, metric DESC")
    grouped = {"trial_no_upload": [], "inactive_subscriber": [], "coach_linkable": []}
    for r in rows:
        grouped.setdefault(r["category"], []).append(r)
    return jsonify({"ok": True, "groups": grouped,
                    "counts": {k: len(v) for k, v in grouped.items()}})


@cockpit_bp.route(f"{_PREFIX}/processing-ops", methods=["GET", "OPTIONS"])
def processing_ops():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    status = (request.args.get("status") or "").strip().lower()
    params = {}
    clause = ""
    if status:
        clause = " WHERE derived_status = :s"
        params["s"] = status
    rows = _rows(
        f"SELECT * FROM core.vw_processing_ops{clause} "
        f"ORDER BY COALESCE(ingest_finished_at, ingest_started_at) DESC NULLS LAST LIMIT 300",
        params,
    )
    summary = _rows("SELECT derived_status, count(*) AS n FROM core.vw_processing_ops GROUP BY derived_status")
    return jsonify({"ok": True, "matches": rows, "count": len(rows),
                    "summary": {r["derived_status"]: r["n"] for r in summary}})


@cockpit_bp.route(f"{_PREFIX}/feedback", methods=["GET", "OPTIONS"])
def feedback():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    summary = _one("SELECT * FROM core.vw_nps_summary")
    monthly = _rows("SELECT to_char(month,'YYYY-MM') AS month, responses, nps FROM core.vw_nps_monthly LIMIT 12")
    verbatims = _rows(
        "SELECT score, bucket, comment, submitted_at FROM core.nps_response "
        "WHERE comment IS NOT NULL AND comment <> '' ORDER BY submitted_at DESC LIMIT 25")
    recent_feedback = _rows(
        "SELECT survey_key, responses, submitted_at FROM core.survey_response "
        "ORDER BY submitted_at DESC LIMIT 25")
    return jsonify({"ok": True, "summary": summary, "monthly": monthly,
                    "verbatims": verbatims, "recent_feedback": recent_feedback})


@cockpit_bp.route(f"{_PREFIX}/sync-crm", methods=["POST", "OPTIONS"])
def sync_crm():
    """Trigger a full DB→HubSpot/Klaviyo profile sync (admin; for a nightly cron or manual run).
    No-op unless CRM_SYNC_ENABLED=1 + provider keys set."""
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        from marketing_crm.crm_sync import enabled, sync_all
        if not enabled():
            return jsonify({"ok": True, "synced": 0, "note": "CRM_SYNC_ENABLED is off"})
        n = sync_all()
        return jsonify({"ok": True, "synced": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def register(app):
    """Register the cockpit blueprint. Always on (de-gated 2026-06-17, post go-live —
    every route is admin-gated via _admin_ok)."""
    app.register_blueprint(cockpit_bp)
    return True
