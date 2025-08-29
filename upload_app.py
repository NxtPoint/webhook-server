import os
from flask import Flask, jsonify, render_template, send_from_directory

BOOT_TAG = os.getenv("RENDER_GIT_COMMIT", "local")[:7]
print("=== BOOT upload_app ===", BOOT_TAG)

app = Flask(__name__, template_folder="templates", static_folder="static")

# ---- HEALTH ENDPOINTS (registered in multiple paths) ----
def _health():
    return jsonify({
        "ok": True,
        "app": "upload_app",
        "tag": BOOT_TAG,
        "routes": len(list(app.url_map.iter_rules()))
    }), 200

# register on several common paths so any setting works
for p in ("/healthz", "/alive", "/_alive", "/__alive"):
    app.add_url_rule(p, endpoint=f"health_{p}", view_func=_health, methods=["GET", "HEAD"])

# ---- DIAGNOSTICS ----
@app.get("/__routes")
def __routes():
    routes = sorted(
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}),
        }
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# ---- ROOT & UPLOAD ----
@app.get("/")
def root():
    return jsonify({"service": "NextPoint Upload", "ok": True, "see": ["/upload", "/__routes"], "tag": BOOT_TAG})

INLINE_UPLOAD = """<!doctype html><meta charset="utf-8">
<title>Upload</title><body style="font-family:sans-serif;background:#0b1220;color:#fff">
<h2>🎾 Upload UI Shell</h2>
<p>This is the inline fallback. Add templates/upload.html to override.</p>
</body>"""

@app.get("/upload")
@app.get("/upload/")
def upload_page():
    try:
        return render_template("upload.html")
    except Exception:
        return INLINE_UPLOAD, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.get("/upload/static/<path:filename>")
def upload_static(filename):
    base = os.path.join(app.root_path, "static", "upload")
    return send_from_directory(base, filename)

# Optional: mount UI blueprint if present (safe if missing)
try:
    from ui_app import ui_bp  # noqa
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# Dump routes at startup for verification in Render logs
print("=== ROUTES LOADED ===")
for r in app.url_map.iter_rules():
    print(f"{r.rule} -> {r.endpoint} [{','.join(sorted(r.methods))}]")
print("=== END ROUTES ===")
