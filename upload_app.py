# upload_app.py — Clean S3 → SportAI → Bronze (task_id-only)
# - Keeps: S3 upload, SportAI submit/status/cancel, presign, check-video
# - On status=completed: fetch result_url JSON and ingest via ingest_bronze_strict (task_id-only)
# - Uses bronze.submission_context keyed by task_id (no public schema)

import os, json, time, socket, sys, inspect, hashlib, re, threading
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, Response
from werkzeug.utils import secure_filename
from sqlalchemy import text as sql_text

import models_billing  # ensure billing models are registered
from billing_api import billing_bp

# ==========================
# BOTO3 (REQUIRED)
# ==========================
try:
    import boto3
except Exception as e:
    raise RuntimeError("boto3 is required. Add it to requirements.txt and redeploy.") from e

# ==========================
# FLASK APP
# ==========================
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
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

# ==========================
# ENV / CONFIG
# ==========================
OPS_KEY = os.getenv("OPS_KEY", "").strip()

# ---------- SportAI config ----------
SPORTAI_BASE = os.getenv("SPORT_AI_BASE", "https://api.sportai.com").strip().rstrip("/")

# Hard-guard: never allow .app even if env is wrong
if "sportai.app" in SPORTAI_BASE:
    SPORTAI_BASE = "https://api.sportai.com"

SPORTAI_SUBMIT_PATH = os.getenv("SPORT_AI_SUBMIT_PATH", "/api/statistics/tennis").strip()
SPORTAI_STATUS_PATH = os.getenv("SPORT_AI_STATUS_PATH", "/api/statistics/tennis/{task_id}/status").strip()
SPORTAI_TOKEN       = os.getenv("SPORT_AI_TOKEN", "").strip()
SPORTAI_CHECK_PATH  = os.getenv("SPORT_AI_CHECK_PATH",  "/api/videos/check").strip()
SPORTAI_CANCEL_PATH = os.getenv("SPORT_AI_CANCEL_PATH", "/api/tasks/{task_id}/cancel").strip()

# Auto-ingest once completed
AUTO_INGEST_ON_COMPLETE = os.getenv("AUTO_INGEST_ON_COMPLETE", "1").lower() in ("1","true","yes","y")
DEFAULT_REPLACE_ON_INGEST = (
    os.getenv("INGEST_REPLACE_EXISTING")
    or os.getenv("DEFAULT_REPLACE_ON_INGEST")
    or os.getenv("STRICT_REINGEST")
    or "1"
).strip().lower() in ("1","true","yes","y")

ENABLE_CORS = os.environ.get("ENABLE_CORS", "0").lower() in ("1","true","yes","y")

