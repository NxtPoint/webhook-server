# locker_room_app.py — Standalone Flask server for the Locker Room service (Render).
#
# Serves client-facing HTML SPAs as static files via send_file():
#
# Members area (logged-in):
#   GET /              → locker_room.html   (dashboard: matches, stats, video playback)
#   GET /register      → players_enclosure.html (onboarding wizard)
#   GET /media-room    → media_room.html    (video upload wizard)
#   GET /backoffice    → backoffice.html    (admin dashboard)
#   GET /match-analysis → match_analysis.html (match analytics dashboard)
#   GET /portal        → portal.html        (unified nav shell, main Wix entry point)
#   GET /pricing       → pricing.html       (entitlement-aware plans page, inside portal)
#   GET /coach-accept  → coach_accept.html  (coach invitation acceptance)
#
# Public marketing pages (pre-login; primary host is Wix, these are same-origin backups):
#   GET /home          → home.html          (landing)
#   GET /how-it-works  → how_it_works.html
#   GET /pricing-public → pricing_public.html (marketing pricing — distinct from /pricing)
#   GET /for-coaches   → for_coaches.html
#
# No database connection — all data access goes through the webhook-server API.
# Only installs flask + gunicorn (not full requirements.txt).
# Start command: gunicorn locker_room_app:app

import os
from flask import Flask, send_file, jsonify

app = Flask(__name__)


@app.get("/")
def index():
    return send_file("locker_room.html")


@app.get("/register")
def players_enclosure():
    return send_file("players_enclosure.html")


@app.get("/media-room")
def media_room():
    return send_file("media_room.html")


@app.get("/backoffice")
def backoffice():
    return send_file("backoffice.html")



@app.get("/practice")
def practice():
    return send_file("practice.html")


@app.get("/match-analysis")
def match_analysis():
    return send_file("match_analysis.html")


@app.get("/portal")
def portal():
    return send_file("portal.html")


@app.get("/pricing")
def pricing():
    return send_file("pricing.html")


@app.get("/coach-accept")
def coach_accept():
    return send_file("coach_accept.html")


# ----------------------------------------------------------------
# Public marketing pages — served same-origin as backup to Wix hosting
# ----------------------------------------------------------------

@app.get("/home")
def public_home():
    return send_file("home.html")


@app.get("/how-it-works")
def public_how_it_works():
    return send_file("how_it_works.html")


@app.get("/pricing-public")
def public_pricing():
    return send_file("pricing_public.html")


@app.get("/for-coaches")
def public_for_coaches():
    return send_file("for_coaches.html")


@app.get("/__alive")
def alive():
    return jsonify({"ok": True, "service": "locker-room"})


@app.errorhandler(404)
def not_found(e):
    """Return JSON for API paths that accidentally hit the locker-room service."""
    return jsonify({"ok": False, "error": "not_found", "service": "locker-room"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
