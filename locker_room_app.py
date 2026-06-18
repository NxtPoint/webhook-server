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
# PUBLIC MARKETING SITE (host-aware — see below):
#   When the request host is a marketing host (www.ten-fifty5.com / ten-fifty5.com)
#   this same service serves the native, fully-crawlable marketing site so we get
#   the SEO win WITHOUT paying for a second Render service. On every other host
#   (the onrender URL the Wix portal embeds, my.ten-fifty5.com, etc.) behaviour is
#   UNCHANGED — `/` is still the Locker Room dashboard. Only `/` and `/pricing`
#   are host-switched; all the marketing-only paths are pure additions.
#     GET /            → home.html            (marketing host only; else dashboard)
#     GET /overview    → how_it_works.html
#     GET /pricing     → pricing_public.html  (marketing host only; else app pricing)
#     GET /coaching    → for_coaches.html
#     GET /academies   → for_academies.html
#     GET /contact-us  → contact.html
#     GET /blog        → blog/index.html
#     GET /post/<slug> → blog/<slug>.html     (migrated posts, original URLs)
#     GET /robots.txt, /sitemap.xml → generated
#
# No database connection — all data access goes through the main API ("Sport AI - API call" on Render).
# Only installs flask + gunicorn (not full requirements.txt).
# Start command: gunicorn locker_room_app:app

import os
import glob
from flask import Flask, send_file, jsonify, request, Response, redirect, abort

app = Flask(__name__)

# All SPA HTML lives in frontend/ — resolve by absolute path so the service
# doesn't depend on the process cwd.
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
BLOG_DIR = os.path.join(FRONTEND_DIR, "blog")

# Canonical public marketing host (used in robots/sitemap output).
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://www.ten-fifty5.com").rstrip("/")
# Where the logged-in app lives (Wix login + portal). Marketing CTAs already
# point here in the HTML; kept here for reference / future use.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://info5945780.wixstudio.com/online-tennis-analyt").rstrip("/")

# Hosts that should see the marketing site at `/`. Extra hosts can be added via
# the MARKETING_HOSTS env var (comma-separated) without a code change.
_DEFAULT_MARKETING_HOSTS = {"www.ten-fifty5.com", "ten-fifty5.com"}
_env_hosts = {h.strip().lower() for h in os.environ.get("MARKETING_HOSTS", "").split(",") if h.strip()}
MARKETING_HOSTS = _DEFAULT_MARKETING_HOSTS | _env_hosts


# App pages that DON'T get the auth_client.js shim: marketing/SEO pages (no portal,
# keep them lean), the blog, and login.html (carries its own Clerk logic).
_NO_AUTH_CLIENT = {
    "home.html", "how_it_works.html", "pricing_public.html", "for_coaches.html",
    "for_academies.html", "contact.html", "404.html", "login.html",
}


