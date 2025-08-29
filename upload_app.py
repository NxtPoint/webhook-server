# upload_app.py
import os, json, re, uuid, pathlib, hashlib
from datetime import datetime, timezone, timedelta

import requests
from flask import (
    Flask, request, jsonify, Response, make_response,
    render_template, render_template_string, send_from_directory
)

# If you have a db_init.py with an SQLAlchemy engine, keep this import.
# If not, you can safely comment it out and set INGEST_RESULTS=0 in Render.
try:
    from db_init import engine
except Exception:
    engine = None

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------
DATABASE_URL   = os.environ.get("DATABASE_URL")  # optional for pure upload flow
OPS_KEY        = os.environ.get("OPS_KEY", "270fb80a747d459eafded0ae67b9b8f6")

ENABLE_CORS    = (os.environ.get("ENABLE_CORS", "0").strip().lower() in ("1","true","yes","y"))
MAX_UPLOAD_MB  = int(os.environ.get("MAX_UPLOAD_MB", "200"))

# Dropbox
DROPBOX_ACCESS_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
DROPBOX_TARGET_FOLDER= os.environ.get("DROPBOX_TARGET_FOLDER") or os.environ.get("DBX_TARGET_FOLDER") or "/uploads"

# SportAI
SPORT_AI_TOKEN        = os.environ.get("SPORT_AI_TOKEN", "")
SPORTAI_API_BASE      = os.environ.get("SPORTAI_API_BASE", "").rstrip("/")
SPORTAI_CREATE_URL    = os.environ.get("SPORT_AI_CREATE_URL") or (f"{SPORTAI_API_BASE}/v1/tasks" if SPORTAI_API_BASE else None)
SPORTAI_STATUS_URL_TPL= os.environ.get("SPORT_AI_STATUS_URL_TEMPLATE") or (f"{SPORTAI_API_BASE}/v1/tasks/{{task_id}}" if SPORTAI_API_BASE else None)
SPORT_AI_RESULT_FIELD = os.environ.get("SPORT_AI_RESULT_FIELD", "result_json_url")

# Public base for webhook callback (used when creating SportAI task)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Optional minimal ingestion when a final JSON URL is present
INGEST_RESULTS = (os.environ.get("INGEST_RESULTS", "0").strip().lower() in ("1","true","yes","y"))

# ------------------------------------------------------------------------------
# Flask app (ONE instance only)
# ------------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# ------------------------------------------------------------------------------
# CORS helper
# ------------------------------------------------------------------------------
@app.after_request
def _maybe_cors(resp):
    if ENABLE_CORS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-OPS-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

# ------------------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------------------
def _guard():
    qk  = request.args.get("key") or request.args.get("ops_key")
    hk  = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth= request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return supplied == OPS_KEY

def _forbid(): return Response("Forbidden", status=403)

def _float(v):
    if v is None: return None
    try: return float(v)
    except Exception:
        try: return float(str(v))
        except Exception: return None

def seconds_to_ts(base_dt, s):
    if s is None: return None
    try: return base_dt + timedelta(seconds=float(s))
    except Exception: return None

# ------------------------------------------------------------------------------
# Diagnostics
# ------------------------------------------------------------------------------
@app.get("/__alive")
def __alive():
    return jsonify({
        "ok": True,
        "service": "NextPoint Upload/Ingester (upload focus)",
        "routes": len(list(app.url_map.iter_rules())),
        "now_utc": datetime.utcnow().isoformat() + "Z"
    })

@app.get("/__routes")
def __routes():
    routes = sorted(
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})
        }
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

# simple JSON root
@app.get("/")
def root():
    return jsonify(service="NextPoint Upload/Ingester", status="ok", see=["/upload", "/__routes"])