# Try public hostnames / path variants for resilience — COM ONLY
SPORTAI_BASES = list(dict.fromkeys([
    SPORTAI_BASE,
    "https://api.sportai.com",
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
from ingest_bronze import ingest_bronze, ingest_bronze_strict, _run_bronze_init  # noqa: E402
from build_silver_point_detail import build_silver as build_silver_point_detail  # noqa: E402
app.register_blueprint(ingest_bronze, url_prefix="")

# ---------- S3 config (MANDATORY) ----------
AWS_REGION = os.getenv("AWS_REGION", "").strip() or None
S3_BUCKET  = os.getenv("S3_BUCKET", "").strip() or None
S3_PREFIX  = (os.getenv("S3_PREFIX", "incoming") or "incoming").strip().strip("/")
S3_GET_EXPIRES = int(os.getenv("S3_GET_EXPIRES", "604800"))  # 7 days

def _require_s3():
    if not (AWS_REGION and S3_BUCKET):
        raise RuntimeError("S3 is required: set AWS_REGION and S3_BUCKET env vars")

# ---------- Wix backend notify (server-side completion email trigger) ----------
# Render -> Wix backend endpoint. Wix backend validates X-Ops-Key.

WIX_NOTIFY_URL = (
    os.getenv("WIX_NOTIFY_UPLOAD_COMPLETE_URL")  # NEW (your Render env)
    or os.getenv("WIX_NOTIFY_URL")               # legacy fallback (safe)
    or ""
).strip()

WIX_NOTIFY_KEY = (
    os.getenv("RENDER_TO_WIX_OPS_KEY")  # NEW (your Render env)
    or os.getenv("WIX_NOTIFY_KEY")      # legacy fallback (safe)
    or ""
).strip()

WIX_NOTIFY_TIMEOUT_S = int(os.getenv("WIX_NOTIFY_TIMEOUT_S", "15"))
WIX_NOTIFY_RETRIES = int(os.getenv("WIX_NOTIFY_RETRIES", "3"))


# ==========================
# HELPERS
# ==========================
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

def _guard_wix_upload_task() -> bool:
    expected = (os.getenv("WIX_UPLOAD_TASK_KEY") or "").strip()
    if not expected:
        return False
    hk = request.headers.get("X-Ops-Key") or request.headers.get("X-Ops-KEY") or request.headers.get("X-OPS-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    return hk == expected

#------- helper to fix wix front end upload issues ---- delete later not required
def _head_url(url: str, timeout: int = 30) -> dict:
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        h = {k.lower(): v for k, v in (r.headers or {}).items()}
        return {
            "ok": True,
            "status_code": r.status_code,
            "content_length": h.get("content-length"),
            "content_type": h.get("content-type"),
            "accept_ranges": h.get("accept-ranges"),
            "etag": h.get("etag"),
            "final_url": str(r.url),
        }
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}

def _range_probe(url: str, nbytes: int = 1024 * 256, timeout: int = 30) -> dict:
    """
    Fetch first N bytes via HTTP Range. Proves:
    - URL is reachable from Render
    - Range works (what SportAI often uses)
    - Content is non-empty
    """
    try:
        headers = {"Range": f"bytes=0-{nbytes-1}"}
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        # 206 Partial Content is ideal; 200 also acceptable
        ok = r.status_code in (200, 206)
        return {
            "ok": ok,
            "status_code": r.status_code,
            "bytes_received": len(r.content or b""),
            "content_type": r.headers.get("Content-Type"),
            "content_range": r.headers.get("Content-Range"),
            "accept_ranges": r.headers.get("Accept-Ranges"),
        }
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


# ==========================
# BRONZE.SUBMISSION_CONTEXT (TASK_ID KEYED)
# ==========================
def _ensure_submission_context_schema(conn):
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze;"))
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.submission_context (
          task_id            TEXT PRIMARY KEY,
          created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          email              TEXT,
          customer_name      TEXT,
          match_date         DATE,
          start_time         TEXT,
          location           TEXT,
          player_a_name      TEXT,
          player_b_name      TEXT,
          player_a_utr       TEXT,
          player_b_utr       TEXT,
          video_url          TEXT,
          share_url          TEXT,
          raw_meta           JSONB,
          session_id         TEXT,
          last_status        TEXT,
          last_status_at     TIMESTAMPTZ,
          last_result_url    TEXT,
          ingest_started_at  TIMESTAMPTZ,
          ingest_finished_at TIMESTAMPTZ,
          ingest_error       TEXT,

          -- Wix notify audit (server-side completion email)
          wix_notified_at    TIMESTAMPTZ,
          wix_notify_status  TEXT,
          wix_notify_error   TEXT
        );
    """))

    # Idempotent safety: keep these as no-ops if columns already exist
    # NOTE: Fixed a production bug here: missing comma after session_id ALTER caused SQL string concatenation.
    for ddl in (
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_status TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_status_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_result_url TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_started_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_error TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS session_id TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notified_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notify_status TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notify_error TEXT",
    ):
        conn.execute(sql_text(ddl))

def _store_submission_context(task_id: str, email: str, meta: dict | None, video_url: str, share_url: str | None = None):
    if not engine:
        return
    m = meta or {}
    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        conn.execute(sql_text("""
            INSERT INTO bronze.submission_context (
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

def _set_status_cache(conn, task_id: str, status: str | None, result_url: str | None):
    conn.execute(sql_text("""
        UPDATE bronze.submission_context
           SET last_status     = :s,
               last_status_at  = now(),
               last_result_url = :r
         WHERE task_id = :t
    """), {"t": task_id, "s": status, "r": result_url})

def _mirror_submission_to_bronze_by_task(conn, task_id: str):
    """No-op: submission_context now lives directly in bronze.submission_context."""
    return

# ==========================
# WIX NOTIFY (RENDER → WIX → AUTOMATION)
# ==========================
def _wix_payload(task_id: str, status: str, session_id: str | None, result_url: str | None, error: str | None):
    return {
        "task_id": task_id,
        "status": status,  # "completed" | "failed"
        "session_id": session_id,
        "result_url": result_url,
        "error": error,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
    }

def _already_notified(conn, task_id: str, desired_status: str) -> bool:
    row = conn.execute(sql_text("""
        SELECT wix_notified_at, wix_notify_status
          FROM bronze.submission_context
         WHERE task_id = :t
         LIMIT 1
    """), {"t": task_id}).mappings().first()
    return bool(row and row.get("wix_notified_at") and (row.get("wix_notify_status") == desired_status))

def _mark_wix_notify(conn, task_id: str, status: str, err: str | None):
    conn.execute(sql_text("""
        UPDATE bronze.submission_context
           SET wix_notified_at   = now(),
               wix_notify_status = :s,
               wix_notify_error  = :e
         WHERE task_id = :t
    """), {"t": task_id, "s": status, "e": err})

def _notify_wix(task_id: str, status: str, session_id: str | None, result_url: str | None, error: str | None) -> None:
    """
    Server-side: Render calls Wix backend notify endpoint.
    Idempotent: prevents spamming via bronze.submission_context(wix_notified_at, wix_notify_status).
    """
    if not WIX_NOTIFY_URL:
        app.logger.warning("WIX_NOTIFY_URL not set; skipping Wix notify task_id=%s", task_id)
        return

    # Gate idempotency from DB
    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        if _already_notified(conn, task_id, status):
            return

    headers = {"Content-Type": "application/json"}
    if WIX_NOTIFY_KEY:
        # Wix handler expects X-Ops-Key (matches your working Postman/Wix setup)
        headers["X-Ops-Key"] = WIX_NOTIFY_KEY

    payload = _wix_payload(task_id, status, session_id, result_url, error)

    last_err = None
    for _ in range(max(1, WIX_NOTIFY_RETRIES)):
        try:
            r = requests.post(WIX_NOTIFY_URL, headers=headers, json=payload, timeout=WIX_NOTIFY_TIMEOUT_S)
            if r.status_code >= 400:
                last_err = f"HTTP {r.status_code}: {r.text}"
                continue

            with engine.begin() as conn:
                _ensure_submission_context_schema(conn)
                _mark_wix_notify(conn, task_id, status, None)
            return

        except Exception as e:
            last_err = f"{e.__class__.__name__}: {e}"

    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        _mark_wix_notify(conn, task_id, status, last_err)

# ==========================
# SPORTAI HTTP
# ==========================
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

    headers = {
        "Authorization": f"Bearer {SPORTAI_TOKEN}",
        "Content-Type": "application/json",
    }

    # Canonical payloads using *only* video_url (no url / video_urls legacy forms)
    base_min = {"video_url": video_url, "version": "latest"}
    with_email = {**base_min, **({"email": email} if email else {})}
    with_meta = {**with_email, **({"metadata": meta} if meta else {})}

    payload_variants = [with_meta, with_email, base_min]

    last_err = None
    for submit_url in _iter_submit_endpoints():
        for payload in payload_variants:
            try:
                app.logger.info(
                    "SPORTAI SUBMIT url=%s payload_keys=%s video_url=%s",
                    submit_url,
                    list(payload.keys()),
                    video_url,
                )

                r = requests.post(submit_url, headers=headers, json=payload, timeout=60)

                if r.status_code in (400, 404, 405, 415, 422):
                    last_err = f"{submit_url} -> {r.status_code}: {r.text}"
                    continue

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
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}
    last_err, j = None, None
    for url in _iter_status_endpoints(task_id):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code >= 500:
                last_err = f"{url} -> {r.status_code}: {r.text}"
                continue
            if r.status_code == 404:
                j = {"message": "Task not visible yet (404)."}
                break
            r.raise_for_status()
            j = r.json() or {}
            break
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
    terminal = status.lower() in ("completed", "done", "success", "succeeded", "failed", "canceled")
    if result_url and not terminal:
        status = "completed"
        terminal = True
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

    # SportAI docs: expects POST with video_urls[]
    payload = {"video_urls": [video_url], "version": "stable"}

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    app.logger.error("SPORTAI CHECK url=%s status=%s body=%s", url, r.status_code, (r.text or "")[:800])


    # IMPORTANT: expose upstream failures clearly
    if r.status_code >= 400:
        raise RuntimeError(f"SportAI check failed HTTP {r.status_code}: {r.text}")

    return r.json() if r.content else {}


def _sportai_cancel(task_id: str) -> dict:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}
    cancel_paths = list(dict.fromkeys([
        SPORTAI_CANCEL_PATH,
        "/api/tasks/{task_id}/cancel",
        "/api/statistics/{task_id}/cancel",
        "/api/statistics/tennis/{task_id}/cancel",
    ]))
    last_err = None
    for base in SPORTAI_BASES:
        for path in cancel_paths:
            url = f"{base.rstrip('/')}/{path.lstrip('/').format(task_id=task_id)}"
            try:
                r = requests.post(url, headers=headers, json={}, timeout=30)
                if r.status_code in (400, 404, 405):
                    try:
                        detail = r.json()
                    except Exception:
                        detail = r.text
                    last_err = f"{url} -> {r.status_code}: {detail}"
                    continue
                r.raise_for_status()
                return (r.json() or {})
            except Exception as e:
                last_err = f"{url} -> {e}"
    raise RuntimeError(f"SportAI cancel failed across endpoints: {last_err}")

# ==========================
# S3 HELPERS
# ==========================
def _s3_client():
    _require_s3()
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

# ==========================
# INGEST WORKER (TASK_ID-ONLY)
# ==========================
def _do_ingest(task_id: str, result_url: str) -> bool:
    try:
        # mark started
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_started_at = COALESCE(ingest_started_at, now()),
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"t": task_id})

        # fetch result and ingest
        r = requests.get(result_url, timeout=600)
        r.raise_for_status()
        payload = r.json()

        with engine.begin() as conn:
            _run_bronze_init(conn)  # ensure bronze schema/tables exist
            res = ingest_bronze_strict(
                conn,
                payload,
                replace=DEFAULT_REPLACE_ON_INGEST,
                src_hint=result_url,
                task_id=task_id,  # canonical key
            )
            sid = res.get("session_id")

            # status + mirror
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET session_id         = :sid,
                       ingest_finished_at = now(),
                       ingest_error       = NULL,
                       last_result_url    = :url,
                       last_status        = 'completed',
                       last_status_at     = now()
                 WHERE task_id = :t
            """), {"sid": sid, "t": task_id, "url": result_url})

            _mirror_submission_to_bronze_by_task(conn, task_id)

        # --- auto-build Silver point_detail after Bronze succeeds ---
        try:
            build_silver_point_detail(task_id=task_id, phase="all", replace=True)
        except Exception as e:
            app.logger.error("Silver build failed for task_id=%s: %s", task_id, e)

        # --- NEW: notify Wix after successful ingest (server-side email trigger) ---
        try:
            _notify_wix(task_id, status="completed", session_id=sid, result_url=result_url, error=None)
        except Exception as e:
            app.logger.exception("Wix notify failed (completed) task_id=%s: %s", task_id, e)

        return True

    except Exception as e:
        err_txt = f"{e.__class__.__name__}: {e}"
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_error = :err,
                       ingest_finished_at = now()
                 WHERE task_id = :t
            """), {"t": task_id, "err": err_txt})

        # --- NEW: notify Wix about failure (optional but useful) ---
        try:
            _notify_wix(task_id, status="failed", session_id=None, result_url=result_url, error=err_txt)
        except Exception as e2:
            app.logger.exception("Wix notify failed (failed) task_id=%s: %s", task_id, e2)

        return False

