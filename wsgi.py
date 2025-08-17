# wsgi.py
import os
from upload_app import app as backend_app   # your existing backend, unchanged
from ui_app import ui_bp                    # the UI blueprint

# Mount the UI under /upload
backend_app.register_blueprint(ui_bp, url_prefix="/upload")

# Expose as 'app' for Render or gunicorn
app = backend_app

if __name__ == "__main__":
    # lets you keep using a Python start command if you want
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
