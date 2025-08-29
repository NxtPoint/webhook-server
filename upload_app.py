# upload_app.py
import os
from flask import Flask, jsonify, render_template, Response, request, send_from_directory

app = Flask(__name__, template_folder="templates", static_folder="static")

# --------- config / guard ----------
OPS_KEY = os.getenv("OPS_KEY", "")  # set in Render > Environment

def _guard_ok() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("Authorization", "").replace("Bearer ", "")
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

# --------- health & diagnostics ----------
@app.get("/_alive")
@app.get("/__alive")   # alias so both work
def alive():
    return jsonify({"ok": True, "app": "upload_app", "routes": len(list(app.url_map.iter_rules()))})

@app.get("/__routes")
def routes_open():
    routes = sorted(
        {"rule": r.rule, "endpoint": r.endpoint, "methods": sorted(r.methods)}
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def routes_locked():
    if not _guard_ok():
        return Response("Forbidden", 403)
    routes = sorted(
        {"rule": r.rule, "endpoint": r.endpoint, "methods": sorted(r.methods)}
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# --------- UI: always serve /upload ----------
INLINE_UPLOAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Upload Match Video</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  html,body{margin:0;font-family:system-ui,Segoe UI,Arial,sans-serif;background:#0b1220;color:#fff}
  .overlay{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
  .card{max-width:560px;width:100%;background:rgba(34,197,94,.15);border:2px solid #22c55e;border-radius:16px;box-shadow:0 0 24px #22c55e}
  .pad{padding:22px}
  input[type=email],input[type=file]{width:100%;padding:12px;margin:10px 0;border-radius:10px;border:none;font-size:1rem}
  button{background:#22c55e;color:#000;padding:12px 20px;border:none;border-radius:10px;font-size:1rem;cursor:pointer}
  #status{margin-top:10px;white-space:pre-wrap}
</style></head>
<body>
  <div class="overlay"><div class="card"><div class="pad">
    <h2>🎾 Upload Match Video</h2>
    <form id="f" enctype="multipart/form-data">
      <input type="email" name="email" placeholder="Your email" required/>
      <input type="file" name="video" accept=".mp4,.mov,.m4v" required/>
      <button type="submit">Upload</button>
    </form>
    <div id="status"></div>
  </div></div></div>
  <script>
    const f=document.getElementById('f'), s=document.getElementById('status');
    f.addEventListener('submit', e => { e.preventDefault(); s.textContent="This is the UI shell. Upload API isn't wired yet."; });
  </script>
</body></html>"""

@app.get("/upload")
@app.get("/upload/")
def upload_page():
    # Try real template first; fall back to inline shell.
    try:
        return render_template("upload.html")
    except Exception:
        return Response(INLINE_UPLOAD, mimetype="text/html")

# Serve static assets placed in static/upload/*
@app.get("/upload/static/<path:filename>")
def upload_static(filename):
    base = os.path.join(app.root_path, "static", "upload")
    return send_from_directory(base, filename)

# Legacy alias just in case
@app.get("/")
def root():
    return jsonify({"service": "NextPoint Upload (minimal)", "ok": True, "see": ["/upload", "/__routes"]})

# Optional: mount UI blueprint if it exists; otherwise the fallback above still works.
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    app.logger.info("Mounted ui_bp at /upload")
except Exception as e:
    app.logger.warning("ui_bp not mounted: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
