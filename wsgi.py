# wsgi.py
import os
from upload_app import app  # upload_app already mounts the UI blueprint

@app.get("/__wsgi_ping__")
def __wsgi_ping__():
    return {"ok": True, "app_file": __file__, "routes": len(list(app.url_map.iter_rules()))}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