def _start_ingest_background(task_id: str, result_url: str) -> bool:
    """Return True if we started a worker; False if already done/running."""
    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        row = conn.execute(sql_text("""
            SELECT session_id, ingest_started_at, ingest_finished_at
              FROM bronze.submission_context
             WHERE task_id = :t
             LIMIT 1
        """), {"t": task_id}).mappings().first()

        if row and row.get("session_id"):
            return False  # already done
        if row and row.get("ingest_started_at") and not row.get("ingest_finished_at"):
            return False  # already running

        conn.execute(sql_text("""
            UPDATE bronze.submission_context
               SET ingest_started_at = now(),
                   ingest_error = NULL
             WHERE task_id = :t
        """), {"t": task_id})

    th = threading.Thread(target=_do_ingest, args=(task_id, result_url), daemon=True)
    th.start()
    return True

# ==========================
# PUBLIC ENDPOINTS (UPLOADS + STATUS + OPS)
# ==========================
@app.get("/")
def root_ok():
    return jsonify({"service": "NextPoint Upload/Ingester v3 (S3-only)", "ok": True})

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

@app.get("/__routes")
def __routes_open():
    routes = [{"rule": r.rule, "endpoint": r.endpoint,
               "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})}
              for r in app.url_map.iter_rules()]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def __routes_locked():
    if not _guard():
        return Response("Forbidden", 403)
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
    if not _guard():
        return Response("Forbidden", 403)
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
        "WIX_NOTIFY_URL_SET": bool(WIX_NOTIFY_URL),
        "WIX_NOTIFY_KEY_SET": bool(WIX_NOTIFY_KEY),
    })

