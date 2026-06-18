# marketing_crm/tracking/beacon.py — public page-view beacon (navigation analytics).
#
# POST /api/track/page records a page_view into core.usage_event (account resolved by email when the
# page is authed; anonymous otherwise) + Amplitude. Designed for navigator.sendBeacon: body is parsed
# from the raw request (text/plain), so there is NO CORS preflight — works from the public marketing
# pages and the member SPAs alike. Never blocks; gated by TRACKING_ENABLED (returns tracked:false off).
#
# page_view is intentionally NOT forwarded to Klaviyo (would be noisy/expensive) — DB + Amplitude only.

import json
import logging
import os
import threading

from flask import Blueprint, jsonify, request

log = logging.getLogger("marketing_crm.tracking.beacon")
page_bp = Blueprint("mc_page_beacon", __name__)


@page_bp.route("/api/track/page", methods=["POST", "OPTIONS"])
def page():
    if request.method == "OPTIONS":
        return ("", 204)
    # Always on (de-gated 2026-06-17). Page-views land in core.usage_event; Amplitude
    # forwarding self-gates on AMPLITUDE_API_KEY.
    try:
        body = json.loads(request.get_data() or b"{}")
    except Exception:
        body = request.get_json(silent=True) or {}
    path = (body.get("path") or "")[:300]
    if not path:
        return jsonify({"ok": False, "error": "path required"}), 400
    email = (body.get("email") or "").strip().lower() or None
    referrer = (body.get("referrer") or "")[:300]
    # First-party anonymous visitor id (client-generated, persisted in localStorage) — lets us
    # count UNIQUE VISITORS for logged-out marketing traffic (account_id is NULL there). UTM
    # params (client-parsed from the URL) power acquisition-source analytics.
    anon_id = (str(body.get("anon_id") or "")[:64]) or None
    utm = body.get("utm") if isinstance(body.get("utm"), dict) else {}
    props = body.get("props") if isinstance(body.get("props"), dict) else {}
    try:
        threading.Thread(target=_record, args=(path, email, referrer, props, anon_id, utm),
                         daemon=True).start()
    except Exception:
        log.exception("page beacon: thread spawn failed")
    return jsonify({"ok": True})


def _record(path, email, referrer, props, anon_id=None, utm=None):
    try:
        from core_db.db import session_scope
        from core_db.repositories import accounts, matches
        with session_scope() as s:
            account_id = None
            if email:
                a = accounts.get_account_by_email(s, email)
                if a:
                    account_id = a.id
            meta = {"path": path, "referrer": referrer}
            if anon_id:
                meta["anon_id"] = anon_id
            for k in ("source", "medium", "campaign", "term", "content"):
                v = (utm or {}).get(k)
                if v:
                    meta["utm_" + k] = str(v)[:120]
            for k in list(props)[:10]:
                meta[str(k)[:40]] = str(props[k])[:200]
            if account_id is None and email:
                meta["email_unmatched"] = email
            matches.record_usage(s, event_type="page_view", account_id=account_id,
                                 ref_type="page", ref_id=path, metadata=meta)
    except Exception:
        log.exception("page beacon: usage_event write failed")
    try:
        from marketing_crm.tracking.client import _amplitude
        _amplitude("page_view", email, None, {"path": path})
    except Exception:
        pass


def register(app):
    """Register the page beacon. Always registered (self-gates on TRACKING_ENABLED)."""
    app.register_blueprint(page_bp)
    return True
