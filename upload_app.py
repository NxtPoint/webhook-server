# upload_app.py — Clean S3 → SportAI → Bronze (task_id-only, hardened)
# - Keeps: S3 upload, SportAI submit/status/cancel, presign, check-video
# - On status=completed: fetch result_url JSON and ingest via ingest_bronze_strict (task_id-only)
# - Mirrors public.submission_context → bronze.submission_context keyed by task_id (no behavior change)
# - HARDENING: move all DDL to AUTOCOMMIT, never run DDL inside transactions; avoid poisoned pool

import os, json, time, socket, sys, inspect, hashlib, re, threading
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, Response
from werkzeug.utils import secure_filename
from sqlalchemy import text as sql_text

# ---- boto3 is REQUIRED ----
try:
    import boto3
except Exception as e:
    raise RuntimeError("boto3 is required. Add it to requirements.txt and redeploy.") from e

app = Flask(__name__, template_folder="templates", static_folder="static")
app.url_map.strict_slashes = False
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_MB", "150")) * 1024 * 1024  # 150MB default

OPS_KEY = os.getenv("OPS_KEY", "").strip()

# ---------- SportAI config ----------
SPORTAI_BASE        = os.getenv("SPORT_AI_BASE", "https://api.sportai.com").strip().rstrip("/")
SPORTAI_SUBMIT_PATH = os.getenv("SPORT_AI_SUBMIT_PATH", "/api/statistics/tennis").strip()
SPORTAI_STATUS_PATH = os.getenv("SPORT_AI_STATUS_PATH", "/api/statistics/tennis/{task_id}/status").strip()
SPORTAI_TOKEN       = os.getenv("SPORT_AI_TOKEN", "").strip()
SPORTAI_CHECK_PATH  = os.getenv("SPORT_AI_CHECK_PATH",  "/api/videos/check").strip()
SPORTAI_CANCEL_PATH = os.getenv("SPORT_AI_CANCEL_PATH", "/api/tasks/{task_id}/cancel").strip()

AUTO_INGEST_ON_COMPLETE = os.getenv("AUTO_INGEST_ON_COMPLETE", "1").lower() in ("1","true","yes","y")
DEFAULT_REPLACE_ON_INGEST = (
    os.getenv("INGEST_REPLACE_EXISTING")
    or os.getenv("DEFAULT_REPLACE_ON_INGEST")
    or os.getenv("STRICT_REINGEST")
    or "1"
).strip().lower() in ("1","true","yes","y")

ENABLE_CORS = os.environ.get("ENABLE_CORS", "0").lower() in ("1","true","yes","y")

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

# ---------- DB engine / bronze ingest ----------
from db_init import engine  # noqa: E402
from ingest_bronze import ingest_bronze_strict_blueprint as ingest_bronze, _run_bronze_init_autocommit  # noqa: E402
app.register_blueprint(ingest_bronze, url_prefix="")

# Ensure bronze schema/DDL at startup (AUTOCOMMIT; safe no-op after first run)
try:
    _run_bronze_init_autocommit()
    print("upload_app: bronze init (autocommit) ok")
except Exception as e:
    print("upload_app: bronze init failed (non-fatal):", e)

# ---------- S3 config ----------
AWS_REGION = os.getenv("AWS_REGION", "").strip() or None
S3_BUCKET  = os.getenv("S3_BUCKET", "").strip() or None
S3_PREFIX  = (os.getenv("S3_PREFIX", "incoming") or "incoming").strip().strip("/")
S3_GET_EXPIRES = int(os.getenv("S3_GET_EXPIRES", "604800"))  # 7 days

def _require_s3():
    if not (AWS_REGION and S3_BUCKET):
        raise RuntimeError("S3 is required: set AWS_REGION and S3_BUCKET env vars")

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

