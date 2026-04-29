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

# All SPA HTML lives in frontend/ — resolve by absolute path so the service
# doesn't depend on the process cwd.
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")


def _html(name: str):
    return send_file(os.path.join(FRONTEND_DIR, name))


@app.get("/")
def index():
    return _html("locker_room.html")


@app.get("/register")
def players_enclosure():
    return _html("players_enclosure.html")


@app.get("/media-room")
def media_room():
    return _html("media_room.html")


@app.get("/backoffice")
def backoffice():
    return _html("backoffice.html")


@app.get("/practice")
def practice():
    return _html("practice.html")


@app.get("/match-analysis")
def match_analysis():
    return _html("match_analysis.html")


@app.get("/portal")
def portal():
    return _html("portal.html")


@app.get("/pricing")
def pricing():
    return _html("pricing.html")


@app.get("/coach-accept")
def coach_accept():
    return _html("coach_accept.html")


@app.get("/help")
def help_page():
    return _html("support.html")


# ----------------------------------------------------------------
# Public marketing pages — served same-origin as backup to Wix hosting
# ----------------------------------------------------------------

@app.get("/home")
def public_home():
    return _html("home.html")


@app.get("/how-it-works")
def public_how_it_works():
    return _html("how_it_works.html")


@app.get("/pricing-public")
def public_pricing():
    return _html("pricing_public.html")


@app.get("/for-coaches")
def public_for_coaches():
    return _html("for_coaches.html")


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
