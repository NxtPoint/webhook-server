# --- put these lines at the very top of upload_app.py ---
import os
from flask import Flask, jsonify

COMMIT = os.getenv("RENDER_GIT_COMMIT", "local")[:7]

# if something else hasn't created 'app' yet, create it
try:
    app  # noqa
except NameError:
    app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "OK", 200

@app.get("/__routes")
def __routes():
    rules = sorted(
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}),
        }
        for r in app.url_map.iter_rules()
    )
    return jsonify(ok=True, commit=COMMIT, count=len(rules), routes=rules)

print("=== ROUTES AT IMPORT ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    methods = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"  {r.rule:20s} -> {r.endpoint:20s} [{methods}]")
print("=== END ROUTES ===")
# --- rest of your file can follow as-is ---
