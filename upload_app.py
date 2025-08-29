# upload_app.py
import os
from flask import Flask, jsonify, Response, render_template, send_from_directory, request

BOOT_TAG = os.getenv("DEPLOY_TAG", "boot-"+os.getenv("RENDER_GIT_COMMIT", "local")[:7])
print("=== BOOT upload_app minimal ===", BOOT_TAG)

app = Flask(__name__, template_folder="templates", static_folder="static")

OPS_KEY = os.getenv("OPS_KEY", "")

def _guard_ok() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        bearer = bearer.split(" ", 1)[1].strip()
    hk = request.headers.get("X-OPS-Key") or bearer
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

# Health (both spellings)
@app.get("/_alive")
@app.get("/__alive")
def alive():
    return jsonify({"ok": True, "app": "upload_app", "tag": BOOT_TAG, "routes": len(list(app.url_map.iter_rules()))})

# Open routes dump
@app.get("/__routes")
def routes_open():
    routes = sorted({"rule": r.rule, "endpoint": r.endpoint, "methods": sorted(r.methods)} for r in app.url_map.iter_rules())
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# Locked routes dump
@app.get("/ops/routes")
def routes_locked():
    if not _guard_ok():
        return Response("Forbidden", 403)
    routes = sorted({"rule": r.rule, "endpoint": r.endpoint, "methods": sorted(r.methods)} for r in app.url_map.iter_rules())
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# Always serve /upload (real template if present; otherwise fallback)
INLINE_UPLOAD = """<!doctype html><meta charset="utf-8">
<title>Upload</title><body style="font-family:sans-serif;background:#0b1220;color:#fff">
<h2>🎾 Upload UI Shell</h2>
<p>This is the inline fallback. If you place templates/upload.html it will render instead.</p>
<form id="f"><input type="email" required placeholder="Email"><input type="file" required>
<button>Upload</button></form><pre id="s"></pre>
<script>document.getElementById('f').onsubmit=e=>{e.preventDefault();document.getElementById('s').textContent='Stub UI ok.'}</script>
"""
@app.get("/upload")
@app.get("/upload/")
def upload_page():
    try:
        return render_template("upload.html")
    except Exception:
        return INLINE_UPLOAD, 200, {"Content-Type":"text/html; charset=utf-8"}

# Serve /upload/static/* from static/upload/*
@app.get("/upload/static/<path:filename>")
def upload_static(filename):
    base = os.path.join(app.root_path, "static", "upload")
    return send_from_directory(base, filename)

@app.get("/")
def root():
    return jsonify({"service": "NextPoint Upload (minimal)", "ok": True, "see": ["/upload", "/__routes"], "tag": BOOT_TAG})

# (Optional) mount UI blueprint if it exists—fallbacks above still work even if import fails
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)
