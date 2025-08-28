# wsgi.py
import os
from upload_app import app  # app already has the UI blueprint mounted

# Optional tiny probe to confirm which app is running
@app.get("/__wsgi_ping__")
def __wsgi_ping__():
    return {"ok": True, "app_file": __file__, "routes": len(list(app.url_map.iter_rules()))}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
