# wsgi.py
import os
from upload_app import app as backend_app          # the real Flask app
from probes import install as install_probes       # <-- new

# Force-install diagnostic routes on the exact app Render runs
install_probes(backend_app)

# Try to mount the UI blueprint if it isn't already
try:
    from ui_app import ui_bp
    if 'ui' not in backend_app.blueprints:
        backend_app.register_blueprint(ui_bp, url_prefix="/upload")
except Exception:
    pass

# Expose for Render / gunicorn
app = backend_app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
