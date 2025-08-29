import os
from upload_app import app

if __name__ == "__main__":
    # Render exposes PORT=10000. Keep debug off.
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