# ==========================
# PRESIGN (OPTIONAL)
# ==========================
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

# ==========================
# VIDEO CHECK & CANCEL
# ==========================
@app.route("/upload/api/check-video", methods=["POST", "OPTIONS"])
def api_check_video():
    if request.method == "OPTIONS":
        return ("", 204)

    def _passed(obj):
        if isinstance(obj, dict):
            if "ok" in obj:
                return bool(obj["ok"])
            if str(obj.get("status", "")).lower() in ("ok", "success", "passed", "ready"):
                return True
            if obj.get("errors"):
                return False
        return True

    try:
        # -------------------------
        # JSON path (client provides video_url)
        # -------------------------
        if request.is_json:
            body = request.get_json(silent=True) or {}
            video_url = (body.get("video_url") or body.get("share_url") or "").strip()
            if not video_url:
                return jsonify({"ok": False, "error": "video_url required"}), 400

            # Precheck: prove URL is reachable + range-readable from Render
            head = _head_url(video_url)
            probe = _range_probe(video_url)

            # MUST-SEE log line in Render logs
            app.logger.error("PRECHECK video_url=%s head=%s probe=%s", video_url, head, probe)

            # Retry SportAI check a few times
            last_exc = None
            for attempt in range(1, 4):
                try:
                    chk = _sportai_check(video_url)
                    return jsonify({
                        "ok": True,
                        "video_url": video_url,
                        "precheck": {"head": head, "range_probe": probe},
                        "check": chk,
                        "check_passed": _passed(chk),
                    })
                except Exception as e:
                    last_exc = e
                    app.logger.error("SportAI check attempt %s failed: %s", attempt, e)
                    time.sleep(1.5 * attempt)

            # Return precheck even when SportAI fails (critical for diagnosis)
            return jsonify({
                "ok": False,
                "video_url": video_url,
                "precheck": {"head": head, "range_probe": probe},
                "error": str(last_exc),
            }), 502

        # -------------------------
        # Multipart path (file upload -> S3 -> check)
        # -------------------------
        f = request.files.get("file") or request.files.get("video")
        if not f or not f.filename:
            return jsonify({"ok": False, "error": "No file provided."}), 400

        clean = secure_filename(f.filename)
        ts = int(time.time())
        key = f"{S3_PREFIX}/{ts}_{clean}"

        try:
            f.stream.seek(0)
        except Exception:
            pass

        _ = _s3_put_fileobj(f.stream, key, content_type=getattr(f, "mimetype", None))
        video_url = _s3_presigned_get_url(key)

        head = _head_url(video_url)
        probe = _range_probe(video_url)
        app.logger.error("PRECHECK(multipart) video_url=%s head=%s probe=%s", video_url, head, probe)

        chk = _sportai_check(video_url)
        return jsonify({
            "ok": True,
            "video_url": video_url,
            "precheck": {"head": head, "range_probe": probe},
            "check": chk,
            "check_passed": _passed(chk),
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.post("/upload/api/cancel-task")
def api_cancel_task():
    tid = request.values.get("task_id") or (request.get_json(silent=True) or {}).get("task_id")
    if not tid:
        return jsonify({"ok": False, "error": "task_id required"}), 400
    try:
        out = _sportai_cancel(str(tid))
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
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

# ==========================
# UPLOAD API (S3 ONLY)
# ==========================
@app.route("/upload/api/upload", methods=["POST", "OPTIONS"])
def api_upload_to_s3():
    if request.method == "OPTIONS":
        return ("", 204)
    _require_s3()

    # JSON path: already have video_url (e.g., after presign upload)
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
                    _ensure_submission_context_schema(conn)
                    _set_status_cache(conn, task_id, "queued", None)
                return jsonify({"ok": True, "task_id": task_id, "video_url": video_url})
            except Exception as e:
                return jsonify({"ok": False, "error": f"SportAI submit failed: {e}"}), 502

    # Multipart path: browser → server → S3 (fallback)
    f = request.files.get("file") or request.files.get("video")
    email = (request.form.get("email") or "").strip().lower()
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file provided."}), 400

    clean = secure_filename(f.filename)
    ts = int(time.time())
    key = f"{S3_PREFIX}/{ts}_{clean}"
    try:
        try:
            f.stream.seek(0)
        except Exception:
            pass
        meta_up = _s3_put_fileobj(f.stream, key, content_type=getattr(f, "mimetype", None))
        video_url = _s3_presigned_get_url(key)
        meta = _extract_meta_from_form(request.form)
        task_id = _sportai_submit(video_url, email=email, meta=meta)
        _store_submission_context(task_id, email, meta, video_url, share_url=video_url)
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            _set_status_cache(conn, task_id, "queued", None)
        return jsonify({
            "ok": True, "task_id": task_id, "share_url": video_url, "video_url": video_url,
            "upload": {"path": key, "size": meta_up.get("size"), "name": clean}
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"S3 upload/submit failed: {e}"}), 502

# =========================
# /api/upload_task — Wix JSON → store full metadata 1:1 into submission_context.raw_meta
# Minimal change: extend meta mapping + persist player_a_utr and set scores + end_time
# =========================

@app.post("/api/upload_task")
def api_upload_task():
    """
    JSON-only endpoint for Wix
    expects:
      ownerId, playerId, playerName,
      opponentName, opponentUtr,
      playerUTR,
      startTime, endTime, matchDate, location,
      Set 1 (A/B), Set 2 (A/B), Set 3 (A/B),
      videoUrl, firstServer
    """
    if not request.is_json:
        return jsonify({"ok": False, "error": "JSON body required"}), 400

    body = request.get_json(silent=True) or {}

    # =========================
    # Extract canonical fields (accept both camelCase + your internal variants)
    # =========================
    source_url    = (body.get("videoUrl") or body.get("video_url") or "").strip()
    owner_id      = (body.get("ownerId") or body.get("owner_id") or "").strip()
    player_id     = body.get("playerId") or body.get("player_id")
    player_name   = (body.get("playerName") or body.get("player_a_name") or "").strip()
    opponent_name = (body.get("opponentName") or body.get("player_b_name") or "").strip()
    opponent_utr  = (body.get("opponentUtr") or body.get("player_b_utr") or "").strip()
    player_utr    = (body.get("playerUTR") or body.get("playerUtr") or body.get("myUtr") or body.get("player_a_utr") or "").strip()

    start_time    = (body.get("startTime") or body.get("start_time") or "").strip()
    end_time      = (body.get("endTime") or body.get("end_time") or "").strip()
    match_date    = (body.get("matchDate") or body.get("match_date") or "").strip()
    location      = (body.get("location") or "").strip()
    first_server  = (body.get("firstServer") or body.get("first_server") or "").strip()

    # Wix CMS score fields (support both spaced + coded variants)
    set1A = body.get("Set 1 (A)") if "Set 1 (A)" in body else body.get("set1A")
    set2A = body.get("Set 2 (A)") if "Set 2 (A)" in body else body.get("set2A")
    set3A = body.get("Set 3 (A)") if "Set 3 (A)" in body else body.get("set3A")
    set1B = body.get("Set 1 (B)") if "Set 1 (B)" in body else body.get("set1B")
    set2B = body.get("Set 2 (B)") if "Set 2 (B)" in body else body.get("set2B")
    set3B = body.get("Set 3 (B)") if "Set 3 (B)" in body else body.get("set3B")

    if not source_url:
        return jsonify({"ok": False, "error": "videoUrl required"}), 400

    # helper to normalize scores to strings (keeps blanks as None)
    def _norm(v):
        if v is None:
            return None
        s = str(v).strip()
        return s if s != "" else None

    # =========================
    # Build metadata (raw_meta) — 1:1 mirror of Wix submission
    # Keep keys stable (snake_case) to avoid future confusion
    # =========================
    meta = {
        # identity
        "owner_id": owner_id or None,
        "player_id": str(player_id) if player_id is not None else None,

        # people
        "customer_name": player_name or owner_id or None,
        "player_a_name": player_name or None,
        "player_b_name": opponent_name or None,

        # UTRs
        "player_a_utr": _norm(player_utr),
        "player_b_utr": _norm(opponent_utr),

        # match info
        "match_date": _norm(match_date),
        "start_time": _norm(start_time),
        "end_time": _norm(end_time),
        "location": location or None,
        "first_server": first_server or None,

        # score (as submitted)
        "score": {
            "set1": {"a": _norm(set1A), "b": _norm(set1B)},
            "set2": {"a": _norm(set2A), "b": _norm(set2B)},
            "set3": {"a": _norm(set3A), "b": _norm(set3B)},
        },

        # optional: keep the exact Wix field names too for perfect “diffing”
        # (helps reconciliation if Wix changes display names)
        "wix_payload": {k: body.get(k) for k in [
            "ownerId","playerId","playerName","opponentName","opponentUtr",
            "playerUTR","startTime","endTime","location","firstServer","matchDate",
            "Set 1 (A)","Set 2 (A)","Set 3 (A)","Set 1 (B)","Set 2 (B)","Set 3 (B)",
        ] if k in body}
    }

    # =========================
    # Existing behavior continues below (no logic changes):
    # - download from source_url
    # - upload to S3
    # - submit S3 URL to SportAI
    # - store submission_context
    # =========================
    try:
        _require_s3()

        resp = requests.get(source_url, stream=True, timeout=600)
        resp.raise_for_status()

        parsed = urlparse(source_url)
        filename = os.path.basename(parsed.path) or "video.mp4"
        clean_name = secure_filename(filename) or "video.mp4"
        ts = int(time.time())
        key = f"{S3_PREFIX}/{ts}_{clean_name}"

        meta_up = _s3_put_fileobj(
            resp.raw,
            key,
            content_type=resp.headers.get("Content-Type") or "video/mp4",
        )
        s3_video_url = _s3_presigned_get_url(key)

        email = ""  # Wix flow: email can be added later if you decide to pass it
        task_id = _sportai_submit(s3_video_url, email=email, meta=meta)

        _store_submission_context(
            task_id,
            email,
            meta,
            s3_video_url,
            share_url=source_url,  # original Wix download URL
        )

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            _set_status_cache(conn, task_id, "queued", None)

        return jsonify({"ok": True, "task_id": task_id, "video_url": s3_video_url})

    except Exception as e:
        return jsonify({"ok": False, "error": f"SportAI submit failed: {e}"}), 502

# =======================================================
# Wix submit via S3 key (no URLs from Wix)
# POST /api/submit_s3_task
# body: { s3_key, ownerId, playerId, playerName, ...metadata... }
# =======================================================
@app.post("/api/submit_s3_task")
def api_submit_s3_task():
    if not request.is_json:
        return jsonify({"ok": False, "error": "JSON body required"}), 400

    body = request.get_json(silent=True) or {}
    s3_key = (body.get("s3_key") or body.get("key") or "").strip()
    if not s3_key:
        return jsonify({"ok": False, "error": "s3_key required"}), 400

    _require_s3()

    # build a presigned GET internally (Wix never sees it)
    s3_video_url = _s3_presigned_get_url(s3_key)
    # DEBUG: prove the S3 object exists + headers before SportAI sees it
    s3_head = _head_url(s3_video_url)

    # reuse your existing meta builder logic (same keys you just added)
    owner_id      = (body.get("ownerId") or "").strip()
    player_id     = body.get("playerId")
    player_name   = (body.get("playerName") or "").strip()
    opponent_name = (body.get("opponentName") or "").strip()
    opponent_utr  = (body.get("opponentUtr") or "").strip()
    player_utr    = (body.get("playerUTR") or body.get("myUtr") or "").strip()
    start_time    = (body.get("startTime") or "").strip()
    end_time      = (body.get("endTime") or "").strip()
    match_date    = (body.get("matchDate") or "").strip()
    location      = (body.get("location") or "").strip()
    first_server  = (body.get("firstServer") or "").strip()

    def _norm(v):
        if v is None: return None
        s = str(v).strip()
        return s if s else None

    set1A = body.get("Set 1 (A)") if "Set 1 (A)" in body else body.get("set1A")
    set2A = body.get("Set 2 (A)") if "Set 2 (A)" in body else body.get("set2A")
    set3A = body.get("Set 3 (A)") if "Set 3 (A)" in body else body.get("set3A")
    set1B = body.get("Set 1 (B)") if "Set 1 (B)" in body else body.get("set1B")
    set2B = body.get("Set 2 (B)") if "Set 2 (B)" in body else body.get("set2B")
    set3B = body.get("Set 3 (B)") if "Set 3 (B)" in body else body.get("set3B")

    meta = {
        "owner_id": owner_id or None,
        "player_id": str(player_id) if player_id is not None else None,
        "customer_name": player_name or owner_id or None,
        "player_a_name": player_name or None,
        "player_b_name": opponent_name or None,
        "player_a_utr": _norm(player_utr),
        "player_b_utr": _norm(opponent_utr),
        "match_date": _norm(match_date),
        "start_time": _norm(start_time),
        "end_time": _norm(end_time),
        "location": location or None,
        "first_server": first_server or None,
        "score": {
            "set1": {"a": _norm(set1A), "b": _norm(set1B)},
            "set2": {"a": _norm(set2A), "b": _norm(set2B)},
            "set3": {"a": _norm(set3A), "b": _norm(set3B)},
        },
    }

    # accept email from Wix (preferred) or fallback field names
    email = (body.get("customer_email") or body.get("email") or "").strip().lower()

    # submit to SportAI using S3 presigned GET (Render-managed)
    task_id = _sportai_submit(s3_video_url, email=email, meta=meta)

    # store submission_context (video_url = s3 presigned GET, share_url = s3_key for traceability)
    _store_submission_context(task_id, email, meta, s3_video_url, share_url=s3_key)



    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        _set_status_cache(conn, task_id, "queued", None)

        return jsonify({
        "ok": True,
        "task_id": task_id,
        "debug": {
            "s3_key": s3_key,
            "s3_head": s3_head,
        }
    })


# ==========================
# LEGACY ALIAS (KEPT)
# ==========================
@app.route("/upload", methods=["POST", "OPTIONS"])
def upload_alias():
    if request.method == "OPTIONS":
        return ("", 204)
    return api_upload_to_s3()

# ==========================
# TASK POLL (NORMALIZED PROGRESS + AUTO-INGEST)
# ==========================
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
            _ensure_submission_context_schema(conn)
            _set_status_cache(conn, tid, status, result_url)
            sc = conn.execute(sql_text("""
                SELECT session_id,
                    ingest_started_at,
                    ingest_finished_at,
                    ingest_error,
                    wix_notified_at,
                    wix_notify_status,
                    wix_notify_error
                FROM bronze.submission_context
                WHERE task_id = :t
                LIMIT 1
            """), {"t": tid}).mappings().first() or {}

        auto_ingested = False
        auto_ingest_error = sc.get("ingest_error")
        session_id = sc.get("session_id")
        ingest_started = sc.get("ingest_started_at") is not None
        ingest_finished = sc.get("ingest_finished_at") is not None
        ingest_running = ingest_started and not ingest_finished

        if AUTO_INGEST_ON_COMPLETE and terminal and result_url and not session_id and not ingest_running:
            started = _start_ingest_background(tid, result_url)
            ingest_started = ingest_started or started

        if session_id and ingest_finished and not auto_ingest_error:
            auto_ingested = True

            if (not sc.get("wix_notified_at")) and (WIX_NOTIFY_URL and WIX_NOTIFY_KEY):
                try:
                    _notify_wix(tid, status="completed", session_id=session_id, result_url=result_url, error=None)
                except Exception as e:
                    app.logger.error("Wix notify retry from poller failed task_id=%s: %s", tid, e)

        return jsonify({
            "ok": True, **out,
            "session_id": session_id,
            "auto_ingested": auto_ingested,
            "auto_ingest_error": auto_ingest_error,
            "ingest_started": ingest_started,
            "ingest_running": ingest_running,
            "ingest_finished": ingest_finished,
            "wix_notified_at": sc.get("wix_notified_at"),
            "wix_notify_status": sc.get("wix_notify_status"),
            "wix_notify_error": sc.get("wix_notify_error")
        })

    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 200

# ==========================
# MANUAL INGEST HELPER (TASK_ID-ONLY)
# ==========================
@app.post("/ops/ingest-task")
def ops_ingest_task():
    if not _guard():
        return Response("Forbidden", 403)

    body = request.get_json(silent=True) or {}
    tid = (body.get("task_id") or "").strip()
    if not tid:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    try:
        st = _sportai_status(tid)
        result_url = st.get("result_url")
        if not result_url:
            return jsonify({"ok": False, "error": "result_url not available yet"}), 400

        r = requests.get(result_url, timeout=300)
        r.raise_for_status()
        payload = r.json()

        with engine.begin() as conn:
            _run_bronze_init(conn)
            res = ingest_bronze_strict(
                conn,
                payload,
                replace=DEFAULT_REPLACE_ON_INGEST,
                src_hint=result_url,
                task_id=tid,
            )
            sid = res.get("session_id")

            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET session_id        = :sid,
                       ingest_started_at = COALESCE(ingest_started_at, now()),
                       ingest_finished_at= now(),
                       ingest_error      = NULL,
                       last_status       = COALESCE(last_status, 'completed'),
                       last_status_at    = COALESCE(last_status_at, now())
                 WHERE task_id = :tid
            """), {"sid": sid, "tid": tid})

            _mirror_submission_to_bronze_by_task(conn, tid)

        try:
            build_silver_point_detail(task_id=tid, phase="all", replace=True)
        except Exception as e:
            app.logger.error(f"Silver build failed for task_id={tid}: {e}")

        # Optional: notify on manual ingest too (kept OFF by default; uncomment if desired)
        # try:
        #     _notify_wix(tid, status="completed", session_id=sid, result_url=result_url, error=None)
        # except Exception as e:
        #     app.logger.exception("Wix notify failed (manual completed) task_id=%s: %s", tid, e)

        return jsonify({"ok": True, "task_id": tid, "session_id": sid})

    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

# ==========================
# SQL HELPERS FOR QUICK INSPECTION
# ==========================
@app.post("/ops/sqlx")
def ops_sql_json():
    if not _guard():
        return Response("Forbidden", 403)
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(_sql_exec_to_json(body.get("q", "")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.get("/ops/sqlq")
def ops_sql_qs():
    if not _guard():
        return Response("Forbidden", 403)
    try:
        return jsonify(_sql_exec_to_json(request.args.get("q", "")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# ==========================
# OPTIONAL UI BLUEPRINT (IF PRESENT)
# ==========================
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# ==========================
# BILLING API BLUEPRINT
# (Left untouched as requested.)
# ==========================
try:
    app.register_blueprint(billing_bp)  # billing_bp already has url_prefix="/api/billing"
    print("Mounted billing_bp at /api/billing")
except Exception as e:
    print("billing_bp not mounted:", e)

# ==========================
# BOOT LOG
# ==========================
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