def _html(name: str):
    path = os.path.join(FRONTEND_DIR, name)
    if not os.path.isfile(path):
        abort(404)
    # Auto-inject page-view analytics into every served HTML page (marketing + member).
    # Server-side beacon self-gates on TRACKING_ENABLED, so this is inert until that's on.
    if name.endswith(".html"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
            # Shared dual-mode auth helper (TFAuth) for the logged-in app pages.
            # In <head>, non-defer, so window.TFAuth exists before page scripts run.
            # Skipped for marketing/blog/login; legacy (Wix) behaviour is unchanged
            # because TFAuth only acts in clerk mode (no ?key= + a Clerk session).
            base = os.path.basename(name)
            # Check for the actual <script src> tag, NOT the bare substring — the
            # converted pages mention "/auth_client.js" in a comment, which must
            # not be mistaken for an already-present include.
            if (base not in _NO_AUTH_CLIENT and not base.startswith("blog")
                    and 'src="/auth_client.js"' not in html):
                auth_tag = '<script src="/auth_client.js"></script>'
                if "</head>" in html:
                    html = html.replace("</head>", auth_tag + "\n</head>", 1)
                else:
                    html = auth_tag + "\n" + html
            if "/analytics.js" not in html:
                tag = '<script src="/analytics.js" defer></script>'
                html = html.replace("</body>", tag + "\n</body>", 1) if "</body>" in html else html + tag
            return Response(html, mimetype="text/html")
        except Exception:
            return send_file(path)
    return send_file(path)


def _is_marketing_host() -> bool:
    """True when the request arrived on the public marketing domain."""
    host = (request.host or "").split(":")[0].lower()
    return host in MARKETING_HOSTS


# ----------------------------------------------------------------
# Host-switched roots (`/` and `/pricing`)
# On a marketing host → marketing page. Everywhere else → unchanged app page.
# ----------------------------------------------------------------

@app.get("/")
def index():
    if _is_marketing_host():
        return _html("home.html")
    return _html("locker_room.html")


@app.get("/pricing")
def pricing():
    if _is_marketing_host():
        return _html("pricing_public.html")
    return _html("pricing.html")


@app.get("/register")
def players_enclosure():
    return _html("players_enclosure.html")


@app.get("/media-room")
def media_room():
    return _html("media_room.html")


@app.get("/backoffice")
def backoffice():
    return _html("backoffice.html")


@app.get("/cockpit")
def cockpit():
    # Internal admin cockpit (marketing_crm). Static SPA; data via the main API
    # (/api/client/backoffice/cockpit/*), admin-gated there.
    return _html("cockpit.html")


@app.get("/feedback_widget.js")
def feedback_widget_js():
    # In-app feedback + NPS widget (marketing_crm). Included by portal/dashboard pages.
    return _html("feedback_widget.js")


@app.get("/consent.js")
def consent_js():
    # Consent screens module (marketing_crm). Included by signup / technique / settings pages.
    return _html("consent.js")


@app.get("/analytics.js")
def analytics_js():
    # Page-view analytics, auto-injected into served HTML pages.
    return _html("analytics.js")


@app.get("/privacy")
@app.get("/consent")
def privacy_consent():
    # Self-serve "Privacy & Consent" preferences screen (logged-in member).
    # Polished SPA matching the Locker Room design system; dual-mode TFAuth;
    # reads/writes /api/client/consent/* on the main API.
    return _html("consent.html")


@app.get("/privacy-settings")
def privacy_settings():
    # Legacy minimal consent widget (consent.js stub). Kept as a fallback;
    # the canonical screen is /privacy (consent.html) above.
    return _html("privacy_settings.html")


@app.get("/practice")
def practice():
    return _html("practice.html")


@app.get("/match-analysis")
def match_analysis():
    return _html("match_analysis.html")


@app.get("/portal")
def portal():
    return _html("portal.html")


@app.get("/dashboard")
def dashboard():
    # Dedicated, NON host-switched route for the Locker Room dashboard SPA. The
    # portal nav loads this (not '/', which serves marketing on a marketing host).
    return _html("locker_room.html")


@app.get("/plans")
def plans():
    # Dedicated, NON host-switched route for the in-app pricing/plans SPA. The portal
    # nav loads this (not '/pricing', which serves the marketing page on a marketing host).
    return _html("pricing.html")


def _serve_login():
    """Serve login.html with Clerk config injected from env. The publishable key
    is PUBLIC (browser-side by design), so substituting it into the page is fine.
    Dark until AUTH_V2_ENABLED=1 + CLERK_PUBLISHABLE_KEY set — until then the page
    renders a graceful 'being set up' notice. Wix login is unaffected."""
    path = os.path.join(FRONTEND_DIR, "login.html")
    if not os.path.isfile(path):
        abort(404)
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    html = (html
            .replace("__CLERK_PUBLISHABLE_KEY__", os.getenv("CLERK_PUBLISHABLE_KEY", "").strip())
            .replace("__CLERK_AFTER_LOGIN__", os.getenv("AUTH_AFTER_LOGIN_URL", "/portal").strip())
            .replace("__AUTH_V2_ENABLED__", os.getenv("AUTH_V2_ENABLED", "0").strip())
            .replace("__AUTH_API_BASE__", os.getenv("AUTH_API_BASE", "https://api.nextpointtennis.com").strip())
            .replace("__CLERK_JWT_TEMPLATE__", os.getenv("CLERK_JWT_TEMPLATE", "").strip()))
    if "/analytics.js" not in html:
        html = html.replace("</body>", '<script src="/analytics.js" defer></script>\n</body>', 1)
    return Response(html, mimetype="text/html")


@app.get("/login")
def login_page():
    return _serve_login()


@app.get("/auth_client.js")
def auth_client_js():
    """Shared dual-mode auth helper (TFAuth) with Clerk config injected from env.
    All public values. Lazy: loads Clerk only when a page actually uses clerk mode."""
    path = os.path.join(FRONTEND_DIR, "auth_client.js")
    if not os.path.isfile(path):
        abort(404)
    with open(path, "r", encoding="utf-8") as f:
        js = f.read()
    js = (js
          .replace("__AUTH_V2_ENABLED__", os.getenv("AUTH_V2_ENABLED", "0").strip())
          .replace("__CLERK_PUBLISHABLE_KEY__", os.getenv("CLERK_PUBLISHABLE_KEY", "").strip())
          .replace("__CLERK_JWT_TEMPLATE__", os.getenv("CLERK_JWT_TEMPLATE", "").strip()))
    return Response(js, mimetype="application/javascript")


@app.get("/coach-accept")
def coach_accept():
    return _html("coach_accept.html")


@app.get("/help")
def help_page():
    return _html("support.html")


# ----------------------------------------------------------------
# Public marketing pages — served at the SEO-indexed paths
# (Harmless on the app host; meaningful when www points at this service.)
# ----------------------------------------------------------------

@app.get("/overview")
def overview():
    return _html("how_it_works.html")


@app.get("/coaching")
def coaching():
    return _html("for_coaches.html")


@app.get("/academies")
def academies():
    return _html("for_academies.html")


@app.get("/contact-us")
def contact():
    return _html("contact.html")


@app.get("/blog")
def blog_index():
    return _html(os.path.join("blog", "index.html"))


@app.get("/post/<slug>")
def blog_post(slug: str):
    if "/" in slug or "\\" in slug or slug.startswith("."):
        abort(404)
    return _html(os.path.join("blog", f"{slug}.html"))


@app.get("/blog/images/<filename>")
def blog_image(filename: str):
    # Static blog hero/thumbnail images (frontend/blog/images/*).
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(404)
    return _html(os.path.join("blog", "images", filename))


# Favicon + touch icons (avoid the silent /favicon.ico 404 and brand the tab).
@app.get("/favicon.svg")
def favicon_svg():
    return _html("favicon.svg")


@app.get("/favicon.ico")
def favicon_ico():
    return _html("favicon.ico")


@app.get("/favicon.png")
def favicon_png():
    return _html("favicon.png")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return _html("apple-touch-icon.png")


@app.get("/og/<filename>")
def og_image(filename: str):
    # Open Graph / social-share images (frontend/og/*).
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(404)
    return _html(os.path.join("og", filename))


# Legacy same-origin marketing backups (kept; their canonicals point at the
# clean URLs above so search engines don't treat them as duplicates).
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


@app.get("/for-academies")
def public_for_academies():
    return _html("for_academies.html")


# ----------------------------------------------------------------
# Crawl infrastructure (only meaningful on the marketing host, harmless elsewhere)
# ----------------------------------------------------------------

_MARKETING_URLS = [
    ("/", "weekly", "1.0"),
    ("/overview", "monthly", "0.9"),
    ("/pricing", "monthly", "0.9"),
    ("/coaching", "monthly", "0.8"),
    ("/academies", "monthly", "0.8"),
    ("/blog", "weekly", "0.7"),
    ("/contact-us", "yearly", "0.4"),
]


def _blog_slugs():
    if not os.path.isdir(BLOG_DIR):
        return []
    out = []
    for p in sorted(glob.glob(os.path.join(BLOG_DIR, "*.html"))):
        name = os.path.splitext(os.path.basename(p))[0]
        if name != "index":
            out.append(name)
    return out


@app.get("/robots.txt")
def robots_txt():
    body = "User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % SITE_BASE_URL
    return Response(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, freq, prio in _MARKETING_URLS:
        parts += ["  <url>", f"    <loc>{SITE_BASE_URL}{path}</loc>",
                  f"    <changefreq>{freq}</changefreq>",
                  f"    <priority>{prio}</priority>", "  </url>"]
    for slug in _blog_slugs():
        parts += ["  <url>", f"    <loc>{SITE_BASE_URL}/post/{slug}</loc>",
                  "    <changefreq>monthly</changefreq>",
                  "    <priority>0.6</priority>", "  </url>"]
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@app.get("/__alive")
def alive():
    return jsonify({"ok": True, "service": "locker-room"})


@app.errorhandler(404)
def not_found(e):
    """Branded HTML 404 for humans; JSON for API/ops paths and JSON clients."""
    path = request.path or ""
    is_api = path.startswith("/api") or path.startswith("/ops")
    if not is_api and request.accept_mimetypes.accept_html:
        page = os.path.join(FRONTEND_DIR, "404.html")
        if os.path.isfile(page):
            return send_file(page), 404
    return jsonify({"ok": False, "error": "not_found", "service": "locker-room"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
