# --- HARD GUARANTEE: make sure /healthz exists on the final app object ---
from flask import jsonify  # if not already imported

if not any(r.rule == "/healthz" for r in app.url_map.iter_rules()):
    @app.get("/healthz")
    def healthz():
        return "OK", 200

# Helpful one-time route dump in logs so we can verify what's actually mounted
try:
    print("=== ROUTES (final) ===")
    for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
        methods = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
        print(f"{r.rule:24s} -> {r.endpoint:20s} [{methods}]")
    print("=== END ROUTES ===")
except Exception as _e:
    print("route dump failed:", _e)
