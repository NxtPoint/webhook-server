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
    else:
        # default view hides terminated accounts; pass ?stage=terminated to see them
        where.append("stage <> 'terminated'")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = _rows(
        f"SELECT * FROM core.vw_customer_list{clause} ORDER BY last_activity DESC NULLS LAST LIMIT 500",
        params,
    )
    return jsonify({"ok": True, "customers": rows, "count": len(rows)})


@cockpit_bp.route(f"{_PREFIX}/customer", methods=["GET", "OPTIONS"])
def customer_360():
    """Full Customer-360 drill-down for one account, keyed by ?email=. Summary scalars come from
    core.vw_customer_360; the lists are bounded sub-selects against the live SoR. Robust to sparse
    core.* rows (returns [])."""
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    summary = _one("SELECT * FROM core.vw_customer_360 WHERE lower(email) = :e", {"e": email})
    if not summary:
        return jsonify({"ok": False, "error": "customer not found"}), 404
    aid = summary.get("account_id")

    payments = _rows(
        "SELECT id, kind, amount_cents, currency, status, occurred_at, plan_code, "
        "provider, provider_payment_id "
        "FROM billing.payment WHERE account_id = :a ORDER BY occurred_at DESC NULLS LAST LIMIT 50",
        {"a": aid})

    transaction_log = _rows(
        "SELECT te.task_id, te.step, te.status, te.detail, te.error, te.created_at, sc.sport_type "
        "FROM bronze.task_event te "
        "JOIN bronze.submission_context sc ON sc.task_id = te.task_id "
        "WHERE lower(sc.email) = :e ORDER BY te.created_at DESC LIMIT 100",
        {"e": email})

    subscription_events = _rows(
        "SELECT event_id, event_type, payload, created_at "
        "FROM billing.subscription_event_log WHERE account_id = :a "
        "ORDER BY created_at DESC LIMIT 50",
        {"a": aid})

    support_chat = _rows(
        "SELECT question, answer, confidence, needs_human, escalated_at, feedback, created_at "
        "FROM support_bot.conversations WHERE lower(email) = :e "
        "ORDER BY created_at DESC LIMIT 50",
        {"e": email})

    coach_chat = _rows(
        "SELECT task_id, prompt_key, question, response, created_at "
        "FROM tennis_coach.conversations WHERE lower(email) = :e "
        "ORDER BY created_at DESC LIMIT 50",
        {"e": email})

    feedback_rows = []
    if aid is not None:
        feedback_rows = _rows(
            "SELECT 'nps' AS kind, score::text AS detail, bucket, comment, submitted_at "
            "FROM core.nps_response WHERE account_id = :a "
            "UNION ALL "
            "SELECT 'survey' AS kind, survey_key AS detail, NULL AS bucket, "
            "responses::text AS comment, submitted_at "
            "FROM core.survey_response WHERE account_id = :a "
            "ORDER BY submitted_at DESC LIMIT 50",
            {"a": aid})

    consent_rows = _rows(
        "SELECT cn.consent_type, cn.status, cn.policy_version, cn.granted_at, cn.withdrawn_at, "
        "pe.full_name AS subject "
        "FROM core.account ca "
        "JOIN core.person pe ON pe.account_id = ca.id "
        "JOIN core.consent cn ON cn.subject_person_id = pe.id "
        "WHERE lower(ca.email) = :e "
        "ORDER BY cn.granted_at DESC NULLS LAST, cn.created_at DESC LIMIT 50",
        {"e": email})

    return jsonify({
        "ok": True,
        "summary": summary,
        "payments": payments,
        "transaction_log": transaction_log,
        "subscription_events": subscription_events,
        "support_chat": support_chat,
        "coach_chat": coach_chat,
        "feedback": feedback_rows,
        "consent": consent_rows,
    })


# ── Admin maintenance actions (write) — all admin-gated, all via the SAFE billing paths ──
# No plan edits, no row deletes, no manual balance edits. Refunds are NOT here — do those in
# the PayPal dashboard (the webhook records them). See docs/business/billing-implementation.md.

def _acct_by_email(email):
    return _one("SELECT id, email, active, comp FROM billing.account WHERE lower(email) = :e",
                {"e": (email or "").strip().lower()})


def _cancel_paypal_sub(account_id):
    """Best-effort cancel the account's ACTIVE PayPal subscription at PayPal. The webhook then
    reconciles subscription_state to CANCELLED. Returns a short status string."""
    sub = _one(
        "SELECT provider_subscription_id AS sid FROM billing.subscription_state "
        "WHERE account_id = :a AND billing_provider = 'paypal' "
        "AND provider_subscription_id IS NOT NULL AND status = 'ACTIVE' "
        "ORDER BY updated_at DESC NULLS LAST LIMIT 1", {"a": account_id})
    sid = (sub or {}).get("sid")
    if not sid:
        return "no_active_paypal_sub"
    try:
        from paypal_billing import client
        client.cancel_subscription(sid, reason="Admin cancellation")
        return "cancel_requested"
    except Exception:
        return "paypal_cancel_failed"


