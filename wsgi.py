import os

try:
    from upload_app import app as _real_app
    app = _real_app
    USING_FALLBACK = False
except Exception as e:
    from flask import Flask, jsonify
    app = Flask(__name__)
    USING_FALLBACK = True
    _IMPORT_ERR = str(e)

    @app.get("/")
    def _fallback_root():
        return jsonify({
            "ok": False, "fallback": True,
            "reason": "Failed to import upload_app.app",
            "error": _IMPORT_ERR,
        }), 500

@app.get("/__alive")
def __alive():
    return {"ok": True, "fallback": USING_FALLBACK, "from": "wsgi.py", "routes": len(list(app.url_map.iter_rules()))}

@app.get("/__routes")
def __routes():
    rules = []
    for r in app.url_map.iter_rules():
        methods = sorted(m for m in r.methods if m in {"GET","POST","PUT","DELETE","PATCH","OPTIONS"})
        rules.append({"rule": r.rule, "endpoint": r.endpoint, "methods": methods})
    rules.sort(key=lambda x: x["rule"])
    return {"ok": True, "count": len(rules), "routes": rules}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
