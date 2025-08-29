# upload_app.py
import os
from flask import Flask, jsonify, render_template, send_from_directory, Response, request

BOOT_TAG = os.getenv("DEPLOY_TAG", os.getenv("RENDER_GIT_COMMIT", "local")[:7] or "local")
print("=== BOOT upload_app ===", BOOT_TAG)

# ❗️One — and only one — Flask app.
app = Flask(__name__, template_folder="templates", static_folder="static")

# ---------------- Health & diagnostics ----------------
@app.get("/healthz")
def healthz():
    # Plain text is fine for Render’s health check
    return "OK", 200

@app.get("/__alive")
def __alive():
    return jsonify({"ok": True, "tag": BOOT_TAG, "routes": len(list(app.url_map.iter_rules()))})

@app.get("/_alive")  # extra alias used by some platforms/tools
def _alive_alias():
    return "OK", 200

@app.get("/__routes")
def __routes():
    routes = sorted(
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})
        }
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# ---------------- Minimal UI so /upload never 404s ----------------
_INLINE_UPLOAD = """<!doctype html><meta charset="utf-8">
<title>Upload</title><body style="font-family:sans-serif;background:#0b1220;color:#fff">
<h2>🎾 Upload UI (fallback)</h2>
<p>If templates/upload.html exists, it will be rendered instead.</p>
<form id="f"><input type="email" placeholder="Email" required>
<input type="file" required><button>Upload</button></form>
<pre id="s"></pre>
<script>document.getElementById('f').onsubmit=e=>{e.preventDefault();document.getElementById('s').textContent='Stub UI ok.'}</script>
"""

@app.get("/upload")
@app.get("/upload/")
def upload_page():
    try:
        return render_template("upload.html")
    except Exception:
        return _INLINE_UPLOAD, 200, {"Content-Type": "text/html; charset=utf-8"}

# If you keep manual static for /upload assets, leave this one single handler
@app.get("/upload/static/<path:filename>")
def upload_static(filename):
    base = os.path.join(app.root_path, "static", "upload")
    return send_from_directory(base, filename)

@app.get("/")
def root():
    return jsonify({"service": "NextPoint Upload", "ok": True, "see": ["/upload", "/__routes"], "tag": BOOT_TAG})

# ---------------- Optional: mount UI blueprint (non-fatal) --------
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# Log routes once so you can verify in Render logs
try:
    print("=== ROUTES LOADED (%d) ===" % len(list(app.url_map.iter_rules())))
    for r in app.url_map.iter_rules():
        print("ROUTE", r.rule, "endpoint=", r.endpoint, "methods=", sorted(r.methods))
    print("=== END ROUTES ===")
except Exception:
    pass
