# wsgi.py
import os
from upload_app import app

@app.get("/__alive_wsgi")
def __alive_wsgi():
    return {"ok": True, "from": "wsgi.py", "routes": len(list(app.url_map.iter_rules()))}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
