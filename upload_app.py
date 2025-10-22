# upload_app.py — S3-only entrypoint (uploads + SportAI + status), webhook disabled, VIEW-only (no MV)
import os, json, time, socket, sys, inspect, hashlib, re, threading
from urllib.parse import urlparse
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, Response
from werkzeug.utils import secure_filename
from sqlalchemy import text as sql_text

# ---- boto3 is REQUIRED (fail fast if missing) ----
try:
    import boto3
except Exception as e:
    raise RuntimeError("boto3 is required. Add it to requirements.txt and redeploy.") from e

# -------------------------------------------------------
# Flask app
# -------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.url_map.strict_slashes = False
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_MB", "150")) * 1024 * 1024  # 150MB default

@app.get("/ops/code-hash")
def ops_code_hash():
    try:
        with open(__file__, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()[:16]
        src = inspect.getsource(sys.modules[__name__])
        idx = src.find("@app.route(\"/upload\", methods=[\"POST\", \"OPTIONS\"])")
        snippet = src[max(0, idx-80): idx+200] if idx != -1 else "alias not found in source"
        return jsonify({"ok": True, "file": __file__, "sha256_16": sha, "snippet": snippet})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# -------------------------------------------------------
# Env / config
# -------------------------------------------------------
OPS_KEY = os.getenv("OPS_KEY", "").strip()

# ---------- SportAI config ----------
SPORTAI_BASE         = os.getenv("SPORT_AI_BASE", "https://api.sportai.com").strip().rstrip("/")
SPORTAI_SUBMIT_PATH  = os.getenv("SPORT_AI_SUBMIT_PATH", "/api/statistics/tennis").strip()
SPORTAI_STATUS_PATH  = os.getenv("SPORT_AI_STATUS_PATH", "/api/statistics/tennis/{task_id}/status").strip()
SPORTAI_TOKEN        = os.getenv("SPORT_AI_TOKEN", "").strip()
SPORTAI_CHECK_PATH   = os.getenv("SPORT_AI_CHECK_PATH",  "/api/videos/check").strip()
SPORTAI_CANCEL_PATH  = os.getenv("SPORT_AI_CANCEL_PATH", "/api/tasks/{task_id}/cancel").strip()

AUTO_INGEST_ON_COMPLETE = os.getenv("AUTO_INGEST_ON_COMPLETE", "1").lower() in ("1","true","yes","y")
DEFAULT_REPLACE_ON_INGEST = (
    os.getenv("INGEST_REPLACE_EXISTING")
    or os.getenv("DEFAULT_REPLACE_ON_INGEST")
    or os.getenv("STRICT_REINGEST")
    or "1"
).strip().lower() in ("1","true","yes","y")
ENABLE_CORS = os.environ.get("ENABLE_CORS", "0").lower() in ("1","true","yes","y")

# Optional flag (default OFF). If ever set true, we’ll try to refresh ss_.mv_point.
USE_MV_POINT = os.getenv("USE_MV_POINT", "0").lower() in ("1","true","yes","y")

# Try both public hostnames
SPORTAI_BASES = list(dict.fromkeys([
    SPORTAI_BASE,
    "https://api.sportai.com",
    "https://api.sportai.app",
]))

SPORTAI_SUBMIT_PATHS = list(dict.fromkeys([SPORTAI_SUBMIT_PATH, "/api/statistics/tennis", "/api/statistics"]))
SPORTAI_STATUS_PATHS = list(dict.fromkeys([
    SPORTAI_STATUS_PATH,
    "/api/statistics/tennis/{task_id}/status",
    "/api/statistics/{task_id}/status",
    "/api/statistics/tennis/{task_id}",
    "/api/statistics/{task_id}",
    "/api/tasks/{task_id}",
]))

# ---------- DB engine / ingest blueprint ----------
from db_init import engine, ingest_all_for_session                      # << added ingest_all_for_session
from ingest_app import ingest_bp, ingest_result_v2                      # keep your ops routes as-is
app.register_blueprint(ingest_bp, url_prefix="")                        # mounts /ops/* ingest routes

# ---------- Raw payload persistence helpers (added, minimal) ----------
def _ensure_raw_result_schema(conn):
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS raw_result (
          raw_result_id   BIGSERIAL PRIMARY KEY,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          session_id      INT,
          session_uid     TEXT,
          doc_type        TEXT,
          source          TEXT,
          payload_json    JSONB,
          payload         JSONB,
          payload_gzip    BYTEA,
          payload_sha256  TEXT
        );
    """))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS raw_result_session_id_idx ON raw_result(session_id)"))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS raw_result_session_uid_idx ON raw_result(session_uid)"))

def _detect_session_uid(payload: dict):
    return payload.get("session_uid") or payload.get("sessionId") or payload.get("uid")

def _detect_session_id(payload: dict):
    sid = payload.get("session_id") or payload.get("sessionId")
    try:
        return int(sid) if sid is not None else None
    except Exception:
        return None

def _store_raw_payload(conn, *, payload_dict: dict, session_id=None, session_uid=None,
                       doc_type="sportai.result", source=None):
    blob = json.dumps(payload_dict, ensure_ascii=False)
    sha  = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    _ensure_raw_result_schema(conn)
    conn.execute(sql_text("""
        INSERT INTO raw_result (session_id, session_uid, doc_type, source, payload_json, payload_sha256)
        VALUES (:sid, :suid, :dt, :src, CAST(:payload AS jsonb), :sha)
        ON CONFLICT DO NOTHING
    """), {"sid": session_id, "suid": session_uid, "dt": doc_type, "src": source, "payload": blob, "sha": sha})

def _update_raw_result_session_id(conn, *, session_id: int, session_uid):
    if not session_uid:
        return
    conn.execute(sql_text("""
        UPDATE raw_result
           SET session_id = :sid
         WHERE session_id IS NULL
           AND session_uid = :suid
    """), {"sid": session_id, "suid": session_uid})

# ---------- S3 config (MANDATORY) ----------
AWS_REGION = os.getenv("AWS_REGION", "").strip() or None
S3_BUCKET  = os.getenv("S3_BUCKET", os.getenv("UPLOAD_S3_BUCKET", "")).strip() or None
S3_PREFIX  = (os.getenv("S3_PREFIX", os.getenv("UPLOAD_S3_PREFIX", "incoming")) or "incoming").strip().strip("/")
S3_GET_EXPIRES = int(os.getenv("S3_GET_EXPIRES", "604800"))  # 7d

def _require_s3():
    if not (AWS_REGION and S3_BUCKET):
        raise RuntimeError("S3 is required: set AWS_REGION and S3_BUCKET env vars")

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------
def _j(ok=True, **k):
    return jsonify({"ok": ok, **k})

def _err(msg, code=400, **k):
    return jsonify({"ok": False, "error": msg, **k}), code

def _now():
    return datetime.now(timezone.utc).isoformat()

# -------------------------------------------------------
# Health
# -------------------------------------------------------
@app.get("/health")
def health():
    return _j(service="sportai-api", ts=_now())

@app.get("/")
def root():
    return _j(service="sportai-api")

@app.get("/ops/whoami")
def whoami():
    return _j(host=socket.gethostname(), pid=os.getpid(), py=sys.version, file=__file__)

@app.get("/ops/ping")
def ops_ping():
    return _j(ts=_now())

# -------------------------------------------------------
# Guard + misc helpers
# -------------------------------------------------------
def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token and (token == OPS_KEY):
            return True
    if qk and (qk == OPS_KEY): return True
    if hk and (hk == OPS_KEY): return True
    return False

def _s3_client():
    _require_s3()
    return boto3.client("s3", region_name=AWS_REGION)

def _s3_put_fileobj(fileobj, key: str, content_type: str | None = None):
    cli = _s3_client()
    extra = {"ACL": "private"}
    if content_type:
        extra["ContentType"] = content_type
    cli.upload_fileobj(fileobj, S3_BUCKET, key, ExtraArgs=extra)
    return {"bucket": S3_BUCKET, "key": key}

def _s3_presigned_get_url(key: str, expires: int | None = None):
    cli = _s3_client()
    return cli.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=int(expires or S3_GET_EXPIRES),
    )

# ---------- submission_context schema ----------
def _ensure_submission_context_schema(conn):
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS submission_context (
          task_id         TEXT PRIMARY KEY,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          email           TEXT,
          customer_name   TEXT,
          match_date      DATE,
          start_time      TEXT,
          location        TEXT,
          player_a_name   TEXT,
          player_b_name   TEXT,
          player_a_utr    TEXT,
          player_b_utr    TEXT,
          video_url       TEXT,
          share_url       TEXT,
          raw_meta        JSONB,
          last_status     TEXT,
          last_status_at  TIMESTAMPTZ,
          last_result_url TEXT,
          session_id      INT,
          ingest_started_at  TIMESTAMPTZ,
          ingest_finished_at TIMESTAMPTZ,
          ingest_error    TEXT
        );
    """))
    for _, ddl in [
        ("session_id",          "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS session_id INT"),
        ("ingest_started_at",   "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_started_at TIMESTAMPTZ"),
        ("ingest_finished_at",  "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ"),
        ("ingest_error",        "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_error TEXT")
    ]:
        conn.execute(sql_text(ddl))

