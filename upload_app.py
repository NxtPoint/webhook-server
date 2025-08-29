# upload_app.py
import os
from flask import Flask, jsonify, request, Response

BOOT_TAG = os.getenv("RENDER_GIT_COMMIT", "local")[:7]
app = Flask(__name__, template_folder="templates", static_folder="static")

OPS_KEY = os.getenv("OPS_KEY", "")

def _guard_ok() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        auth = auth.split(" ", 1)[1].strip()
    hk = request.headers.get("X-OPS-Key") or auth
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

@app.get("/__whoami")
def whoami():
    return jsonify({
        "ok": True,
        "service": "upload_app",
        "tag": BOOT_TAG,
        "routes": len(list(app.url_map.iter_rules()))
    })

@app.get("/__routes")
def __routes():
    routes = sorted(
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})
        }
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def ops_routes():
    if not _guard_ok():
        return Response("Forbidden", 403)
    return __routes()

# optional DB ping (safe if db_init missing)
try:
    from sqlalchemy import text
    from db_init import engine
    HAVE_DB = True
except Exception:
    HAVE_DB = False

@app.get("/ops/db-ping")
def db_ping():
    if not _guard_ok():
        return Response("Forbidden", 403)
    if not HAVE_DB:
        return jsonify({"ok": False, "error": "db not available in this build"}), 500
    with engine.connect() as conn:
        now = conn.execute(text("SELECT now() AT TIME ZONE 'utc'")).scalar_one()
    return jsonify({"ok": True, "now_utc": str(now)})

# Try to mount UI, but don't fail boot if missing
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)
