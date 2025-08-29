# upload_app.py
import os
from flask import Flask, jsonify

COMMIT = os.getenv("RENDER_GIT_COMMIT", "local")[:7]
print("=== BOOT upload_app ===")
print("  __file__:", __file__)
print("  commit  :", COMMIT)

app = Flask(__name__)

# --- Healthz first (the thing Render probes) ---
@app.get("/healthz")
def healthz():
    return "OK", 200

# --- Root: helps Render's HEAD / health check too ---
@app.get("/")
def root():
    return jsonify(ok=True, service="upload_app", commit=COMMIT)

# --- Routes inspector (for us) ---
@app.get("/__routes")
def routes():
    rules = sorted(
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}),
        }
        for r in app.url_map.iter_rules()
    )
    return jsonify(ok=True, commit=COMMIT, count=len(rules), routes=rules)

# Dump the final routes to logs so we can SEE what's live
try:
    print("=== ROUTES AT IMPORT ===")
    for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
        methods = ",".join(sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}))
        print(f"  {r.rule:20s}  -> {r.endpoint:20s}  [{methods}]")
    print("=== END ROUTES ===")
except Exception as e:
    print("Route dump failed:", e)