def _store_submission_context(task_id: str, email: str, meta: dict | None, video_url: str, share_url: str | None = None):
    if not engine: return
    try:
        m = meta or {}
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            conn.execute(sql_text("""
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
                  raw_meta=EXCLUDED.raw_meta
            """), {
                "task_id": task_id,
                "email": m.get("email") or "",
                "customer_name": m.get("customer_name") or m.get("name") or "",
                "match_date": m.get("match_date"),
                "start_time": m.get("start_time"),
                "location": m.get("location"),
                "player_a_name": m.get("player_a_name") or "",
                "player_b_name": m.get("player_b_name") or "",
                "player_a_utr": m.get("player_a_utr") or "",
                "player_b_utr": m.get("player_b_utr") or "",
                "video_url": video_url,
                "share_url": share_url or "",
                "raw_meta": json.dumps(m, ensure_ascii=False),
            })
    except Exception:
        pass

def _get_status_cache(conn, task_id: str):
    return conn.execute(sql_text("""
        SELECT task_id, last_status, last_status_at, last_result_url, session_id,
               ingest_started_at, ingest_finished_at, ingest_error
          FROM submission_context
         WHERE task_id = :t
         LIMIT 1
    """), {"t": task_id}).mappings().first()

def _set_status_cache(conn, task_id: str, status: str | None, result_url: str | None):
    conn.execute(sql_text("""
        UPDATE submission_context
           SET last_status     = :s,
               last_status_at  = now(),
               last_result_url = :r
         WHERE task_id = :t
    """), {"t": task_id, "s": status, "r": result_url})

