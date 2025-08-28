# wsgi.py
import os
from upload_app import app as backend_app

# Mount the UI blueprint once (no-op if it's already mounted inside upload_app)
try:
    from ui_app import ui_bp
    if 'ui' not in backend_app.blueprints:
        backend_app.register_blueprint(ui_bp, url_prefix="/upload")
except Exception:
    pass

# ---- DIAGNOSTIC ROUTES (top-level, no prefix) ----
@backend_app.get("/__alive")
def __alive():
    return {"ok": True, "pid": os.getpid()}

@backend_app.get("/__routes")
def __routes():
    # return every known rule so you can verify registration over the network
    rules = sorted(f"{r.rule} -> {','.join(sorted(r.methods - {'HEAD'}))}"
                   for r in backend_app.url_map.iter_rules())
    return {"count": len(rules), "rules": rules}

# Expose as 'app' for gunicorn/render
app = backend_app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
