# marketing_crm/tracking/beacon.py — public page-view beacon (navigation analytics).
#
# POST /api/track/page records a page_view into core.usage_event (account resolved by email when the
# page is authed; anonymous otherwise) + Amplitude. Designed for navigator.sendBeacon: body is parsed
# from the raw request (text/plain), so there is NO CORS preflight — works from the public marketing
# pages and the member SPAs alike. Never blocks; gated by TRACKING_ENABLED (returns tracked:false off).
#
# SHARED ENGINE (replicable across sites — kept in lock-step with the nextpoint repo). The metadata
# contract is identical everywhere so the analytics aggregation is portable:
#   page_view  → {path, referrer, anon_id?, country?, device, browser, os, tz?, lang?, sw?, pvid?, utm_*?}
#   page_leave → {path, pvid?, anon_id?, duration_ms}   (powers time-on-site; append-only, no UPDATE)
# Cookieless: the only client identifier is a first-party anon_id (localStorage). country comes from
# the CDN edge header (CF-IPCountry); device/browser/os are parsed server-side from the User-Agent.
#
# page_view is intentionally NOT forwarded to Klaviyo (would be noisy/expensive) — DB + Amplitude only.

import json
import logging
import threading

from flask import Blueprint, jsonify, request

log = logging.getLogger("marketing_crm.tracking.beacon")
page_bp = Blueprint("mc_page_beacon", __name__)


def _parse_ua(ua):
    """Tiny User-Agent classifier → (device, browser, os). No dependency. Shared verbatim with
    the nextpoint repo so device analytics are identical across sites."""
    u = (ua or "").lower()
    if "ipad" in u or ("tablet" in u and "mobile" not in u):
        device = "tablet"
    elif any(k in u for k in ("mobi", "iphone", "android", "ipod")) and "ipad" not in u:
        device = "mobile"
    else:
        device = "desktop"
    if "iphone" in u or "ipad" in u or "ipod" in u:
        os_ = "iOS"
    elif "android" in u:
        os_ = "Android"
    elif "windows" in u:
        os_ = "Windows"
    elif "mac os" in u or "macintosh" in u:
        os_ = "macOS"
    elif "linux" in u:
        os_ = "Linux"
    else:
        os_ = "Other"
    if "edg" in u:
        browser = "Edge"
    elif "opr" in u or "opera" in u:
        browser = "Opera"
    elif "samsungbrowser" in u:
        browser = "Samsung"
    elif "firefox" in u or "fxios" in u:
        browser = "Firefox"
    elif "chrome" in u or "crios" in u:
        browser = "Chrome"
    elif "safari" in u:
        browser = "Safari"
    else:
        browser = "Other"
    return device, browser, os_


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
    # Client-supplied, non-identifying context (device sizing + locale + ephemeral pageview id).
    pvid = (str(body.get("pvid") or "")[:48]) or None
    tz = (str(body.get("tz") or "")[:60]) or None
    lang = (str(body.get("lang") or "")[:20]) or None
    sw = str(body.get("sw") or "")
    screen_w = int(sw) if sw.isdigit() else None
    is_leave = (body.get("event") == "leave")
    dur = body.get("duration_ms")
    duration_ms = int(dur) if isinstance(dur, (int, float)) and 0 < dur <= 6 * 60 * 60 * 1000 else None
    # Geolocation (country) from the edge: a CDN (Cloudflare) fronting the service adds CF-IPCountry.
    # Must be read here (request context); the writer thread has none. 'XX'/'T1' = unknown/Tor.
    country = (request.headers.get("CF-IPCountry")
               or request.headers.get("X-Country-Code") or "").strip().upper()[:2]
    if country in ("", "XX", "T1"):
        country = None
    device, browser, os_ = _parse_ua(request.headers.get("User-Agent", ""))
    ctx = {"anon_id": anon_id, "utm": utm, "props": props, "country": country,
           "device": device, "browser": browser, "os": os_, "tz": tz, "lang": lang,
           "screen_w": screen_w, "pvid": pvid, "is_leave": is_leave, "duration_ms": duration_ms}
    try:
        threading.Thread(target=_record, args=(path, email, referrer, ctx), daemon=True).start()
    except Exception:
        log.exception("page beacon: thread spawn failed")
    return jsonify({"ok": True})


def _record(path, email, referrer, ctx):
    try:
        from core_db.db import session_scope
        from core_db.repositories import accounts, matches
        with session_scope() as s:
            account_id = None
            if email:
                a = accounts.get_account_by_email(s, email)
                if a:
                    account_id = a.id
            # Time-on-site: a page_leave is a separate append-only event carrying the duration.
            if ctx.get("is_leave"):
                if not ctx.get("duration_ms"):
                    return
                meta = {"path": path, "duration_ms": ctx["duration_ms"]}
                if ctx.get("anon_id"):
                    meta["anon_id"] = ctx["anon_id"]
                if ctx.get("pvid"):
                    meta["pvid"] = ctx["pvid"]
                matches.record_usage(s, event_type="page_leave", account_id=account_id,
                                     ref_type="page", ref_id=path, metadata=meta)
                return
            meta = {"path": path, "referrer": referrer}
            for k in ("anon_id", "country", "device", "browser", "os", "tz", "lang", "pvid"):
                if ctx.get(k):
                    meta[k] = ctx[k]
            if ctx.get("screen_w"):
                meta["screen_w"] = ctx["screen_w"]
            for k in ("source", "medium", "campaign", "term", "content"):
                v = (ctx.get("utm") or {}).get(k)
                if v:
                    meta["utm_" + k] = str(v)[:120]
            for k in list(ctx.get("props") or {})[:10]:
                meta[str(k)[:40]] = str(ctx["props"][k])[:200]
            if account_id is None and email:
                meta["email_unmatched"] = email
            matches.record_usage(s, event_type="page_view", account_id=account_id,
                                 ref_type="page", ref_id=path, metadata=meta)
    except Exception:
        log.exception("page beacon: usage_event write failed")
    if not ctx.get("is_leave"):
        try:
            from marketing_crm.tracking.client import _amplitude
            _amplitude("page_view", email, None, {"path": path})
        except Exception:
            pass


def register(app):
    """Register the page beacon. Always registered (self-gates on TRACKING_ENABLED)."""
    app.register_blueprint(page_bp)
    return True
