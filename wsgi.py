# wsgi.py
import os
from upload_app import app

print("=== START wsgi.py ===")
print("  cwd    :", os.getcwd())
print("  commit :", os.getenv("RENDER_GIT_COMMIT", "local")[:7])
print("=== END wsgi.py ===")

if __name__ == "__main__":
    # Render injects PORT. Default to 10000 so local runs match Render.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), debug=False)
