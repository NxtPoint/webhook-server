# wsgi.py
import os
from upload_app import app as _app  # import the final app object from your code

# expose as 'app' for Render
app = _app

# guaranteed health check (added after the final app exists)
@app.get("/healthz")
def healthz():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