def _sql_clean_one_select(q: str) -> str:
    q = (q or "").strip()
    q = re.sub(r"\s*;\s*$", "", q)
    if not re.match(r"^(select|with)\b", q, flags=re.I):
        raise ValueError("Only SELECT/CTE queries are allowed")
    if ";" in q:
        raise ValueError("Only a single SELECT/CTE statement is allowed")
    return q

def _sql_exec_to_json(q: str):
    q = _sql_clean_one_select(q)
    with engine.begin() as conn:
        res = conn.execute(sql_text(q))
        cols = list(res.keys())
        rows = [dict(zip(cols, r)) for r in res.fetchall()]
    return {"ok": True, "columns": cols, "rows": rows, "rowcount": len(rows)}

# ---------- Public submission_context (DDL hardened) ----------
def _ensure_public_submission_context_autocommit():
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as ddl:
        ddl.execute(sql_text("""
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
              session_id      BIGINT,
              last_status     TEXT,
              last_status_at  TIMESTAMPTZ,
              last_result_url TEXT,
              ingest_started_at  TIMESTAMPTZ,
              ingest_finished_at TIMESTAMPTZ,
              ingest_error       TEXT
            )
        """))
        for ddl_sql in (
            "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_started_at  TIMESTAMPTZ",
            "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ",
            "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_error       TEXT",
        ):
            ddl.execute(sql_text(ddl_sql))

# Call once at startup (safe/no-op)
try:
    _ensure_public_submission_context_autocommit()
    print("upload_app: public.submission_context init ok")
except Exception as e:
    print("upload_app: public.submission_context init failed (non-fatal):", e)

def _set_status_cache(conn, task_id: str, status: str | None, result_url: str | None):
    conn.execute(sql_text("""
        UPDATE submission_context
           SET last_status     = :s,
               last_status_at  = now(),
               last_result_url = :r
         WHERE task_id = :t
    """), {"t": task_id, "s": status, "r": result_url})

def _store_submission_context(task_id: str, email: str, meta: dict | None, video_url: str, share_url: str | None = None):
    m = meta or {}
    with engine.begin() as conn:
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

# -------------------------------------------------------
# SportAI HTTP
# -------------------------------------------------------
def _iter_submit_endpoints():
    for base in SPORTAI_BASES:
        for path in SPORTAI_SUBMIT_PATHS:
            yield f"{base.rstrip('/')}/{path.lstrip('/')}"

def _iter_status_endpoints(task_id: str):
    for base in SPORTAI_BASES:
        for path in SPORTAI_STATUS_PATHS:
            yield f"{base.rstrip('/')}/{path.lstrip('/').format(task_id=task_id)}"

def _sportai_submit(video_url: str, email: str | None = None, meta: dict | None = None) -> str:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"}

    base_min  = {"video_url": video_url, "version": "latest"}
    base_arr  = {"video_urls": [video_url], "version": "latest"}
    with_email = {**base_min, **({"email": email} if email else {})}
    with_meta  = {**with_email, **({"metadata": meta} if meta else {})}
    payload_variants = [with_meta, with_email, base_min, base_arr, {"url": video_url, "version": "latest"}]

    last_err = None
    for submit_url in _iter_submit_endpoints():
        for payload in payload_variants:
            try:
                app.logger.info("SportAI submit: video_url=%s via=%s", video_url, submit_url)
                r = requests.post(submit_url, headers=headers, json=payload, timeout=60)
                if r.status_code in (400,404,405,415,422):
                    last_err = f"{submit_url} -> {r.status_code}: {r.text}"; continue
                if r.status_code >= 500:
                    last_err = f"{submit_url} -> {r.status_code}: {r.text}"; break
                r.raise_for_status()
                j = r.json() if r.content else {}
                task_id = j.get("task_id") or (j.get("data") or {}).get("task_id") or j.get("id")
                if not task_id:
                    last_err = f"{submit_url} -> no task_id in response: {j}"; continue
                return str(task_id)
            except Exception as e:
                last_err = f"{submit_url} with {list(payload.keys())} -> {e}"; continue
    raise RuntimeError(f"SportAI submit failed across all endpoints: {last_err}")

