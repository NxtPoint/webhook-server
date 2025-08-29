# probes.py
from flask import jsonify

def install(app):
    @app.get("/__alive")
    def _alive():
        return jsonify(ok=True, where="probes.py")

    @app.get("/__routes")
    def _routes():
        rules = []
        for r in app.url_map.iter_rules():
            methods = sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})
            rules.append({"rule": r.rule, "endpoint": r.endpoint, "methods": methods})
        rules.sort(key=lambda x: x["rule"])
        return jsonify(ok=True, count=len(rules), routes=rules)
