# upload_app.py — entrypoint (uploads + SportAI + status)
import os, json, time, socket, sys, inspect, hashlib
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, Response
from werkzeug.utils import secure_filename

# -------------------------------------------------------
# Flask app
# -------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.url_map.strict_slashes = False

@app.get("/ops/code-hash")
def ops_code_hash():
    if not _guard(): return _forbid()
    try:
        with open(__file__, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()[:16]
        src = inspect.getsource(sys.modules[__name__])
        idx = src.find("@app.route(\"/upload\", methods=[\"POST\", \"OPTIONS\"])")
        snippet = src[max(0, idx-80): idx+200] if idx != -1 else "alias not found in source"
        return jsonify({"ok": True, "file": __file__, "sha256_16": sha, "snippet": snippet})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# 150 MB is Dropbox /files/upload limit (bigger uses upload_session)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_MB", "150")) * 1024 * 1024

# -------------------------------------------------------
# Env / config
# -------------------------------------------------------
OPS_KEY = os.getenv("OPS_KEY", "")

DBX_APP_KEY     = os.getenv("DROPBOX_APP_KEY", "")
DBX_APP_SECRET  = os.getenv("DROPBOX_APP_SECRET", "")
DBX_REFRESH     = os.getenv("DROPBOX_REFRESH_TOKEN", "")
DBX_FOLDER      = os.getenv("DROPBOX_UPLOAD_FOLDER", "/wix-uploads").strip()
DBX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()  # legacy token (optional)

# ---------- SportAI config (updated) ----------
SPORTAI_BASE        = os.getenv("SPORT_AI_BASE", "https://api.sportai.com").strip().rstrip("/")
SPORTAI_SUBMIT_PATH = os.getenv("SPORT_AI_SUBMIT_PATH", "/api/statistics/tennis").strip()
SPORTAI_STATUS_PATH = os.getenv("SPORT_AI_STATUS_PATH", "/api/statistics/tennis/{task_id}/status").strip()
SPORTAI_TOKEN       = os.getenv("SPORT_AI_TOKEN", "").strip()
SPORTAI_CHECK_PATH  = os.getenv("SPORT_AI_CHECK_PATH",  "/api/videos/check").strip()
SPORTAI_CANCEL_PATH = os.getenv("SPORT_AI_CANCEL_PATH", "/api/tasks/{task_id}/cancel").strip()

# Replace behavior (env-backed default; backward-compat aliases)
DEFAULT_REPLACE_ON_INGEST = (
    os.getenv("INGEST_REPLACE_EXISTING")
    or os.getenv("DEFAULT_REPLACE_ON_INGEST")
    or os.getenv("STRICT_REINGEST")
    or "1"
).strip().lower() in ("1","true","yes","y")

AUTO_INGEST_ON_COMPLETE = os.getenv("AUTO_INGEST_ON_COMPLETE", "0").lower() in ("1","true","yes","y")

# Try both public hostnames
SPORTAI_BASES = list(dict.fromkeys([
    SPORTAI_BASE,
    "https://api.sportai.com",
    "https://api.sportai.app",
]))

# Submit can vary by tenant — prefer tennis path, then generic
SPORTAI_SUBMIT_PATHS = list(dict.fromkeys([
    SPORTAI_SUBMIT_PATH,
    "/api/statistics/tennis",
    "/api/statistics",
]))

# Status can also vary by tenant → try all
SPORTAI_STATUS_PATHS = list(dict.fromkeys([
    SPORTAI_STATUS_PATH,
    "/api/statistics/tennis/{task_id}/status",
    "/api/statistics/{task_id}/status",
    "/api/statistics/tennis/{task_id}",
    "/api/statistics/{task_id}",
    "/api/tasks/{task_id}",
]))

ENABLE_CORS = os.environ.get("ENABLE_CORS", "0").lower() in ("1","true","yes","y")

# DB engine (used by callback)
from db_init import engine  # noqa: E402
# ingest core + ops blueprint
from ingest_app import ingest_bp, ingest_result_v2  # noqa: E402
app.register_blueprint(ingest_bp, url_prefix="")    # mounts all /ops/* ingest routes

# ---------- S3 config ----------
AWS_REGION      = os.getenv("AWS_REGION", "").strip() or None
S3_BUCKET       = os.getenv("S3_BUCKET", "").strip()
S3_PREFIX       = (os.getenv("S3_PREFIX", "wix-uploads") or "wix-uploads").strip().strip("/")
S3_GET_EXPIRES  = int(os.getenv("S3_GET_EXPIRES", "604800"))  # 7 days default

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------
def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

def _forbid():
    return Response("Forbidden", 403)

@app.after_request
def _maybe_cors(resp):
    if ENABLE_CORS:
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-OPS-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

# ---------- S3 helpers ----------
def _s3_client():
    try:
        import boto3  # lazy import so boot works without it
    except Exception as e:
        raise RuntimeError(f"boto3 not installed: {e}")
    return boto3.client("s3", region_name=AWS_REGION)

def _s3_put_fileobj(fobj, key: str, content_type: str | None = None) -> dict:
    """Upload file-like object to S3 key; returns {'bucket','key','size'}."""
    cli = _s3_client()
    extra = {}
    if content_type:
        extra["ContentType"] = content_type
    cli.upload_fileobj(fobj, S3_BUCKET, key, ExtraArgs=extra or None)
    # size is only known from stream; use fobj.tell() if possible
    try:
        pos = fobj.tell()
    except Exception:
        pos = None
    return {"bucket": S3_BUCKET, "key": key, "size": pos}

def _s3_presigned_get_url(key: str, expires: int | None = None) -> str:
    cli = _s3_client()
    return cli.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=int(expires or S3_GET_EXPIRES),
    )

