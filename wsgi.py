# wsgi.py
import os
from upload_app import app as backend_app   # your existing backend
from ui_app import ui_bp                    # the UI blueprint

# Mount the UI under /upload
backend_app.register_blueprint(ui_bp, url_prefix="/upload")

# Expose as 'app' for Render or gunicorn
app = backend_app

# ---- GUARANTEED PROBES + ROUTE DUMP (diagnostics) ----
from flask import jsonify

@app.get("/__alive")
def _alive():
    return jsonify(ok=True, where="wsgi.py", app_name=app.name)

@app.get("/__routes")
def _routes():
    rules = []
    for r in app.url_map.iter_rules():
        methods = sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})
        rules.append({"rule": r.rule, "endpoint": r.endpoint, "methods": methods})
    rules.sort(key=lambda x: x["rule"])
    return jsonify(ok=True, count=len(rules), routes=rules)

# print routes to logs at startup
print("=== ROUTE DUMP (startup) ===")
for r in app.url_map.iter_rules():
    print(f"{r.rule} -> {r.endpoint} {sorted(r.methods)}")
print("=== END ROUTE DUMP ===")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
