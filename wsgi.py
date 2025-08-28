# wsgi.py
import os
from upload_app import app as backend_app   # imports the real Flask app

# expose the app object Render will run
app = backend_app

# --- PROBES ADDED HERE, guaranteed to register ---
@app.get("/__alive")
def _alive():
    return {
        "ok": True,
        "from": "wsgi.py",
        "rules": len(list(app.url_map.iter_rules()))
    }

@app.get("/__routes_open")
def _routes_open():
    rules = []
    for r in app.url_map.iter_rules():
        methods = sorted(m for m in r.methods if m in {"GET","POST","PUT","DELETE","PATCH","OPTIONS"})
        rules.append({"rule": r.rule, "endpoint": r.endpoint, "methods": methods})
    rules.sort(key=lambda x: x["rule"])
    return {"ok": True, "count": len(rules), "routes": rules}
# -------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