def _sportai_status(task_id: str) -> dict:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}
    last_err, j = None, None
    for url in _iter_status_endpoints(task_id):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code >= 500:
                last_err = f"{url} -> {r.status_code}: {r.text}"; continue
            if r.status_code == 404:
                j = {"message": "Task not visible yet (404)."}; break
            r.raise_for_status()
            j = r.json() or {}; break
        except Exception as e:
            last_err = str(e)
    if j is None:
        raise RuntimeError(f"SportAI status failed: {last_err}")

    d = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), dict) else j
    status = (d.get("status") or d.get("task_status") or "").strip()
    msg = (j.get("message") or "").lower()
    if not status and "still being processed" in msg:
        status = "processing"

    prog = d.get("task_progress") or d.get("progress") or d.get("total_subtask_progress")
    try:
        if prog is None:
            progress_pct = None
        elif isinstance(prog, (int, float)) and prog <= 1.0:
            progress_pct = int(round(float(prog) * 100))
        else:
            progress_pct = int(round(float(prog)))
        if progress_pct is not None:
            progress_pct = max(0, min(100, progress_pct))
    except Exception:
        progress_pct = None

    result_url = d.get("result_url") or j.get("result_url")
    terminal = status.lower() in ("completed","done","success","succeeded","failed","canceled")
    if result_url and not terminal:
        status = "completed"; terminal = True
    if terminal and (progress_pct is None or progress_pct < 100):
        progress_pct = 100

    return {
        "status": status or None,
        "result_url": result_url,
        "progress_pct": progress_pct,
        "progress": progress_pct,
        "terminal": terminal,
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
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}
    cancel_paths = list(dict.fromkeys([
        SPORTAI_CANCEL_PATH, "/api/tasks/{task_id}/cancel",
        "/api/statistics/{task_id}/cancel", "/api/statistics/tennis/{task_id}/cancel",
    ]))
    last_err = None
    for base in SPORTAI_BASES:
        for path in cancel_paths:
            url = f"{base.rstrip('/')}/{path.lstrip('/').format(task_id=task_id)}"
            try:
                r = requests.post(url, headers=headers, json={}, timeout=30)
                if r.status_code in (400,404,405):
                    try: detail = r.json()
                    except Exception: detail = r.text
                    last_err = f"{url} -> {r.status_code}: {detail}"; continue
                r.raise_for_status()
                return (r.json() or {})
            except Exception as e:
                last_err = f"{url} -> {e}"
    raise RuntimeError(f"SportAI cancel failed across endpoints: {last_err}")

# -------------------------------------------------------
# S3 helpers
# -------------------------------------------------------
def _s3_client():
    if not (AWS_REGION and S3_BUCKET):
        raise RuntimeError("S3 is required: set AWS_REGION and S3_BUCKET")
    return boto3.client("s3", region_name=AWS_REGION)

def _s3_put_fileobj(fobj, key: str, content_type: str | None = None) -> dict:
    cli = _s3_client()
    extra = {"ContentType": content_type} if content_type else {}
    cli.upload_fileobj(fobj, S3_BUCKET, key, ExtraArgs=(extra or None))
    try:
        size = fobj.tell()
    except Exception:
        size = None
    return {"bucket": S3_BUCKET, "key": key, "size": size}

def _s3_presigned_get_url(key: str, expires: int | None = None) -> str:
    cli = _s3_client()
    return cli.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=int(expires or S3_GET_EXPIRES),
    )

# -------------------------------------------------------
# Import after app init (routes from bronze)
# -------------------------------------------------------
from ingest_bronze import ingest_bronze_strict  # noqa: E402

