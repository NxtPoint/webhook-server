# probes.py
import os
from flask import jsonify, request

OPS_KEY = os.environ.get("OPS_KEY", "").strip()

def _guard() -> bool:
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    return bool(OPS_KEY) and (hk or "").strip() == OPS_KEY

def install(app):
    @app.get("/__alive")
    def _alive():
        return jsonify(ok=True)

    @app.get("/__routes")
    def _routes():
        if not _guard():
            return jsonify(ok=False, error="forbidden"), 403
        rules = []
        for r in app.url_map.iter_rules():
            methods = sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})
            rules.append({"rule": r.rule, "endpoint": r.endpoint, "methods": methods})
        rules.sort(key=lambda x: x["rule"])
        return jsonify(ok=True, count=len(rules), routes=rules)
