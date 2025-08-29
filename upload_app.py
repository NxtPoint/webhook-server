# upload_app.py
import os
from flask import Flask, jsonify, request, Response

BOOT_TAG = os.getenv("DEPLOY_TAG", os.getenv("RENDER_GIT_COMMIT", "local")[:7])

# Single Flask app (do NOT re-create app anywhere else)
app = Flask(__name__, template_folder="templates", static_folder="static")

# ---- OPS key guard ----
OPS_KEY = os.environ.get("OPS_KEY", "")

def _guard_ok() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        bearer = bearer.split(" ", 1)[1].strip()
    hk = request.headers.get("X-OPS-Key") or bearer
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

# ---- Health + root ----
@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

# ---- Who am I (quick diagnostics) ----
@app.get("/__whoami")
def __whoami():
    return jsonify({
        "ok": True,
        "service": "upload_app",
        "tag": BOOT_TAG,
        "port": os.getenv("PORT", "10000"),
        "commit": os.getenv("RENDER_GIT_COMMIT", ""),
        "branch": os.getenv("RENDER_GIT_BRANCH", ""),
        "render_service": os.getenv("RENDER_SERVICE_ID", ""),
    })

# ---- Open routes dump ----
@app.get("/__routes")
def __routes_open():
    routes = [
        {"rule": r.rule, "endpoint": r.endpoint,
         "methods": sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})}
        for r in app.url_map.iter_rules()
    ]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# ---- Locked routes dump (requires OPS_KEY) ----
@app.get("/ops/routes")
def __routes_locked():
    if not _guard_ok():
        return Response("Forbidden", 403)
    routes = [
        {"rule": r.rule, "endpoint": r.endpoint,
         "methods": sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})}
        for r in app.url_map.iter_rules()
    ]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# ---- (Optional) mount UI blueprint if present ----
try:
    from ui_app import ui_bp  # may require DATABASE_URL
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# One-time boot banner + route dump (appears in Render logs)
print("=== BOOT upload_app ===", BOOT_TAG)
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    meth = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:24s} -> {r.endpoint:20s} [{meth}]")
print("=== END ROUTES ===")
