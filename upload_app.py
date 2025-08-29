# upload_app.py
import os
from flask import Flask, jsonify

# IMPORTANT: only one Flask() in the whole file
app = Flask(__name__, template_folder="templates", static_folder="static")

# Ultra-fast health + root. Keep them simple, no DB, no imports.
@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

# Route list for verification
@app.get("/__routes")
def __routes():
    routes = sorted(
        {"rule": r.rule, "endpoint": r.endpoint,
         "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})}
        for r in app.url_map.iter_rules()
    )
    return jsonify(ok=True, count=len(routes), routes=routes)

# …your other routes/blueprints below, but do NOT reassign `app = Flask(...)` again …
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# One-time final route dump (helps confirm what Render is actually serving)
print("=== ROUTES (final) ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    meth = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:24s} -> {r.endpoint:20s} [{meth}]")
print("=== END ROUTES ===")
