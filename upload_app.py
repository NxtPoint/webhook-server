import os, sys
from flask import Flask, jsonify, request, Response

# Single Flask app (do not create another anywhere)
app = Flask(__name__, template_folder="templates", static_folder="static")

BOOT_TAG = os.getenv("RENDER_GIT_COMMIT", "local")[:7]

# ----- simple liveness -----
@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

# ----- ops guard (for /ops/*) -----
OPS_KEY = os.environ.get("OPS_KEY", "")
def _guard_ok() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    bearer = request.headers.get("Authorization", "")
    if bearer and bearer.lower().startswith("bearer "):
        bearer = bearer.split(" ", 1)[1].strip()
    hk = request.headers.get("X-OPS-Key") or bearer
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

# ----- route listings -----
@app.get("/__routes")
def __routes_open():
    routes = []
    for r in app.url_map.iter_rules():
        methods = sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})
        routes.append({"rule": r.rule, "endpoint": r.endpoint, "methods": methods})
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def __routes_locked():
    if not _guard_ok():
        return Response("Forbidden", 403)
    return __routes_open()

# ----- signature + diagnostics -----
@app.get("/__whoami")
def __whoami():
    return jsonify({
        "ok": True,
        "file": __file__,
        "cwd": os.getcwd(),
        "boot_tag": BOOT_TAG,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "first_sys_path": sys.path[0],
    })

# If you have ui_app later, you can re-enable; for now keep off to avoid side effects
# try:
#     from ui_app import ui_bp
#     app.register_blueprint(ui_bp, url_prefix="/upload")
#     print("Mounted ui_bp at /upload")
# except Exception as e:
#     print("ui_bp not mounted:", e)

# Print routes at boot (unbuffered mode will ensure this shows in logs)
print("=== ROUTES (startup) ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    methods = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:24s} -> {r.endpoint:20s} [{methods}]")
print("=== END ROUTES ===")