# ---------- Dropbox auth ----------
def _dbx_access_token():
    if not (DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH):
        return None, "Missing Dropbox env vars (DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN)"
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DBX_REFRESH,
            "client_id": DBX_APP_KEY,
            "client_secret": DBX_APP_SECRET,
        },
        timeout=30,
    )
    if r.ok:
        return (r.json() or {}).get("access_token"), None
    return None, f"{r.status_code}: {r.text}"

def _dbx_get_token():
    """Prefer legacy access token if present; otherwise mint from refresh token."""
    if DBX_ACCESS_TOKEN:
        return DBX_ACCESS_TOKEN, None
    return _dbx_access_token()

# ---------- Dropbox helpers ----------
def _dbx_create_or_fetch_shared_link(token: str, path: str) -> str:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers=h, json={"path": path, "settings": {"audience": "public", "access": "viewer"}}, timeout=30)
    if r.status_code == 409:
        r = requests.post(
            "https://api.dropboxapi.com/2/sharing/list_shared_links",
            headers=h, json={"path": path, "direct_only": True}, timeout=30)
        r.raise_for_status()
        links = (r.json() or {}).get("links", [])
        if not links:
            raise RuntimeError("No Dropbox shared link available")
        return links[0]["url"]
    r.raise_for_status()
    return (r.json() or {}).get("url")