# -------------------------------------------------------
# Ingest worker (task_id-only) — DDL never inside transactions
# -------------------------------------------------------
def _start_ingest_background(task_id: str, result_url: str) -> bool:
    """Return True if we started a worker; False if already done/running."""
    with engine.begin() as conn:
        row = conn.execute(sql_text("""
            SELECT session_id, ingest_started_at, ingest_finished_at
              FROM submission_context
             WHERE task_id = :t
             LIMIT 1
        """), {"t": task_id}).mappings().first()

        if row and row.get("session_id"):
            return False
        if row and row.get("ingest_started_at") and not row.get("ingest_finished_at"):
            return False

        conn.execute(sql_text("""
            UPDATE submission_context
               SET ingest_started_at = now(),
                   ingest_error = NULL
             WHERE task_id = :t
        """), {"t": task_id})

    def _worker():
        try:
            r = requests.get(result_url, timeout=600)
            r.raise_for_status()
            payload = r.json()
            with engine.begin() as conn:
                # IMPORTANT: DDL already done at startup; no schema changes here
                res = ingest_bronze_strict(conn, payload, replace=DEFAULT_REPLACE_ON_INGEST, src_hint=result_url, task_id=task_id)
                sid = res.get("session_id")  # may be None if not set by ingest path
                conn.execute(sql_text("""
                    UPDATE submission_context
                       SET session_id        = COALESCE(:sid, session_id),
                           ingest_finished_at= now(),
                           ingest_error      = NULL,
                           last_result_url   = :url,
                           last_status       = 'completed',
                           last_status_at    = now()
                     WHERE task_id = :t
                """), {"sid": sid, "t": task_id, "url": result_url})
        except Exception as e:
            with engine.begin() as conn:
                conn.execute(sql_text("""
                    UPDATE submission_context
                       SET ingest_error = :err,
                           ingest_finished_at = now()
                     WHERE task_id = :t
                """), {"t": task_id, "err": f"{e.__class__.__name__}: {e}"})

    threading.Thread(target=_worker, daemon=True).start()
    return True

# -------------------------------------------------------
# Routes
# -------------------------------------------------------
@app.get("/")
def root_ok(): return jsonify({"service": "NextPoint Upload/Ingester v3 (S3-only)", "ok": True})

@app.get("/healthz")
def healthz_ok(): return "OK", 200

@app.get("/__routes")
def __routes_open():
    routes = [{"rule": r.rule, "endpoint": r.endpoint,
               "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})}
              for r in app.url_map.iter_rules()]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def __routes_locked():
    if not _guard(): return Response("Forbidden", 403)
    return __routes_open()

@app.get("/upload/api/status")
def upload_status():
    return jsonify({
        "ok": True,
        "storage": "s3",
        "s3_ready": bool(S3_BUCKET),
        "s3_bucket": S3_BUCKET or None,
        "s3_prefix": S3_PREFIX or None,
        "sportai_ready": bool(SPORTAI_TOKEN),
        "target_folder": f"s3://{S3_BUCKET}/{S3_PREFIX}" if S3_BUCKET else None,
    })

@app.get("/ops/env")
def ops_env():
    if not _guard(): return Response("Forbidden", 403)
    return jsonify({
        "ok": True,
        "SPORT_AI_BASE": SPORTAI_BASE,
        "SPORT_AI_SUBMIT_PATHS": SPORTAI_SUBMIT_PATHS,
        "SPORT_AI_STATUS_PATHS": SPORTAI_STATUS_PATHS,
        "has_TOKEN": bool(SPORTAI_TOKEN),
        "DEFAULT_REPLACE_ON_INGEST": DEFAULT_REPLACE_ON_INGEST,
        "AUTO_INGEST_ON_COMPLETE": AUTO_INGEST_ON_COMPLETE,
        "AWS_REGION": AWS_REGION,
        "S3_BUCKET": S3_BUCKET,
        "S3_PREFIX": S3_PREFIX,
    })

# ---------- Presign ----------
@app.post("/upload/api/s3-presign")
def api_s3_presign():
    _require_s3()
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "video.mp4").strip()
    ctype = (body.get("content_type") or "application/octet-stream").strip()
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