def _admin_action(fn):
    """Shared boilerplate for the POST admin actions: OPTIONS, admin gate, JSON body, email→account."""
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    acct = _acct_by_email(body.get("email"))
    if not acct:
        return jsonify({"ok": False, "error": "customer not found"}), 404
    return fn(body, acct)


@cockpit_bp.route(f"{_PREFIX}/customer/add-credits", methods=["POST", "OPTIONS"])
def customer_add_credits():
    def _do(body, acct):
        try:
            matches = int(body.get("matches") or 0)
            techniques = int(body.get("techniques") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "matches/techniques must be integers"}), 400
        if matches <= 0 and techniques <= 0:
            return jsonify({"ok": False, "error": "grant at least 1 match or technique credit"}), 400
        import uuid
        ref = "admin:" + uuid.uuid4().hex  # unique per request → distinct grant, retry-idempotent
        from billing_service import grant_entitlement
        gid = grant_entitlement(
            account_id=acct["id"], source="manual_adjustment", plan_code="service_credit",
            matches_granted=max(matches, 0), techniques_granted=max(techniques, 0),
            external_wix_id=ref, valid_to=None,  # life-long (never expires), like PAYG
        )
        return jsonify({"ok": True, "grant_id": gid, "matches": matches, "techniques": techniques})
    return _admin_action(_do)


@cockpit_bp.route(f"{_PREFIX}/customer/set-comp", methods=["POST", "OPTIONS"])
def customer_set_comp():
    def _do(body, acct):
        comp = bool(body.get("comp"))
        from billing_service import set_account_comp
        set_account_comp(account_id=acct["id"], comp=comp)
        return jsonify({"ok": True, "comp": comp})
    return _admin_action(_do)


@cockpit_bp.route(f"{_PREFIX}/customer/terminate", methods=["POST", "OPTIONS"])
def customer_terminate():
    def _do(body, acct):
        from billing_service import set_account_active
        set_account_active(account_id=acct["id"], active=False)
        paypal = _cancel_paypal_sub(acct["id"])  # stop further billing
        return jsonify({"ok": True, "terminated": True, "paypal": paypal})
    return _admin_action(_do)


@cockpit_bp.route(f"{_PREFIX}/customer/reactivate", methods=["POST", "OPTIONS"])
def customer_reactivate():
    def _do(body, acct):
        from billing_service import set_account_active
        set_account_active(account_id=acct["id"], active=True)
        # NB: does not re-subscribe at PayPal — the customer must re-subscribe for paid access.
        return jsonify({"ok": True, "active": True})
    return _admin_action(_do)


@cockpit_bp.route(f"{_PREFIX}/customer/cancel-subscription", methods=["POST", "OPTIONS"])
def customer_cancel_subscription():
    def _do(body, acct):
        paypal = _cancel_paypal_sub(acct["id"])  # account stays active; credits already granted stay
        return jsonify({"ok": True, "paypal": paypal})
    return _admin_action(_do)


@cockpit_bp.route(f"{_PREFIX}/customer/update-profile", methods=["POST", "OPTIONS"])
def customer_update_profile():
    def _do(body, acct):
        from billing_service import update_primary_member_profile
        result = update_primary_member_profile(account_id=acct["id"], fields=body.get("fields") or {})
        return jsonify({"ok": True, **result})
    return _admin_action(_do)


# ── DSAR / erasure (GDPR data-subject requests) — admin-gated ────────────────
@cockpit_bp.route(f"{_PREFIX}/dsar", methods=["GET", "OPTIONS"])
def dsar_list():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    rows = _rows(
        "SELECT d.id, d.request_type, d.status, d.requested_at, d.completed_at, d.notes, "
        "       pe.full_name AS subject, ca.email AS subject_email "
        "FROM core.data_subject_request d "
        "LEFT JOIN core.person pe ON pe.id = d.subject_person_id "
        "LEFT JOIN core.account ca ON ca.id = pe.account_id "
        "ORDER BY (d.status = 'received') DESC, d.requested_at DESC LIMIT 200")
    return jsonify({"ok": True, "requests": rows})