def _force_direct_dropbox(url: str) -> str:
    try:
        p = urlparse(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        q["dl"] = "1"
        host = "dl.dropboxusercontent.com" if "dropbox.com" in p.netloc else p.netloc
        return urlunparse((p.scheme, host, p.path, p.params, urlencode(q), p.fragment))
    except Exception:
        return url

def _store_submission_context(task_id: str, email: str, meta: dict | None, video_url: str, share_url: str | None = None):
    """Persist submission context so we can link to task_id in reporting."""
    if not engine:
        return
    try:
        from sqlalchemy import text as sql_text
        ddl = """
        CREATE TABLE IF NOT EXISTS submission_context (
          task_id        TEXT PRIMARY KEY,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          email          TEXT,
          customer_name  TEXT,
          match_date     DATE,
          start_time     TEXT,
          location       TEXT,
          player_a_name  TEXT,
          player_b_name  TEXT,
          player_a_utr   TEXT,
          player_b_utr   TEXT,
          video_url      TEXT,
          share_url      TEXT,
          raw_meta       JSONB
        );
        """
        ins = """
        INSERT INTO submission_context (
          task_id, email, customer_name, match_date, start_time, location,
          player_a_name, player_b_name, player_a_utr, player_b_utr,
          video_url, share_url, raw_meta
        ) VALUES (
          :task_id, :email, :customer_name, :match_date, :start_time, :location,
          :player_a_name, :player_b_name, :player_a_utr, :player_b_utr,
          :video_url, :share_url, :raw_meta
        )
        ON CONFLICT (task_id) DO UPDATE SET
          email=EXCLUDED.email,
          customer_name=EXCLUDED.customer_name,
          match_date=EXCLUDED.match_date,
          start_time=EXCLUDED.start_time,
          location=EXCLUDED.location,
          player_a_name=EXCLUDED.player_a_name,
          player_b_name=EXCLUDED.player_b_name,
          player_a_utr=EXCLUDED.player_a_utr,
          player_b_utr=EXCLUDED.player_b_utr,
          video_url=EXCLUDED.video_url,
          share_url=EXCLUDED.share_url,
          raw_meta=EXCLUDED.raw_meta;
        """
        m = meta or {}
        with engine.begin() as conn:
            conn.execute(sql_text(ddl))
            conn.execute(sql_text(ins), {
                "task_id": task_id,
                "email": email,
                "customer_name": m.get("customer_name"),
                "match_date": m.get("match_date"),
                "start_time": m.get("start_time"),
                "location": m.get("location"),
                "player_a_name": m.get("player_a_name") or "Player A",
                "player_b_name": m.get("player_b_name") or "Player B",
                "player_a_utr": m.get("player_a_utr"),
                "player_b_utr": m.get("player_b_utr"),
                "video_url": video_url,
                "share_url": share_url,
                "raw_meta": json.dumps(m),
            })
    except Exception:
        pass

def _extract_meta_from_form(form) -> dict:
    return {
        "customer_name": (form.get("customer_name") or "").strip(),
        "match_date": (form.get("match_date") or "").strip(),
        "start_time": (form.get("start_time") or "").strip(),
        "location": (form.get("location") or "").strip(),
        "player_a_name": (form.get("player_a_name") or "").strip() or "Player A",
        "player_b_name": (form.get("player_b_name") or "").strip() or "Player B",
        "player_a_utr": (form.get("player_a_utr") or "").strip(),
        "player_b_utr": (form.get("player_b_utr") or "").strip(),
        "terms_accepted": (form.get("terms_accepted") in ("on","1","true","yes","y")),
    }

# ---------- SportAI ----------
def _iter_submit_endpoints():
    for base in SPORTAI_BASES:
        for path in SPORTAI_SUBMIT_PATHS:
            yield f"{base.rstrip('/')}/{path.lstrip('/')}"

def _iter_status_endpoints(task_id: str):
    for base in SPORTAI_BASES:
        for path in SPORTAI_STATUS_PATHS:
            yield f"{base.rstrip('/')}/{path.lstrip('/').format(task_id=task_id)}"

def _sportai_submit(video_url: str, email: str | None = None, meta: dict | None = None) -> str:
    """
    Submit a video to SportAI. Different deployments expect slightly different JSON
    shapes; we try a few safe variants until one succeeds (avoids 400/404/415/422).
    """
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")

    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"}

    # Build a few payload variants (ordered from richest -> leanest)
    base_min  = {"video_url": video_url, "version": "latest"}
    base_arr  = {"video_urls": [video_url], "version": "latest"}         # some deployments use an array
    with_email = {**base_min, **({"email": email} if email else {})}
    with_meta  = {**with_email, **({"metadata": meta} if meta else {})}  # only if accepted by the API

    payload_variants = [
        with_meta,
        with_email,
        base_min,
        base_arr,
        {"url": video_url, "version": "latest"},  # very old schema
    ]

    last_err = None

    for submit_url in _iter_submit_endpoints():             # try each base/path
        for payload in payload_variants:                    # try each payload shape
            try:
                r = requests.post(submit_url, headers=headers, json=payload, timeout=60)

                # “Wrong schema/path” class of errors -> try next payload/endpoint
                if r.status_code in (400, 404, 405, 415, 422):
                    last_err = f"{submit_url} -> {r.status_code}: {r.text}"
                    continue

                # Server hiccup -> move on to next endpoint
                if r.status_code >= 500:
                    last_err = f"{submit_url} -> {r.status_code}: {r.text}"
                    break

                r.raise_for_status()
                j = r.json() if r.content else {}
                task_id = j.get("task_id") or (j.get("data") or {}).get("task_id") or j.get("id")
                if not task_id:
                    last_err = f"{submit_url} -> no task_id in response: {j}"
                    continue
                return str(task_id)

            except Exception as e:
                last_err = f"{submit_url} with {list(payload.keys())} -> {e}"
                continue

    raise RuntimeError(f"SportAI submit failed across all endpoints: {last_err}")

def _sportai_status(task_id: str) -> dict:
    """Fetch status from SportAI and return normalized fields plus raw blob."""
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}

    last_err = None
    j = None
    for url in _iter_status_endpoints(task_id):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code >= 500:
                last_err = f"{url} -> {r.status_code}: {r.text}"
                continue
            r.raise_for_status()
            j = r.json() or {}
            break
        except Exception as e:
            last_err = str(e)
    if j is None:
        raise RuntimeError(f"SportAI status failed: {last_err}")

    d = j.get("data") or j
    status = d.get("status") or d.get("task_status")
    out = {
        "status": status,
        "result_url": d.get("result_url") or j.get("result_url"),
        "data": {
            "task_id": d.get("task_id"),
            "video_url": d.get("video_url"),
            "task_status": d.get("task_status") or d.get("status"),
            "task_progress": d.get("task_progress") or d.get("progress"),
            "total_subtask_progress": d.get("total_subtask_progress"),
            "subtask_progress": d.get("subtask_progress") or {},
        },
        "raw": j,
    }
    return out

