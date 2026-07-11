# offline_conversions/blueprint.py — the HTTPS feed Google Ads' scheduled upload fetches.
#
# SHARED, PORTABLE. GET /feeds/google-ads/offline-conversions.csv returns the CSV. Auth is HTTP Basic
# against GOOGLE_ADS_FEED_USER / GOOGLE_ADS_FEED_PASS — Google's scheduled-upload UI sends Basic creds,
# so the secret is NEVER in the URL or access logs (mirrors the ops-endpoint header-only rule). The
# route is DARK until both env vars are set (returns 404) so an unconfigured deploy exposes nothing.
# Read-only. Serves a rolling window (Google only accepts clicks < 90 days old and dedupes re-serves).

import base64
import hmac
import logging
import os

from flask import Blueprint, Response, request
from sqlalchemy import text

log = logging.getLogger("offline_conversions.blueprint")
offline_conv_bp = Blueprint("offline_conversions", __name__)

_WINDOW_DAYS = int(os.environ.get("GOOGLE_ADS_FEED_WINDOW_DAYS", "90") or 90)


def _session_scope():
    """Portable session — nextpoint exposes db.session_scope; ten-fifty5 exposes core_db.db."""
    try:
        from db import session_scope
    except ImportError:
        from core_db.db import session_scope
    return session_scope()


def _auth_state():
    """None = feature dark (no creds set). True/False = Basic creds present + (mis)match."""
    user = os.environ.get("GOOGLE_ADS_FEED_USER", "").strip()
    pw = os.environ.get("GOOGLE_ADS_FEED_PASS", "").strip()
    if not user or not pw:
        return None
    hdr = request.headers.get("Authorization", "")
    if not hdr.lower().startswith("basic "):
        return False
    try:
        dec = base64.b64decode(hdr.split(" ", 1)[1]).decode("utf-8", "ignore")
        u, _, p = dec.partition(":")
    except Exception:
        return False
    return hmac.compare_digest(u, user) and hmac.compare_digest(p, pw)


@offline_conv_bp.route("/feeds/google-ads/offline-conversions.csv", methods=["GET"])
def offline_conversions_csv():
    state = _auth_state()
    if state is None:
        return ("", 404)                      # dark: no feed credentials configured
    if not state:
        return Response("unauthorized", status=401,
                        headers={"WWW-Authenticate": 'Basic realm="ads-feed"'})
    from offline_conversions.feed import build_csv
    rows = []
    try:
        with _session_scope() as s:
            rows = [dict(r) for r in s.execute(text(f"""
                SELECT gclid, action_name, occurred_at, value_minor, currency
                FROM core.offline_conversion
                WHERE created_at >= now() - interval '{_WINDOW_DAYS} days'
                ORDER BY occurred_at
            """)).mappings().all()]
    except Exception:
        log.exception("offline-conversions feed query failed")
    body = build_csv(rows)
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=offline-conversions.csv"})


def register(app):
    app.register_blueprint(offline_conv_bp)
    return True