# ------------------------------------------------------------------------------
# Upload UI + static
# ------------------------------------------------------------------------------
UPLOAD_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><title>Upload Match Video</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  html,body{margin:0;font-family:system-ui,Segoe UI,Arial,sans-serif;
    background:
      radial-gradient(1200px 600px at 60% -10%, #0ea5e9 0%, transparent 60%),
      radial-gradient(1000px 500px at -20% 10%, #22c55e 0%, transparent 55%),
      #0b1220 no-repeat center center fixed;
    background-size:cover;color:#fff}
  .overlay{background:rgba(0,0,0,.55);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}
  .card{width:100%;max-width:560px;background:rgba(16,185,129,.18);border:2px solid #22c55e;border-radius:16px;box-shadow:0 0 24px #22c55e}
  .pad{padding:22px}
  h2{margin:0 0 10px 0}
  input[type=email],input[type=file]{width:100%;padding:12px;margin:10px 0;border-radius:10px;border:none;font-size:1rem;background:#fff;color:#0b1220}
  button{background:#22c55e;color:#000;padding:12px 20px;border:none;border-radius:10px;font-size:1rem;cursor:pointer}
  .pill{display:inline-block;padding:6px 10px;border-radius:999px;border:1px solid #22c55e;background:rgba(0,0,0,.25);font-size:.85rem;margin-right:8px}
  .warn{color:#fca5a5;border-color:#fda4af}
  .progress{height:10px;background:#ffffff40;border-radius:8px;overflow:hidden;margin-top:10px}
  .fill{height:100%;width:0%;background:#22c55e;transition:width .25s}
  #status{margin-top:10px;white-space:pre-wrap;font-size:.95rem}
  code{background:rgba(0,0,0,.35);padding:6px 8px;border-radius:8px;border:1px solid #22c55e;display:block}
</style>
</head>
<body>
  <div class="overlay">
    <div class="card">
      <div class="pad">
        <h2>🎾 Upload Match Video</h2>

        <div>
          <span class="pill">Target: <b>{{target_folder}}</b></span>
          {% if not dropbox_ready %}<span class="pill warn">Dropbox not configured</span>{% endif %}
          {% if not sportai_ready %}<span class="pill warn">SportAI not configured</span>{% endif %}
          <span class="pill">Limit: {{max_mb}}MB</span>
        </div>

        <form id="f" enctype="multipart/form-data">
          <input type="email" name="email" placeholder="Your email" required/>
          <input type="file" name="video" accept=".mp4,.mov,.m4v" required/>
          <button type="submit">Upload & Analyze</button>
        </form>

        <div class="progress"><div id="p" class="fill"></div></div>
        <div id="status"></div>
        <div id="subs"></div>
      </div>
    </div>
  </div>

<script>
const form = document.getElementById('f');
const statusEl = document.getElementById('status');
const subsEl = document.getElementById('subs');
const fill = document.getElementById('p');

function setP(p){ fill.style.width = p + '%'; }
function log(m){ statusEl.textContent += (statusEl.textContent?'\\n':'') + m; }
async function readJson(res){ const t = await res.text(); try{ return JSON.parse(t) }catch{ return {_raw:t} } }

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  statusEl.textContent = ''; subsEl.textContent = ''; setP(5);
  try{
    const fd = new FormData(form);
    const f = form.querySelector('input[type=file]').files[0];
    if(!f){ log('❌ Pick a file'); setP(0); return }
    if(f.size > ({{max_mb}} * 1024 * 1024)){ log('❌ File too large'); setP(0); return }

    log('🚀 Uploading...');
    const res = await fetch('/upload', { method:'POST', body:fd });
    const data = await readJson(res);
    if(!res.ok || data.ok===false){ log('❌ ' + (data.error || (res.status+' '+res.statusText))); setP(0); return }

    log('✅ Registered task: ' + (data.task_id || data.sportai_task_id));
    if(data.dropbox_url) log('📎 Source: ' + data.dropbox_url);
    setP(35);
    await poll(data.task_id || data.sportai_task_id);
  }catch(err){
    log('❌ ' + String(err));
    setP(0);
  }
});

async function poll(taskId){
  let tries=0, max=140; // ~12min @ 5s
  while(tries++ < max){
    const r = await fetch('/upload/task_status/'+taskId);
    const j = await readJson(r);
    const node = j?.data?.data || j?.data;  // tolerate both shapes
    const st = node?.task_status || 'unknown';
    const pct = Math.max(0, Math.min(100, Math.round((node?.task_progress||0)*100)));
    setP(pct);
    if(node?.subtask_progress) subsEl.innerHTML = '<code>' + JSON.stringify(node.subtask_progress, null, 2) + '</code>';

    if(st==='completed'){ setP(100); log('✅ Analysis complete.'); return }
    if(st==='failed'){ log('❌ Task failed'); return }
    log('🔄 ' + st + ' ('+pct+'%)');
    await new Promise(x=>setTimeout(x, 5000));
  }
  log('⚠️ Timeout while waiting for completion.');
}
</script>
</body></html>
"""

def _render_upload_html():
    dropbox_ready = bool(DROPBOX_ACCESS_TOKEN)
    sportai_ready = bool(SPORT_AI_TOKEN and (SPORTAI_CREATE_URL or SPORTAI_API_BASE))
    target_folder = DROPBOX_TARGET_FOLDER or "/uploads"
    try:
        return render_template(
            "upload.html",
            dropbox_ready=dropbox_ready,
            sportai_ready=sportai_ready,
            target_folder=target_folder,
            max_upload_mb=MAX_UPLOAD_MB,
        )
    except Exception:
        # Safe inline fallback if template missing
        return Response(
            render_template_string(
                UPLOAD_HTML,
                dropbox_ready=dropbox_ready,
                sportai_ready=sportai_ready,
                target_folder=target_folder,
                max_mb=MAX_UPLOAD_MB
            ),
            mimetype="text/html"
        )

@app.get("/upload/")
def upload_home():
    return _render_upload_html()

@app.get("/upload/static/<path:filename>")
def upload_static(filename):
    base = os.path.join(app.root_path, "static", "upload")
    return send_from_directory(base, filename)

@app.get("/upload/test")
def upload_test():
    return Response("upload ok", mimetype="text/plain")

@app.get("/upload/health")
def upload_health():
    """Light DB ping (optional)."""
    if not engine:
        return jsonify({"ok": True, "db": "disabled"})
    try:
        with engine.connect() as conn:
            conn.execute("SELECT 1")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ------------------------------------------------------------------------------
# Dropbox helpers
# ------------------------------------------------------------------------------
def _dbx_upload_bytes(path_in_dbx: str, blob: bytes) -> dict:
    h = {
        "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
        "Content-Type": "application/octet-stream",
        "Dropbox-API-Arg": json.dumps({
            "path": path_in_dbx,
            "mode": "add", "autorename": True, "mute": False
        })
    }
    r = requests.post("https://content.dropboxapi.com/2/files/upload", headers=h, data=blob, timeout=300)
    r.raise_for_status()
    return r.json()

def _dbx_shared_link(path_in_dbx: str) -> str:
    h = {"Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}", "Content-Type": "application/json"}
    d = {"path": path_in_dbx, "settings": {"requested_visibility": "public"}}
    r = requests.post("https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings", headers=h, json=d, timeout=60)
    if r.status_code == 409:
        rr = requests.post("https://api.dropboxapi.com/2/sharing/list_shared_links", headers=h, json={"path": path_in_dbx}, timeout=60)
        rr.raise_for_status()
        links = rr.json().get("links", [])
        if not links:
            raise RuntimeError("Dropbox: no shared link")
        url = links[0]["url"]
    else:
        r.raise_for_status()
        url = r.json()["url"]
    # force direct download
    if url.endswith("?dl=0"): url = url[:-5]
    if not url.endswith("?dl=1"): url += "?dl=1"
    return url

# ------------------------------------------------------------------------------
# Upload -> Dropbox -> SportAI
# ------------------------------------------------------------------------------
@app.post("/upload")
def upload_post():
    try:
        f = request.files.get("video")
        email = request.form.get("email", "").strip()
        if not f or not f.filename:
            return jsonify({"ok": False, "error": "no file"}), 400
        if not DROPBOX_ACCESS_TOKEN:
            return jsonify({"ok": False, "error": "Dropbox not configured"}), 500
        if not SPORT_AI_TOKEN or not (SPORTAI_CREATE_URL or SPORTAI_API_BASE):
            return jsonify({"ok": False, "error": "SportAI not configured"}), 500

        blob = f.read()
        if MAX_UPLOAD_MB and len(blob) > MAX_UPLOAD_MB * 1024 * 1024:
            return jsonify({"ok": False, "error": f"file too large (> {MAX_UPLOAD_MB}MB)"}), 400

        # path in dropbox
        today = datetime.utcnow().strftime("%Y/%m/%d")
        name  = f"{uuid.uuid4().hex}_{re.sub(r'[^A-Za-z0-9._-]+','_',f.filename)}"
        dbx_path = f"{DROPBOX_TARGET_FOLDER.rstrip('/')}/{today}/{name}"

        up = _dbx_upload_bytes(dbx_path, blob)
        src_url = _dbx_shared_link(up["path_display"])

        # create task with SportAI
        create_url = SPORTAI_CREATE_URL or f"{SPORTAI_API_BASE}/v1/tasks"
        headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "source_url": src_url,
            "metadata": {"email": email} if email else {},
        }
        if PUBLIC_BASE_URL:
            payload["webhook_url"] = f"{PUBLIC_BASE_URL}/ops/sportai-callback?key={OPS_KEY}"

        r = requests.post(create_url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        j = r.json()
        task_id = j.get("task_id") or j.get("id") or j.get("data", {}).get("task_id")

        return jsonify({"ok": True, "task_id": task_id, "dropbox_url": src_url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/upload/task_status/<task_id>")
def upload_task_status(task_id):
    try:
        if not (SPORTAI_STATUS_URL_TPL or SPORTAI_API_BASE):
            return jsonify({"ok": False, "error": "SportAI status not configured"}), 500

        status_url = (SPORTAI_STATUS_URL_TPL or f"{SPORTAI_API_BASE}/v1/tasks/{{task_id}}").format(task_id=task_id)
        headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
        r = requests.get(status_url, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()

        # Optional: minimal ingestion of final JSON when present
        ingested = None
        if INGEST_RESULTS and engine:
            node = data.get("data", data) if isinstance(data, dict) else {}
            json_url = node.get(SPORT_AI_RESULT_FIELD)
            if json_url:
                try:
                    payload = requests.get(json_url, timeout=180).json()
                    # Store only a RAW snapshot to raw_result (minimal, safe)
                    session_uid = payload.get("session_uid") or payload.get("uid") or payload.get("id") or hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
                    with engine.begin() as conn:
                        conn.execute(
                            "INSERT INTO dim_session (session_uid) VALUES (%s) ON CONFLICT (session_uid) DO NOTHING",
                            (session_uid,)
                        )
                        sid = conn.execute("SELECT session_id FROM dim_session WHERE session_uid=%s", (session_uid,)).scalar()
                        conn.execute(
                            "INSERT INTO raw_result (session_id, payload_json, created_at) VALUES (%s, %s::jsonb, now() at time zone 'utc')",
                            (sid, json.dumps(payload))
                        )
                    ingested = {"session_uid": session_uid}
                except Exception as ie:
                    ingested = {"error": str(ie)}

        return jsonify({"ok": True, "data": data if isinstance(data, dict) else {"_raw": str(data)}, "ingested": ingested})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ------------------------------------------------------------------------------
# OPS
# ------------------------------------------------------------------------------
@app.get("/ops/routes")
def ops_routes():
    if not _guard():
        return _forbid()
    routes = sorted(
        {"rule": r.rule, "endpoint": r.endpoint, "methods": sorted(r.methods)}
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/db-ping")
def db_ping():
    if not _guard():
        return _forbid()
    if not engine:
        return jsonify({"ok": False, "error": "DB disabled"}), 500
    with engine.connect() as conn:
        now = conn.execute("SELECT now() AT TIME ZONE 'utc'").scalar()
    return jsonify({"ok": True, "now_utc": str(now)})

# ------------------------------------------------------------------------------
# main (used only for local dev; Render runs wsgi.py)
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