def _sportai_check(video_url: str) -> dict:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    url = f"{SPORTAI_BASE.rstrip('/')}/{SPORTAI_CHECK_PATH.lstrip('/')}"
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"}
    payload = {"video_urls": [video_url], "version": "latest"}
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json() or {}

def _sportai_cancel(task_id: str) -> dict:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    url = f"{SPORTAI_BASE.rstrip('/')}/{SPORTAI_CANCEL_PATH.lstrip('/').format(task_id=task_id)}"
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}
    r = requests.post(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json() or {}

# -------------------------------------------------------
# Public endpoints
# -------------------------------------------------------
@app.get("/")
def root_ok():
    return jsonify({"service": "NextPoint Upload/Ingester v3", "ok": True})

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

@app.get("/__routes")
def __routes_open():
    routes = [
        {"rule": r.rule, "endpoint": r.endpoint,
         "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})}
        for r in app.url_map.iter_rules()
    ]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def __routes_locked():
    if not _guard(): return _forbid()
    return __routes_open()

@app.get("/upload/api/status")
def upload_status():
    return jsonify({
        "ok": True,
        "storage": "s3" if S3_BUCKET else "dropbox",
        "s3_ready": bool(S3_BUCKET),
        "s3_bucket": S3_BUCKET or None,
        "s3_prefix": S3_PREFIX or None,
        "dropbox_ready": bool(DBX_ACCESS_TOKEN or (DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH)),
        "sportai_ready": bool(SPORTAI_TOKEN),
        "target_folder": f"s3://{S3_BUCKET}/{S3_PREFIX}" if S3_BUCKET else DBX_FOLDER,
    })

@app.get("/ops/env")
def ops_env():
    if not _guard(): return _forbid()
    return jsonify({
        "ok": True,
        "SPORT_AI_BASE": SPORTAI_BASE,
        "SPORT_AI_SUBMIT_PATHS": SPORTAI_SUBMIT_PATHS,
        "SPORT_AI_STATUS_PATHS": SPORTAI_STATUS_PATHS,
        "has_TOKEN": bool(SPORTAI_TOKEN),
        "DBX_MODE": "access_token" if DBX_ACCESS_TOKEN else "refresh_flow",
        "DEFAULT_REPLACE_ON_INGEST": DEFAULT_REPLACE_ON_INGEST,
        "AUTO_INGEST_ON_COMPLETE": AUTO_INGEST_ON_COMPLETE,
    })

@app.get("/ops/ping-sportai")
def ops_ping_sportai():
    if not _guard(): return _forbid()
    tests = {}
    for u in _iter_submit_endpoints():
        try:
            r = requests.post(u, headers={
                "Authorization": f"Bearer {SPORTAI_TOKEN}",
                "Content-Type": "application/json",
            }, json={"_ping": True}, timeout=10)
            tests[u] = {"ok": True, "status": r.status_code}
        except Exception as e:
            tests[u] = {"ok": False, "error": str(e)}
    return jsonify({"ok": True, "tests": tests})

@app.get("/ops/net-test")
def ops_net_test():
    if not _guard(): return _forbid()
    out = {}
    for base in SPORTAI_BASES:
        try:
            u = f"{base}/"
            r = requests.get(u, timeout=10)
            out[base] = {"ok": r.ok, "code": r.status_code}
        except Exception as e:
            out[base] = {"ok": False, "error": str(e)}
    return jsonify({"ok": True, "tests": out})

@app.get("/ops/dropbox-auth-test")
def ops_dropbox_auth_test():
    if not _guard(): return _forbid()
    tok, err = _dbx_get_token()
    if not tok:
        return jsonify({"ok": False, "error": f"Dropbox auth failed: {err}"}), 500
    mode = "access_token" if DBX_ACCESS_TOKEN else "refresh_flow"
    return jsonify({"ok": True, "mode": mode, "access_token_last4": tok[-4:]})

@app.get("/ops/sportai-dns")
def ops_sportai_dns():
    if not _guard(): return _forbid()
    host = urlparse(SPORTAI_BASE).hostname
    try:
        ip = socket.gethostbyname(host)
        return jsonify({"ok": True, "host": host, "ip": ip})
    except Exception as e:
        return jsonify({"ok": False, "host": host, "error": str(e)}), 500

# -------------------------------------------------------
# Upload API (also accepts direct JSON with video_url)
# -------------------------------------------------------
@app.route("/upload/api/upload", methods=["POST", "OPTIONS"])
def api_upload_to_dropbox():
    if request.method == "OPTIONS":
        return ("", 204)

    # 1) If JSON with video_url is provided, skip Dropbox and submit directly
    if request.is_json:
        body = request.get_json(silent=True) or {}
        video_url = (body.get("video_url") or body.get("share_url") or "").strip()
        email = (body.get("email") or "").strip().lower()
        meta = body.get("meta") or body.get("metadata") or {}
        if video_url:
            try:
                task_id = _sportai_submit(video_url, email=email, meta=meta)
                _store_submission_context(task_id, email, meta, video_url, share_url=body.get("share_url"))
                return jsonify({"ok": True, "task_id": task_id, "video_url": video_url})
            except Exception as e:
                return jsonify({"ok": False, "error": f"SportAI submit failed: {e}"}), 502

    # 2) Multipart upload (preferred)
    f = request.files.get("file") or request.files.get("video")
    email = (request.form.get("email") or "").strip().lower()
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file provided."}), 400

    clean = secure_filename(f.filename)
    ts = int(time.time())

    # ======= S3 branch (preferred when configured) =======
    if S3_BUCKET:
        try:
            key = f"{S3_PREFIX}/{ts}_{clean}"
            # Ensure we read from start for accurate upload
            try:
                f.stream.seek(0)
            except Exception:
                pass
            meta_up = _s3_put_fileobj(f.stream, key, content_type=getattr(f, "mimetype", None))
            share_url = _s3_presigned_get_url(key)  # presigned GET
            video_url = share_url

            meta = _extract_meta_from_form(request.form)
            task_id = _sportai_submit(video_url, email=email, meta=meta)
            _store_submission_context(task_id, email, meta, video_url, share_url=share_url)

            return jsonify({
                "ok": True,
                "task_id": task_id,
                "share_url": share_url,
                "video_url": video_url,
                "upload": {"path": key, "size": meta_up.get("size"), "name": clean}
            })
        except Exception as e:
            return jsonify({"ok": False, "error": f"S3 upload/submit failed: {e}"}), 502

    # ======= Dropbox branch (legacy fallback) =======
    tok, err = _dbx_get_token()
    if not tok:
        return jsonify({"ok": False, "error": f"Dropbox auth failed: {err}"}), 500

    dest_path = f"{DBX_FOLDER.rstrip('/')}/{ts}_{clean}"
    headers = {
        "Authorization": f"Bearer {tok}",
        "Dropbox-API-Arg": json.dumps({"path": dest_path, "mode": "add", "autorename": True, "mute": False}),
        "Content-Type": "application/octet-stream",
    }
    up = requests.post("https://content.dropboxapi.com/2/files/upload",
                       headers=headers, data=f.read(), timeout=600)
    if not up.ok:
        return jsonify({"ok": False, "error": f"Dropbox upload failed: {up.status_code} {up.text}"}), 502
    meta_dbx = up.json()

    try:
        share_url = _dbx_create_or_fetch_shared_link(tok, meta_dbx.get("path_lower") or dest_path)
        video_url = _force_direct_dropbox(share_url)

        meta = _extract_meta_from_form(request.form)
        task_id = _sportai_submit(video_url, email=email, meta=meta)
        _store_submission_context(task_id, email, meta, video_url, share_url=share_url)
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"Upload ok, SportAI submit failed: {e}",
            "upload": {"path": meta_dbx.get("path_display") or dest_path,
                       "size": meta_dbx.get("size"),
                       "name": meta_dbx.get("name", clean)}
        }), 502

    return jsonify({
        "ok": True,
        "task_id": task_id,
        "share_url": share_url,
        "video_url": video_url,
        "upload": {"path": meta_dbx.get("path_display") or dest_path,
                   "size": meta_dbx.get("size"),
                   "name": meta_dbx.get("name", clean)}
    })

