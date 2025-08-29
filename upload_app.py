# upload_app.py
import os
from flask import Flask, jsonify, request, Response

# one (and only one) Flask() in this file
app = Flask(__name__, template_folder="templates", static_folder="static")

# tag to confirm which commit is running (shows in /__whoami and logs)
BOOT_TAG = os.getenv("RENDER_GIT_COMMIT", "local")[:7]

# --- OPS key guard ---
OPS_KEY = os.environ.get("OPS_KEY", "")

def _guard_ok() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        bearer = bearer.split(" ", 1)[1].strip()
    hk = request.headers.get("X-OPS-Key") or bearer
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

# --- ultra-fast health + root (what Render can probe) ---
@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

# --- self-ident + quick sanity ---
@app.get("/__whoami")
def __whoami():
    rules = sorted(r.rule for r in app.url_map.iter_rules())
    return jsonify({
        "ok": True,
        "tag": BOOT_TAG,
        "routes_count": len(rules),
        "has_ops_routes": ("/ops/routes" in rules),
        "sample": rules[:20],  # first few routes for eyeballing
    })

# --- open routes dump (handy while debugging; remove later if you want) ---
@app.get("/__routes")
def __routes_open():
    routes = []
    for r in app.url_map.iter_rules():
        methods = sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})
        routes.append({"rule": r.rule, "endpoint": r.endpoint, "methods": methods})
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# --- locked routes dump (requires OPS_KEY, use ?key=...) ---
@app.get("/ops/routes")
def ops_routes():
    if not _guard_ok():
        return Response("Forbidden", 403)
    routes = []
    for r in app.url_map.iter_rules():
        methods = sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})
        routes.append({"rule": r.rule, "endpoint": r.endpoint, "methods": methods})
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# --- mount UI blueprint (safe if missing) ---
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# final route dump to logs so we see exactly what is live
print(f"=== ROUTES (final) — tag {BOOT_TAG} ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    meth = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:30s} -> {r.endpoint:22s} [{meth}]")
print("=== END ROUTES ===")
