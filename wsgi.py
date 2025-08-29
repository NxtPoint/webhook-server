# wsgi.py
import os

# Import the Flask app object from your main module
from upload_app import app as application  # "application" is the WSGI name some hosts prefer

# Also expose it as "app" (Render accepts either wsgi:app or wsgi:application)
app = application

if __name__ == "__main__":
    # Local/dev fallback; Render will pass PORT in the environment
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