# Legacy /upload alias (keeps old front-end working)
@app.route("/upload", methods=["POST", "OPTIONS"])
def upload_alias():
    if request.method == "OPTIONS":
        return ("", 204)
    return api_upload_to_dropbox()

# Old front-end polls /upload/task_status/<task_id>
@app.get("/upload/task_status/<task_id>")
def task_status_legacy_path(task_id):
    try:
        return jsonify({"ok": True, **_sportai_status(task_id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

# Old front-end polls /upload/task_status?task_id=...
@app.get("/upload/task_status")
def task_status_legacy_qs():
    task_id = request.args.get("task_id", "")
    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400
    try:
        return jsonify({"ok": True, **_sportai_status(task_id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

# -------------------------------------------------------
# Task poll (with optional one-time auto-ingest)
# -------------------------------------------------------
@app.get("/upload/api/task-status")
def api_task_status():
    tid = request.args.get("task_id")
    if not tid:
        return jsonify({"ok": False, "error": "task_id required"}), 400
    try:
        out = _sportai_status(tid)

        if AUTO_INGEST_ON_COMPLETE:
            status = (out.get("status") or "").lower()
            result_url = out.get("result_url")
            if status in ("completed","done","success","succeeded") and result_url:
                from sqlalchemy import text as sql_text
                with engine.begin() as conn:
                    already = conn.execute(
                        sql_text("SELECT session_id FROM submission_context WHERE task_id=:t AND session_id IS NOT NULL"),
                        {"t": tid}
                    ).scalar()
                    if not already:
                        r = requests.get(result_url, timeout=120); r.raise_for_status()
                        payload = r.json()
                        res = ingest_result_v2(conn, payload, replace=DEFAULT_REPLACE_ON_INGEST, src_hint=result_url)
                        sid = res["session_id"]
                        conn.execute(
                            sql_text("UPDATE submission_context SET session_id=:sid WHERE task_id=:t"),
                            {"sid": sid, "t": tid}
                        )
                        conn.execute(sql_text("""
                            UPDATE dim_session
                               SET meta = COALESCE(meta, '{}'::jsonb)
                                        || jsonb_build_object('task_id', :tid)
                                        || jsonb_build_object(
                                             'submission_context',
                                             to_jsonb(sc) - 'task_id' - 'created_at' - 'session_id'
                                           )
                             FROM (
                               SELECT email, customer_name, match_date, start_time, location,
                                      player_a_name, player_b_name, player_a_utr, player_b_utr,
                                      video_url, share_url
                                 FROM submission_context
                                WHERE task_id = :tid
                                LIMIT 1
                             ) sc
                             WHERE dim_session.session_id = :sid
                        """), {"sid": sid, "tid": tid})
                        out["auto_ingested"] = True
                        out["session_id"] = sid

        return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

# -------------------------------------------------------
# SportAI → our callback → ingest
# -------------------------------------------------------
def _download_result_payload(task_id: str | None = None, result_url: str | None = None):
    """Return (payload_dict, src_url)."""
    if not (task_id or result_url):
        return None, None
    try:
        if not result_url and task_id:
            st = _sportai_status(task_id)
            result_url = st.get("result_url")
        if not result_url:
            return None, None
        r = requests.get(result_url, timeout=120)
        r.raise_for_status()
        return r.json(), result_url
    except Exception:
        return None, None

def _attach_submission_context(conn, task_id: str, session_id: int, session_uid: str | None):
    from sqlalchemy import text as sql_text
    conn.execute(sql_text("""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='submission_context' AND column_name='session_id'
          ) THEN
            ALTER TABLE submission_context ADD COLUMN session_id INT;
          END IF;
        END $$;
    """))
    row = conn.execute(sql_text("SELECT * FROM submission_context WHERE task_id=:t LIMIT 1"),
                       {"t": task_id}).mappings().first()
    if row:
        conn.execute(sql_text("UPDATE submission_context SET session_id=:sid WHERE task_id=:t"),
                     {"sid": session_id, "t": task_id})
        keep = ["email","customer_name","match_date","start_time","location",
                "player_a_name","player_b_name","player_a_utr","player_b_utr",
                "video_url","share_url"]
        sc = {k: row[k] for k in keep if k in row and row[k] is not None}
        conn.execute(sql_text("""
            UPDATE dim_session
               SET meta = COALESCE(meta, '{}'::jsonb)
                        || jsonb_build_object('task_id', :task_id)
                        || jsonb_build_object('submission_context', CAST(:sc AS JSONB))
             WHERE session_id = :sid
        """), {"sid": session_id, "task_id": task_id, "sc": json.dumps(sc)})

@app.post("/ops/sportai-callback")
def ops_sportai_callback():
    if not _guard(): return _forbid()
    try:
        body = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400

    rep_arg = request.args.get("replace")
    replace = DEFAULT_REPLACE_ON_INGEST if rep_arg is None else str(rep_arg).lower() in ("1","true","yes","y","on")
    forced_uid = request.args.get("session_uid") or (
        body.get("session_uid") or body.get("sessionId") or body.get("session_id") or body.get("uid") or body.get("id")
    )
    task_id    = body.get("task_id") or body.get("id")
    result_url = body.get("result_url") or (body.get("data") or {}).get("result_url")

    payload, src_hint = (None, None)
    if isinstance(body, dict) and any(k in body for k in ("players","swings","ball_positions","player_positions","ball_bounces","rallies")):
        payload, src_hint = body, "webhook:body"
    if payload is None:
        payload, src_hint = _download_result_payload(task_id=task_id, result_url=result_url)
    if payload is None:
        return jsonify({"ok": True, "ingested": False, "reason": "no payload/result_url yet", "task_id": task_id}), 200

    from sqlalchemy import text as sql_text
    try:
        with engine.begin() as conn:
            res = ingest_result_v2(conn, payload, replace=replace, forced_uid=forced_uid, src_hint=src_hint)
            sid = res.get("session_id")
            if task_id:
                _attach_submission_context(conn, task_id=task_id, session_id=sid, session_uid=res.get("session_uid"))
            counts = conn.execute(sql_text("""
                SELECT
                  (SELECT COUNT(*) FROM dim_rally            WHERE session_id=:sid),
                  (SELECT COUNT(*) FROM fact_bounce          WHERE session_id=:sid),
                  (SELECT COUNT(*) FROM fact_ball_position   WHERE session_id=:sid),
                  (SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid),
                  (SELECT COUNT(*) FROM fact_swing           WHERE session_id=:sid)
            """), {"sid": sid}).fetchone()
        return jsonify({"ok": True, "ingested": True,
                        "session_uid": res.get("session_uid"),
                        "session_id":  sid,
                        "task_id": task_id,
                        "bronze_counts": {"rallies": counts[0],
                                          "ball_bounces": counts[1],
                                          "ball_positions": counts[2],
                                          "player_positions": counts[3],
                                          "swings": counts[4]}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# -------------------------------------------------------
# UI blueprint (if present)
# -------------------------------------------------------
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# Boot log
print("=== ROUTES ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    meth = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:30s} -> {r.endpoint:24s} [{meth}]")
print("================")

@app.get("/upload/__which_app")
def upload_which_app():
    """App-level fallback diagnostic to locate templates/upload.html on disk."""
    try:
        search = getattr(app.jinja_loader, "searchpath", [])
    except Exception:
        search = []
    expected = os.path.join(os.path.dirname(__file__), "templates", "upload.html")
    exists = os.path.exists(expected)
    head = ""
    if exists:
        try:
            with open(expected, "r", encoding="utf-8") as f:
                head = f.read(600)
        except Exception as e:
            head = f"<read error: {e}>"
    return jsonify({
        "ok": True,
        "module": __file__,
        "searchpath": search,
        "expected_template_path": expected,
        "exists": exists,
        "head": head,
    })
