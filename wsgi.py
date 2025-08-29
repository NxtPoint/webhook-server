# wsgi.py
import os
from upload_app import app as application  # for Gunicorn
app = application

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