# ---------- SportAI ----------
def _iter_submit_endpoints():
    for base in SPORTAI_BASES:
        for path in SPORTAI_SUBMIT_PATHS:
            yield f"{base.rstrip('/')}/{path.lstrip('/')}"

def _iter_status_endpoints(task_id: str):
    for base in SPORTAI_BASES:
        for path in SPORTAI_STATUS_PATHS:
            yield f"{base.rstrip('/')}/{path.lstrip('/').replace('{task_id}', task_id)}"

def _sportai_headers():
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    return {"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"}

def _sportai_check(url: str):
    # POST {video_url: ...} -> {ok:..., errors:[...]}
    for base in SPORTAI_BASES:
        u = f"{base.rstrip('/')}/{SPORTAI_CHECK_PATH.lstrip('/')}"
        try:
            r = requests.post(u, headers=_sportai_headers(), json={"video_url": url}, timeout=60)
            if r.status_code >= 400:
                continue
            obj = r.json()
            if isinstance(obj, dict) and not obj.get("errors"):
                return {"ok": True, "raw": obj}
            return {"ok": False, "raw": obj}
        except Exception:
            continue
    return {"ok": False, "raw": {"error": "check failed on all endpoints"}}

def _sportai_submit(url: str, metadata: dict | None = None):
    payload = {"video_url": url}
    if metadata and isinstance(metadata, dict):
        payload["metadata"] = metadata
    last_err = None
    for u in _iter_submit_endpoints():
        try:
            r = requests.post(u, headers=_sportai_headers(), json=payload, timeout=60)
            if r.status_code >= 400:
                last_err = f"{u}: {r.status_code} {r.text[:200]}"
                continue
            return r.json()
        except Exception as e:
            last_err = f"{u}: {e}"
            continue
    raise RuntimeError(last_err or "submit failed")

