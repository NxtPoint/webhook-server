# wsgi.py
import os
from upload_app import app

if __name__ == "__main__":
    # Render provides $PORT (usually 10000). Don't hardcode it.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