@cockpit_bp.route(f"{_PREFIX}/dsar/<int:dsar_id>/action", methods=["POST", "OPTIONS"])
def dsar_action(dsar_id):
    """Action a DSAR. body {action}: in_progress|completed|rejected, OR erase (runs the GDPR erasure
    for the subject's account — defaults to dry_run:true; pass dry_run:false to execute)."""
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip().lower()
    d = _one(
        "SELECT d.id, d.request_type, ca.email AS subject_email "
        "FROM core.data_subject_request d "
        "LEFT JOIN core.person pe ON pe.id = d.subject_person_id "
        "LEFT JOIN core.account ca ON ca.id = pe.account_id WHERE d.id = :id", {"id": dsar_id})
    if not d:
        return jsonify({"ok": False, "error": "DSAR not found"}), 404

    if action == "erase":
        email = (d.get("subject_email") or "").strip().lower()
        acct = _one("SELECT id FROM billing.account WHERE lower(email) = lower(:e)", {"e": email}) if email else None
        if not acct:
            return jsonify({"ok": False, "error": "no billing account for subject email"}), 404
        dry = bool(body.get("dry_run", True))  # SAFE DEFAULT — erase is irreversible
        from cleanup.retention_sweep import erase_account
        result = erase_account(acct["id"], dry_run=dry)
        if not dry and result.get("ok"):
            with get_engine().begin() as conn:
                conn.execute(text("UPDATE core.data_subject_request "
                                  "SET status = 'completed', completed_at = now() WHERE id = :id"),
                             {"id": dsar_id})
        return jsonify({"ok": True, "action": "erase", "erase": result})

    if action not in ("in_progress", "completed", "rejected"):
        return jsonify({"ok": False, "error": "unknown action"}), 400
    with get_engine().begin() as conn:
        conn.execute(text(
            "UPDATE core.data_subject_request SET status = :s, "
            "completed_at = CASE WHEN :s = 'completed' THEN now() ELSE completed_at END "
            "WHERE id = :id"), {"s": action, "id": dsar_id})
    return jsonify({"ok": True, "status": action})


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


@cockpit_bp.route(f"{_PREFIX}/performance", methods=["GET", "OPTIONS"])
def performance():
    """Business-performance time-series for the cockpit charts. All series come from the
    Phase-2 rollup views over the live SoR (each reconciles to raw counts). Daily series are
    bounded to the last 90 days, monthly to 24 months."""
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({
        "ok": True,
        "support_health": _one("SELECT * FROM core.vw_support_health"),
        "dau":              _rows("SELECT * FROM core.vw_dau WHERE day >= current_date - 90 ORDER BY day"),
        "mau":              _rows("SELECT * FROM core.vw_mau WHERE month >= date_trunc('month', current_date) - interval '24 months' ORDER BY month"),
        "usage_daily":      _rows("SELECT * FROM core.vw_usage_daily WHERE day >= current_date - 90 ORDER BY day"),
        "new_accounts":     _rows("SELECT * FROM core.vw_new_accounts_monthly WHERE month >= date_trunc('month', current_date) - interval '24 months' ORDER BY month"),
        "revenue_monthly":  _rows("SELECT * FROM core.vw_revenue_monthly WHERE month >= date_trunc('month', current_date) - interval '24 months' ORDER BY month"),
        "churn_monthly":    _rows("SELECT * FROM core.vw_churn_monthly WHERE month >= date_trunc('month', current_date) - interval '24 months' ORDER BY month"),
        "processing_daily": _rows("SELECT * FROM core.vw_processing_daily WHERE day >= current_date - 90 ORDER BY day"),
        "support_daily":    _rows("SELECT * FROM core.vw_support_daily WHERE day >= current_date - 90 ORDER BY day"),
        "coach_daily":      _rows("SELECT * FROM core.vw_coach_daily WHERE day >= current_date - 90 ORDER BY day"),
        "visitors_daily":   _rows("SELECT * FROM core.vw_visitors_daily WHERE day >= current_date - 90 ORDER BY day"),
    })


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


@cockpit_bp.route(f"{_PREFIX}/web-traffic", methods=["GET", "OPTIONS"])
def web_traffic():
    """Website-traffic overview (visitors, unique vs returning, devices, countries, top pages,
    sources, time-on-site) from the cookieless page-view beacon. Aggregation lives in the shared,
    replicable analytics.traffic module (same engine as the nextpoint repo)."""
    if request.method == "OPTIONS":
        return ("", 204)
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        days = int(request.args.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    try:
        from analytics import overview
        with get_engine().connect() as c:
            data = overview(c, days=days)
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def register(app):
    """Register the cockpit blueprint. Always on (de-gated 2026-06-17, post go-live —
    every route is admin-gated via _admin_ok)."""
    app.register_blueprint(cockpit_bp)
    return True
