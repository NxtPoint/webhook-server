# upload_app.py
import os
from flask import Flask, jsonify, Response, request, send_from_directory

# single Flask app instance
app = Flask(__name__, template_folder="templates", static_folder="static")

BOOT_TAG = os.getenv("RENDER_GIT_COMMIT", "local")[:7]
OPS_KEY  = os.environ.get("OPS_KEY", "")

def _guard_ok() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        auth = auth.split(" ", 1)[1].strip()
    hk = request.headers.get("X-OPS-Key") or auth
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

# ---------- health & root ----------
@app.get("/")
def root_ok():
    return jsonify(ok=True, service="NextPoint Upload API", tag=BOOT_TAG)

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

@app.get("/__whoami")
def whoami():
    return jsonify(
        ok=True,
        file=__file__,
        cwd=os.getcwd(),
        tag=BOOT_TAG,
        routes=len(list(app.url_map.iter_rules())),
    )

# ---------- routes (open + locked) ----------
@app.get("/__routes")
def routes_open():
    routes = [
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}),
        }
        for r in app.url_map.iter_rules()
    ]
    routes.sort(key=lambda x: x["rule"])
    return jsonify(ok=True, count=len(routes), routes=routes)

@app.get("/ops/routes")
def routes_locked():
    if not _guard_ok():
        return Response("Forbidden", 403)
    # reuse same listing
    routes = [
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}),
        }
        for r in app.url_map.iter_rules()
    ]
    routes.sort(key=lambda x: x["rule"])
    return jsonify(ok=True, count=len(routes), routes=routes)

# ---------- DB ping (kept because you’re using it) ----------
try:
    from sqlalchemy import text
    from db_init import engine

    @app.get("/ops/db-ping")
    def db_ping():
        if not _guard_ok():
            return Response("Forbidden", 403)
        with engine.connect() as conn:
            now = conn.execute(text("SELECT now() AT TIME ZONE 'utc'")).scalar_one()
        return jsonify(ok=True, now_utc=str(now))
except Exception as e:
    print("db_ping disabled (no DB available at import):", e)

# ---------- /upload blueprint (optional) ----------
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# serve /upload/static/* from static/upload/*
@app.get("/upload/static/<path:filename>")
def upload_static(filename):
    base = os.path.join(app.root_path, "static", "upload")
    return send_from_directory(base, filename)

# Log routes once at startup (seen in Render logs)
print("=== ROUTES (startup) ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    methods = ",".join(sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}))
    print(f"{r.rule:28s} -> {r.endpoint:20s} [{methods}]")
print("=== END ROUTES ===")
