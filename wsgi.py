import os
from upload_app import app

@app.get("/__ping")
def __ping():
    return {"ok": True, "routes": len(list(app.url_map.iter_rules()))}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))