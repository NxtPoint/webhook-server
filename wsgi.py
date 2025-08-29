# wsgi.py
import os
from upload_app import app

print("wsgi boot:", os.getenv("RENDER_GIT_COMMIT", "local")[:7])
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), debug=False)