def _sportai_status(task_id: str):
    last_err = None
    for u in _iter_status_endpoints(task_id):
        try:
            r = requests.get(u, headers=_sportai_headers(), timeout=30)
            if r.status_code >= 400:
                last_err = f"{u}: {r.status_code} {r.text[:200]}"
                continue
            return r.json()
        except Exception as e:
            last_err = f"{u}: {e}"
            continue
    raise RuntimeError(last_err or "status failed")

def _sportai_cancel(task_id: str):
    for base in SPORTAI_BASES:
        u = f"{base.rstrip('/')}/{SPORTAI_CANCEL_PATH.lstrip('/').replace('{task_id}', task_id)}"
        try:
            r = requests.post(u, headers=_sportai_headers(), timeout=20)
            if r.status_code < 400:
                return True
        except Exception:
            continue
    return False

# ---------- S3 upload API ----------
@app.route("/upload/api/presign", methods=["POST", "OPTIONS"])
def api_presign():
    if request.method == "OPTIONS": return ("", 204)
    try:
        data = request.get_json(silent=True) or {}
        name = (data.get("fileName") or data.get("filename") or "").strip()
        ctype = (data.get("contentType") or data.get("mime") or "video/mp4").strip()
        if not name:
            return _err("fileName required")
        clean = secure_filename(name)
        key = f"{S3_PREFIX}/{int(time.time())}_{clean}"
        cli = _s3_client()
        post = cli.generate_presigned_post(
            Bucket=S3_BUCKET, Key=key,
            Fields={"Content-Type": ctype}, Conditions=[{"Content-Type": ctype}],
            ExpiresIn=600,
        )
        return jsonify({
            "ok": True, "bucket": S3_BUCKET, "key": key,
            "post": post, "get_url": _s3_presigned_get_url(key)
        })
    except Exception as e:
        return _err(f"presign failed: {e}", 500)

# ---------- Video check & cancel ----------
@app.route("/upload/api/check-video", methods=["POST", "OPTIONS"])
@app.route("/api/check-video", methods=["POST", "OPTIONS"])            # alias
@app.route("/upload/api/check", methods=["POST", "OPTIONS"])           # alias
@app.route("/upload/check", methods=["POST", "OPTIONS"])               # alias
def api_check_video():
    if request.method == "OPTIONS": return ("", 204)
    try:
        data = request.get_json(silent=True) or request.values
        video_url = (data.get("video_url") or data.get("url") or "").strip()
        if video_url:
            chk = _sportai_check(video_url)
            return jsonify({"ok": True, "video_url": video_url, "check": chk, "check_passed": bool(chk.get("ok"))})
        f = request.files.get("file") or request.files.get("video")
        if not f or not f.filename: return _err("No file provided.")
        clean = secure_filename(f.filename); ts = int(time.time())
        key = f"{S3_PREFIX}/{ts}_{clean}"
        try:
            f.stream.seek(0)
        except Exception:
            pass
        _ = _s3_put_fileobj(f.stream, key, content_type=getattr(f, "mimetype", None))
        video_url = _s3_presigned_get_url(key)
        chk = _sportai_check(video_url)
        return jsonify({"ok": True, "video_url": video_url, "check": chk, "check_passed": bool(chk.get("ok"))})
    except Exception as e:
        return _err(f"check failed: {e}", 500)

@app.post("/upload/api/cancel")
def api_cancel():
    tid = (request.get_json(silent=True) or {}).get("task_id")
    if not tid:
        return _err("task_id required")
    ok = _sportai_cancel(tid)
    return jsonify({"ok": ok})