# ---------- Video check & cancel ----------
@app.route("/upload/api/check-video", methods=["POST", "OPTIONS"])
def api_check_video():
    if request.method == "OPTIONS": return ("", 204)
    def _passed(obj):
        if isinstance(obj, dict):
            if "ok" in obj: return bool(obj["ok"])
            if str(obj.get("status","")).lower() in ("ok","success","passed","ready"): return True
            if obj.get("errors"): return False
        return True
    try:
        if request.is_json:
            body = request.get_json(silent=True) or {}
            video_url = (body.get("video_url") or body.get("share_url") or "").strip()
            if not video_url: return jsonify({"ok": False, "error": "video_url required"}), 400
            chk = _sportai_check(video_url)
            return jsonify({"ok": True, "video_url": video_url, "check": chk, "check_passed": _passed(chk)})
        f = request.files.get("file") or request.files.get("video")
        if not f or not f.filename: return jsonify({"ok": False, "error": "No file provided."}), 400
        clean = secure_filename(f.filename); ts = int(time.time())
        key = f"{S3_PREFIX}/{ts}_{clean}"
        try: f.stream.seek(0)
        except Exception: pass
        _ = _s3_put_fileobj(f.stream, key, content_type=getattr(f, "mimetype", None))
        video_url = _s3_presigned_get_url(key)
        chk = _sportai_check(video_url)
        return jsonify({"ok": True, "video_url": video_url, "check": chk, "check_passed": _passed(chk)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/upload/api/cancel-task")
def api_cancel_task():
    tid = request.values.get("task_id") or (request.get_json(silent=True) or {}).get("task_id")
    if not tid: return jsonify({"ok": False, "error": "task_id required"}), 400
    try:
        out = _sportai_cancel(str(tid))
        with engine.begin() as conn:
            _set_status_cache(conn, tid, "canceled", None)
        return jsonify({"ok": True, "data": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

def _extract_meta_from_form(form):
    return {
        "customer_name": form.get("customer_name") or form.get("CustomerName"),
        "match_date": form.get("match_date") or form.get("MatchDate"),
        "start_time": form.get("start_time") or form.get("StartTime"),
        "location": form.get("location") or form.get("Location"),
        "player_a_name": form.get("player_a_name") or form.get("PlayerAName"),
        "player_b_name": form.get("player_b_name") or form.get("PlayerBName"),
        "player_a_utr": form.get("player_a_utr") or form.get("PlayerAUTR"),
        "player_b_utr": form.get("player_b_utr") or form.get("PlayerBUTR"),
    }

# ---------- Upload API (S3 only) ----------
@app.route("/upload/api/upload", methods=["POST", "OPTIONS"])
def api_upload_to_s3():
    if request.method == "OPTIONS": return ("", 204)
    _require_s3()

    if request.is_json:
        body = request.get_json(silent=True) or {}
        video_url = (body.get("video_url") or body.get("share_url") or "").strip()
        email = (body.get("email") or "").strip().lower()
        meta = body.get("meta") or body.get("metadata") or {}
        if video_url:
            try:
                task_id = _sportai_submit(video_url, email=email, meta=meta)
                _store_submission_context(task_id, email, meta, video_url, share_url=body.get("share_url"))
                with engine.begin() as conn:
                    _set_status_cache(conn, task_id, "queued", None)
                return jsonify({"ok": True, "task_id": task_id, "video_url": video_url})
            except Exception as e:
                return jsonify({"ok": False, "error": f"SportAI submit failed: {e}"}), 502

    f = request.files.get("file") or request.files.get("video")
    email = (request.form.get("email") or "").strip().lower()
    if not f or not f.filename: return jsonify({"ok": False, "error": "No file provided."}), 400

    clean = secure_filename(f.filename); ts = int(time.time()); key = f"{S3_PREFIX}/{ts}_{clean}"
    try:
        try: f.stream.seek(0)
        except Exception: pass
        meta_up = _s3_put_fileobj(f.stream, key, content_type=getattr(f, "mimetype", None))
        video_url = _s3_presigned_get_url(key)
        meta = _extract_meta_from_form(request.form)
        task_id = _sportai_submit(video_url, email=email, meta=meta)
        _store_submission_context(task_id, email, meta, video_url, share_url=video_url)
        with engine.begin() as conn:
            _set_status_cache(conn, task_id, "queued", None)
        return jsonify({
            "ok": True, "task_id": task_id, "share_url": video_url, "video_url": video_url,
            "upload": {"path": key, "size": meta_up.get("size"), "name": clean}
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"S3 upload/submit failed: {e}"}), 502

# Legacy alias (kept)
@app.route("/upload", methods=["POST", "OPTIONS"])
def upload_alias():
    if request.method == "OPTIONS": return ("", 204)
    return api_upload_to_s3()

# ---------- Task poll (normalized progress + optional auto-ingest) ----------
@app.get("/upload/api/task-status")
def api_task_status():
    tid = request.args.get("task_id")
    if not tid:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    try:
        out = _sportai_status(tid)
        status = (out.get("status") or "").lower()
        result_url = out.get("result_url")
        terminal = bool(out.get("terminal"))

        with engine.begin() as conn:
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

        if AUTO_INGEST_ON_COMPLETE and terminal and result_url and not session_id and not ingest_running:
            _ = _start_ingest_background(tid, result_url)
            ingest_started = True

        if session_id and ingest_finished and not auto_ingest_error:
            auto_ingested = True

        return jsonify({
            "ok": True, **out,
            "session_id": session_id,
            "auto_ingested": auto_ingested,
            "auto_ingest_error": auto_ingest_error,
            "ingest_started": ingest_started,
            "ingest_running": ingest_running,
            "ingest_finished": ingest_finished
        })

    except Exception as e:
        # Never break the poller
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 200

# ---------- Manual ingest helper (task_id-only) ----------
@app.post("/ops/ingest-task")
def ops_ingest_task():
    if not _guard(): return Response("Forbidden", 403)
    body = request.get_json(silent=True) or {}
    tid = (body.get("task_id") or "").strip()
    if not tid: return jsonify({"ok": False, "error": "task_id required"}), 400
    try:
        st = _sportai_status(tid)
        result_url = st.get("result_url")
        if not result_url:
            return jsonify({"ok": False, "error": "result_url not available yet"}), 400

        r = requests.get(result_url, timeout=300); r.raise_for_status()
        payload = r.json()

        with engine.begin() as conn:
            # DDL already done at startup; here we just ingest
            res = ingest_bronze_strict(conn, payload, replace=DEFAULT_REPLACE_ON_INGEST, src_hint=result_url, task_id=tid)
            sid = res.get("session_id")

            conn.execute(sql_text(
                "UPDATE submission_context SET session_id=COALESCE(:sid, session_id) WHERE task_id=:t"
            ), {"sid": sid, "t": tid})

        return jsonify({"ok": True, "task_id": tid, "session_id": sid})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

# ---------- SQL helpers (SELECT-only) ----------
@app.post("/ops/sqlx")
def ops_sql_json():
    if not _guard(): return Response("Forbidden", 403)
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(_sql_exec_to_json(body.get("q","")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.get("/ops/sqlq")
def ops_sql_qs():
    if not _guard(): return Response("Forbidden", 403)
    try:
        return jsonify(_sql_exec_to_json(request.args.get("q","")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# -------------------------------------------------------
# Optional UI blueprint (if present); harmless if missing
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
        "ok": True, "module": __file__, "searchpath": search,
        "expected_template_path": expected, "exists": exists, "head": head,
    })
