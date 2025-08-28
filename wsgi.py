import os
from upload_app import app  # upload_app already registers the UI blueprint

# tiny probe so you can verify you're on the right app
@app.get("/__ping")
def __ping():
    return {"ok": True, "app_file": __file__, "routes": len(list(app.url_map.iter_rules()))}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
