# upload_app.py
import os
from flask import Flask, jsonify

BOOT_TAG = os.getenv("DEPLOY_TAG", os.getenv("RENDER_GIT_COMMIT", "local")[:7])
print("=== BOOT upload_app (minimal) ===", BOOT_TAG)

app = Flask(__name__)

@app.get("/healthz")
def healthz():
    # EXACT path Render probes
    return "OK", 200

@app.get("/")
def root():
    return jsonify({"ok": True, "service": "NextPoint Upload (minimal)", "tag": BOOT_TAG})

@app.get("/__routes")
def routes():
    rules = [
        {"rule": r.rule, "endpoint": r.endpoint,
         "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})}
        for r in app.url_map.iter_rules()
    ]
    return jsonify({"ok": True, "count": len(rules), "routes": sorted(rules, key=lambda x: x["rule"])})
