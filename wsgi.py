import os
from upload_app import app

if __name__ == "__main__":
    # Render provides PORT (often 10000). Do not hard-code.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
