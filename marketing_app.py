# marketing_app.py — Standalone Flask server for the PUBLIC marketing site (Render).
#
# This is the SEO-critical, pre-login public website. It is served at the apex
# domain (www.ten-fifty5.com) once cutover is complete, REPLACING the Wix-hosted
# marketing pages. The whole point: native, fully-crawlable HTML — no iframe, no
# Wix JavaScript soup — so Google reads the same rich content a visitor sees.
#
# It serves the indexed URLs verbatim so existing rankings carry over on cutover:
#   GET /              → home.html           (landing)
#   GET /overview      → how_it_works.html   (how it works)
#   GET /pricing       → pricing_public.html (marketing pricing)
#   GET /coaching      → for_coaches.html
#   GET /contact-us    → contact.html
#   GET /blog          → blog/index.html     (blog hub)
#   GET /post/<slug>   → blog/<slug>.html    (migrated Wix blog posts, same URLs)
#   GET /robots.txt    → generated
#   GET /sitemap.xml   → generated from MARKETING_URLS + blog posts
#
# The logged-in app (Locker Room, dashboards, media room, checkout) is NOT here —
# it stays on Wix (login + portal shell) at APP_BASE_URL, embedding the existing
# Render locker-room service exactly as today. This service is marketing ONLY,
# has no database, and installs only flask + gunicorn.
#
# Start command: gunicorn marketing_app:app

import os
import glob
from flask import Flask, send_file, Response, abort, redirect, jsonify

app = Flask(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
BLOG_DIR = os.path.join(FRONTEND_DIR, "blog")

# Canonical public host (where this service is served). Apex/non-www should 301
# to this. Override per-env; default is the production marketing host.
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://www.ten-fifty5.com").rstrip("/")

# Where the logged-in app lives (Wix login + portal). Marketing "Log in / Get
# started" CTAs point here. Single source of truth so the subdomain is a 1-line
# change at cutover.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://my.ten-fifty5.com").rstrip("/")


def _html(name: str):
    path = os.path.join(FRONTEND_DIR, name)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path)


# ----------------------------------------------------------------
# Public marketing pages — served at the SEO-indexed paths
# ----------------------------------------------------------------

@app.get("/")
def home():
    return _html("home.html")


@app.get("/overview")
def overview():
    return _html("how_it_works.html")


@app.get("/pricing")
def pricing():
    return _html("pricing_public.html")


@app.get("/coaching")
def coaching():
    return _html("for_coaches.html")


@app.get("/contact-us")
def contact():
    return _html("contact.html")


@app.get("/blog")
def blog_index():
    return _html(os.path.join("blog", "index.html"))


@app.get("/post/<slug>")
def blog_post(slug: str):
    # Slug is path-segment only (Flask won't match slashes); guard anyway.
    if "/" in slug or "\\" in slug or slug.startswith("."):
        abort(404)
    return _html(os.path.join("blog", f"{slug}.html"))


# ----------------------------------------------------------------
# Legacy / convenience redirects (preserve any old internal links)
# ----------------------------------------------------------------

@app.get("/home")
def home_legacy():
    return redirect("/", code=301)


@app.get("/how-it-works")
def how_it_works_legacy():
    return redirect("/overview", code=301)


@app.get("/pricing-public")
def pricing_public_legacy():
    return redirect("/pricing", code=301)


@app.get("/for-coaches")
def for_coaches_legacy():
    return redirect("/coaching", code=301)


# ----------------------------------------------------------------
# Crawl infrastructure (audit #6)
# ----------------------------------------------------------------

def _blog_slugs():
    """Slugs of every migrated blog post (frontend/blog/<slug>.html, minus index)."""
    if not os.path.isdir(BLOG_DIR):
        return []
    slugs = []
    for p in sorted(glob.glob(os.path.join(BLOG_DIR, "*.html"))):
        name = os.path.splitext(os.path.basename(p))[0]
        if name != "index":
            slugs.append(name)
    return slugs


# Static marketing URLs (path, change-frequency, priority). Order = importance.
MARKETING_URLS = [
    ("/", "weekly", "1.0"),
    ("/overview", "monthly", "0.9"),
    ("/pricing", "monthly", "0.9"),
    ("/coaching", "monthly", "0.8"),
    ("/blog", "weekly", "0.7"),
    ("/contact-us", "yearly", "0.4"),
]


@app.get("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {SITE_BASE_URL}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for path, freq, prio in MARKETING_URLS:
        parts.append("  <url>")
        parts.append(f"    <loc>{SITE_BASE_URL}{path}</loc>")
        parts.append(f"    <changefreq>{freq}</changefreq>")
        parts.append(f"    <priority>{prio}</priority>")
        parts.append("  </url>")
    for slug in _blog_slugs():
        parts.append("  <url>")
        parts.append(f"    <loc>{SITE_BASE_URL}/post/{slug}</loc>")
        parts.append("    <changefreq>monthly</changefreq>")
        parts.append("    <priority>0.6</priority>")
        parts.append("  </url>")
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


# ----------------------------------------------------------------
# Ops
# ----------------------------------------------------------------

@app.get("/__alive")
def alive():
    return jsonify({"ok": True, "service": "marketing"})


@app.errorhandler(404)
def not_found(e):
    # Serve a friendly branded 404 if present, else minimal text.
    path = os.path.join(FRONTEND_DIR, "404.html")
    if os.path.isfile(path):
        return send_file(path), 404
    return Response("Not found", status=404, mimetype="text/plain")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5060))
    app.run(host="0.0.0.0", port=port, debug=False)
