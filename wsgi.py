# wsgi.py
import os
from upload_app import app

if __name__ == "__main__":
    # Render sets $PORT; default to 10000 locally
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), debug=False)
