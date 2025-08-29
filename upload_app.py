import os
from flask import Flask, jsonify, request, Response
from werkzeug.utils import secure_filename

try:
    from db_init import engine, db_now
except Exception:
    engine, db_now = None, None

# ---- App ----
app = Flask(__name__, template_folder="templates", static_folder="static")

# ---- Env/ops helpers ----
OPS_KEY = os.getenv("OPS_KEY", "")
def _guard_ok() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        bearer = bearer.split(" ", 1)[1].strip()
    hk = request.headers.get("X-OPS-Key") or bearer
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

def _whoami():
    return dict(
        ok=True,
        service="upload_app",
        render_service=os.getenv("RENDER_SERVICE_ID") or os.getenv("RENDER_SERVICE", ""),
        port=os.getenv("PORT", "10000"),
        branch=os.getenv("RENDER_GIT_BRANCH", "unknown"),
        commit=os.getenv("RENDER_GIT_COMMIT", "local"),
        tag=(os.getenv("RENDER_GIT_COMMIT", "local")[:7]),
    )

# ---- Health & whoami ----
@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

@app.get("/__whoami")
def __whoami():
    return jsonify(_whoami())

# ---- Routes dump (open + locked) ----
@app.get("/__routes")
def __routes_open():
    routes = [
        dict(rule=r.rule,
             endpoint=r.endpoint,
             methods=sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}))
        for r in app.url_map.iter_rules()
    ]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def __routes_locked():
    if not _guard_ok():
        return Response("Forbidden", 403)
    return __routes_open()

# ---- /ops/db-ping ----
@app.get("/ops/db-ping")
def ops_db_ping():
    if not _guard_ok():
        return Response("Forbidden", 403)
    try:
        now = db_now()  # raises if DATABASE_URL not set
        return jsonify({"ok": True, "now_utc": str(now)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---- Simple upload API (writes to /tmp/uploads) ----
@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    email = request.form.get("email", "")
    if not f:
        return jsonify({"ok": False, "error": "file is required (form field 'file')"}), 400
    os.makedirs("/tmp/uploads", exist_ok=True)
    fname = secure_filename(f.filename) or "upload.bin"
    path = os.path.join("/tmp/uploads", fname)
    f.save(path)
    return jsonify({"ok": True, "filename": fname, "path": path, "email": email})

# ---- Mount UI blueprint (admin) at /upload ----
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# ---- Final route dump to logs at boot ----
print("=== ROUTES (final) ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    meth = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:30s} -> {r.endpoint:20s} [{meth}]")
print("=== END ROUTES ===")