# ---------- Submit (create task) ----------
@app.post("/upload/api/submit")
def api_submit():
    body = request.get_json(silent=False) or {}
    video_url = body.get("video_url")
    meta = body.get("metadata") or {}
    if not video_url:
        return _err("video_url required")
    try:
        # preflight check (as before)
        chk = _sportai_check(video_url)
        if not chk.get("ok"):
            return jsonify({"ok": False, "error": "video did not pass checks", "check": chk}), 400

        # submit
        job = _sportai_submit(video_url, metadata=meta)
        task_id = job.get("task_id") or job.get("id")
        if not task_id:
            return _err("missing task_id from SportAI", 502)

        # cache form context
        _store_submission_context(task_id, email=meta.get("email") or "", meta=meta, video_url=video_url)
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as e:
        return _err(f"submit failed: {e}", 502)

# ---------- Upload alias (kept) ----------
@app.route("/upload", methods=["POST", "OPTIONS"])
def upload_alias():
    if request.method == "OPTIONS": return ("", 204)
    return api_submit()

# ---------- BACKGROUND INGEST (patched core only) ----------
def _do_ingest(task_id: str, result_url: str):
    try:
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            # mark started
            conn.execute(sql_text("""
                UPDATE submission_context
                   SET ingest_started_at = COALESCE(ingest_started_at, now()),
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"t": task_id})

        # fetch result JSON (unchanged until here)
        r = requests.get(result_url, timeout=600)
        r.raise_for_status()
        payload = r.json()

        if not isinstance(payload, dict):
            raise RuntimeError("Result JSON is not an object")

        suid     = _detect_session_uid(payload)
        sid_hint = _detect_session_id(payload)

        with engine.begin() as conn:
            # 1) save raw payload so it never drops
            _store_raw_payload(conn,
                               payload_dict=payload,
                               session_id=sid_hint,
                               session_uid=suid,
                               doc_type="sportai.result",
                               source=result_url)

            # 2) build Bronze (all towers + dim/fact)
            summary = ingest_all_for_session(conn, sid_hint or -1, payload)
            sid = int(summary.get("session_id") or (sid_hint or -1))

            # 3) update linkage + finished stamp
            _update_raw_result_session_id(conn, session_id=sid, session_uid=suid)
            conn.execute(sql_text("""
                UPDATE submission_context
                   SET session_id = :sid,
                       ingest_finished_at = now(),
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"sid": sid, "t": task_id})

    except Exception as e:
        # capture error but don't crash the process
        with engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE submission_context
                   SET ingest_error = :err,
                       ingest_finished_at = now()
                 WHERE task_id = :t
            """), {"t": task_id, "err": f"{e.__class__.__name__}: {e}"})

def _start_ingest_background(task_id: str, result_url: str) -> bool:
    """Return True if we started a worker; False if already ingested/started."""
    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        row = conn.execute(sql_text("""
            SELECT session_id, ingest_started_at, ingest_finished_at
              FROM submission_context
             WHERE task_id = :t
             LIMIT 1
        """), {"t": task_id}).mappings().first()

        if row and row.get("session_id"):
            return False  # already done
        if row and row.get("ingest_started_at") and not row.get("ingest_finished_at"):
            return False  # already running

        conn.execute(sql_text("""
            UPDATE submission_context
               SET ingest_started_at = now(),
                   ingest_error = NULL
             WHERE task_id = :t
        """), {"t": task_id})

    th = threading.Thread(target=_do_ingest, args=(task_id, result_url), daemon=True)
    th.start()
    return True

# ---------- Task poll (normalized progress + auto-ingest; no MV refresh) ----------
@app.get("/upload/api/task-status")
def api_task_status():
    tid = request.args.get("task_id")
    if not tid:
        return _err("task_id required")

    try:
        status_obj = _sportai_status(tid)  # unchanged
        status = (status_obj.get("status") or status_obj.get("state") or "").lower()
        result_url = (
            status_obj.get("result_url") or
            status_obj.get("result") or
            (status_obj.get("links") or {}).get("result")
        )

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            _set_status_cache(conn, tid, status, result_url)
            sc = conn.execute(sql_text("""
                SELECT session_id, ingest_started_at, ingest_finished_at, ingest_error
                  FROM submission_context
                 WHERE task_id = :t
                 LIMIT 1
            """), {"t": tid}).mappings().first() or {}

        auto_ingested = False
        auto_ingest_error = sc.get("ingest_error")
        session_id = sc.get("session_id")
        ingest_started   = sc.get("ingest_started_at") is not None
        ingest_finished  = sc.get("ingest_finished_at") is not None
        ingest_running   = ingest_started and not ingest_finished

        if AUTO_INGEST_ON_COMPLETE and result_url and not (ingest_started and not ingest_finished) and not session_id:
            auto_ingested = _start_ingest_background(tid, result_url)

        out = {
            "task_id": tid,
            "status": status,
            "result_url": result_url,
            "poll_ts": _now(),
            "session_id": session_id,
        }

        return jsonify({
            "ok": True, **out,
            "auto_ingested": auto_ingested,
            "auto_ingest_error": auto_ingest_error,
            "ingest_started": ingest_started,
            "ingest_running": ingest_running,
            "ingest_finished": ingest_finished
        })

    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}", "task_id": tid})

# ---------- Webhook route kept (disabled/unused) ----------
@app.post("/ops/sportai-callback")
def ops_sportai_callback():
    if not _guard(): return Response("Forbidden", 403)
    try:
        body = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return _err(f"Invalid JSON: {e}", 400)

    forced_uid = request.args.get("session_uid") or (
        body.get("session_uid") or body.get("sessionId") or body.get("session_id") or body.get("uid") or body.get("id")
    )
    task_id    = body.get("task_id") or body.get("id")
    result_url = body.get("result_url") or (body.get("data") or {}).get("result_url") or (body.get("links") or {}).get("result")

    if not result_url:
        return jsonify({"ok": True, "ignored": True, "reason": "no result_url in webhook"})

    try:
        r = requests.get(result_url, timeout=600)
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Result JSON is not an object")

        suid     = forced_uid or _detect_session_uid(payload)
        sid_hint = _detect_session_id(payload)

        with engine.begin() as conn:
            _store_raw_payload(conn, payload_dict=payload, session_id=sid_hint, session_uid=suid,
                               doc_type="sportai.result", source=result_url)
            summary = ingest_all_for_session(conn, sid_hint or -1, payload)
            session_id = int(summary.get("session_id") or (sid_hint or -1))
            _update_raw_result_session_id(conn, session_id=session_id, session_uid=suid)

        return jsonify({"ok": True, "session_id": session_id})
    except Exception as e:
        return _err(f"webhook ingest failed: {e}", 500)

# ---------- Admin helpers ----------
@app.get("/ops/db-ping")
def ops_db_ping():
    try:
        with engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        return _j()
    except Exception as e:
        return _err(str(e), 500)

@app.post("/ops/purge-sessions")
def ops_purge_sessions():
    if not _guard(): return Response("Forbidden", 403)
    try:
        js = request.get_json(silent=True) or {}
        sids = js.get("session_ids") or []
        uids = js.get("session_uids") or []
        with engine.begin() as conn:
            if sids:
                bad_ids = list(map(int, sids))
            elif uids:
                bad_ids = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid IN :uids"),
                                       {"uids": tuple(uids)}).scalars().all()
            else:
                bad_ids = []
            if not bad_ids:
                return jsonify({"ok": True, "deleted": {}, "note": "nothing to delete"}), 200
            tables = [
                "fact_ball_position","fact_player_position","fact_bounce","fact_swing",
                "dim_rally","dim_player","team_session","highlight","bounce_heatmap",
                "session_confidences","thumbnail","raw_result",
            ]
            counts = {}
            for t in tables:
                try:
                    res = conn.execute(sql_text(f"DELETE FROM {t} WHERE session_id = ANY(:ids)"), {"ids": bad_ids})
                    counts[t] = res.rowcount
                except Exception as e:
                    counts[t] = f"skip:{e}"
            try:
                conn.execute(sql_text("DELETE FROM dim_session WHERE session_id = ANY(:ids)"), {"ids": bad_ids})
            except Exception as e:
                counts["dim_session"] = f"skip:{e}"
        return jsonify({"ok": True, "deleted": counts})
    except Exception as e:
        return _err(str(e), 500)

# ---------- UI blueprint mount (unchanged) ----------
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
except Exception:
    pass

# ---------- Local run ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
