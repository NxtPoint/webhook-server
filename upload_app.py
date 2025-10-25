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
SPORTAI_BASE        = os.getenv("SPORT_AI_BASE", "https://api.sportai.com").strip().rstrip("/")
SPORTAI_SUBMIT_PATH = os.getenv("SPORT_AI_SUBMIT_PATH", "/api/statistics/tennis").strip()
SPORTAI_STATUS_PATH = os.getenv("SPORT_AI_STATUS_PATH", "/api/statistics/tennis/{task_id}/status").strip()
SPORTAI_TOKEN       = os.getenv("SPORT_AI_TOKEN", "").strip()
SPORTAI_CHECK_PATH  = os.getenv("SPORT_AI_CHECK_PATH",  "/api/videos/check").strip()
SPORTAI_CANCEL_PATH = os.getenv("SPORT_AI_CANCEL_PATH", "/api/tasks/{task_id}/cancel").strip()

# keep auto-ingest ON for one-and-done UX
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
from db_init import engine                                  # noqa: E402
from ingest_app import ingest_bp
app.register_blueprint(ingest_bp, url_prefix="")

from ingest_bronze import ingest_bronze, ingest_bronze_strict, _run_bronze_init
app.register_blueprint(ingest_bronze, url_prefix="")


# ---------- S3 config (MANDATORY) ----------
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
            # Mirror public.submission_context → bronze.submission_context (1 row per session)
            conn.execute(sql_text("""
                INSERT INTO bronze.submission_context (session_id, data)
                SELECT
                  sc.session_id,
                  to_jsonb(sc.*)
                    - 'ingest_error' - 'ingest_started_at' - 'ingest_finished_at'
                    - 'last_status'  - 'last_status_at'    - 'last_result_url'
                FROM submission_context sc
                WHERE sc.task_id = :t AND sc.session_id = :sid
                ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
            """), {"t": task_id, "sid": sid})

        # fetch result and ingest
        r = requests.get(result_url, timeout=600)   # allow long fetch
        r.raise_for_status()
        payload = r.json()

        with engine.begin() as conn:
            _run_bronze_init(conn)  # ensure bronze schema/tables exist
            res = ingest_bronze_strict(
                conn,
                payload,
                replace=DEFAULT_REPLACE_ON_INGEST,
                forced_uid=None,
                src_hint=result_url,
            )
            sid = res["session_id"]
            conn.execute(sql_text("""
                UPDATE submission_context
                SET session_id = :sid,
                    ingest_finished_at = now(),
                    ingest_error = NULL
                WHERE task_id = :t
            """), {"sid": sid, "t": task_id})
        return jsonify({"ok": True, "session_id": sid})


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

@app.post("/ops/purge-sessions")
def ops_purge_sessions():
    if not _guard(): return jsonify({"ok": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "").lower()              # "keep_only" | "delete_list"
    uids = data.get("session_uids") or []
    if mode not in ("keep_only", "delete_list"):
        return jsonify({"ok": False, "error": "mode must be keep_only or delete_list"}), 400
    if not isinstance(uids, list) or not all(isinstance(x, str) and x for x in uids):
        return jsonify({"ok": False, "error": "session_uids must be a non-empty list of strings"}), 400
    with engine.begin() as conn:
        if mode == "keep_only":
            bad_ids = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid NOT IN :uids"),
                                   {"uids": tuple(uids)}).scalars().all()
        else:
            bad_ids = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid IN :uids"),
                                   {"uids": tuple(uids)}).scalars().all()
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
                counts[t] = f"skipped:{e.__class__.__name__}"
        res = conn.execute(sql_text("DELETE FROM dim_session WHERE session_id = ANY(:ids)"), {"ids": bad_ids})
        counts["dim_session"] = res.rowcount
    return jsonify({"ok": True, "deleted": counts, "target_session_ids": bad_ids}), 200

@app.after_request
def _maybe_cors(resp):
    resp.headers["Cache-Control"] = "no-store"
    if ENABLE_CORS:
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-OPS-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

# ---------- S3 helpers ----------
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

# ---------- DB helpers ----------
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
          session_id      INT,
          last_status     TEXT,
          last_status_at  TIMESTAMPTZ,
          last_result_url TEXT,
          ingest_started_at  TIMESTAMPTZ,
          ingest_finished_at TIMESTAMPTZ,
          ingest_error       TEXT
        );
    """))
    # Make sure new columns exist in older DBs
    for col, ddl in [
        ("ingest_started_at",  "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_started_at  TIMESTAMPTZ"),
        ("ingest_finished_at", "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ"),
        ("ingest_error",       "ALTER TABLE submission_context ADD COLUMN IF NOT EXISTS ingest_error       TEXT")
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

def _get_status_cache(conn, task_id: str):
    return conn.execute(sql_text("""
        SELECT last_status, last_status_at, last_result_url
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
    if j is None: raise RuntimeError(f"SportAI status failed: {last_err}")

    d = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), dict) else j
    status = (d.get("status") or d.get("task_status") or "").strip()
    msg = (j.get("message") or "").lower()
    if not status and "still being processed" in msg:
        status = "processing"

    # normalize progress
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
# Public endpoints
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
        "USE_MV_POINT": USE_MV_POINT,
    })

# ---------- (Optional) presign to remove double hop ----------
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
        try:
            f.stream.seek(0)
        except Exception:
            pass
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

# ---------- Upload API (S3 only) ----------
@app.route("/upload/api/upload", methods=["POST", "OPTIONS"])
def api_upload_to_s3():
    if request.method == "OPTIONS": return ("", 204)
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
    if not f or not f.filename: return jsonify({"ok": False, "error": "No file provided."}), 400

    clean = secure_filename(f.filename); ts = int(time.time()); key = f"{S3_PREFIX}/{ts}_{clean}"
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

# Legacy alias (kept)
@app.route("/upload", methods=["POST", "OPTIONS"])
def upload_alias():
    if request.method == "OPTIONS": return ("", 204)
    return api_upload_to_s3()

# ---------- Task poll (normalized progress + auto-ingest; no MV refresh) ----------
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
            started = _start_ingest_background(tid, result_url)
            ingest_started = ingest_started or started

        # if the worker finished and wrote session_id, reflect that as "auto_ingested"
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
        # never break the poller
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 200

# ---------- Manual ingest helper ----------
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
            _run_bronze_init(conn)
            res = ingest_bronze_strict(
                conn,
                payload,
                replace=DEFAULT_REPLACE_ON_INGEST,
                forced_uid=None,
                src_hint=result_url,
            )
            sid = res["session_id"]
            conn.execute(sql_text(
                "UPDATE submission_context SET session_id=:sid WHERE task_id=:t"
            ), {"sid": sid, "t": tid})
            conn.execute(sql_text("""
                INSERT INTO bronze.submission_context (session_id, data)
                SELECT
                  sc.session_id,
                  to_jsonb(sc.*)
                    - 'ingest_error' - 'ingest_started_at' - 'ingest_finished_at'
                    - 'last_status'  - 'last_status_at'    - 'last_result_url'
                FROM submission_context sc
                WHERE sc.task_id = :t AND sc.session_id = :sid
                ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
            """), {"t": tid, "sid": sid})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

# ---------- Webhook route kept (unused) ----------
@app.post("/ops/sportai-callback")
def ops_sportai_callback():
    if not _guard(): return Response("Forbidden", 403)
    try:
        body = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400

    forced_uid = request.args.get("session_uid") or (
        body.get("session_uid") or body.get("sessionId") or body.get("session_id") or body.get("uid") or body.get("id")
    )
    task_id    = body.get("task_id") or body.get("id")
    result_url = body.get("result_url") or (body.get("data") or {}).get("result_url")

    payload = None; src_hint = None
    if isinstance(body, dict) and any(k in body for k in ("players","swings","ball_positions","player_positions","ball_bounces","rallies")):
        payload, src_hint = body, "webhook:body"
    else:
        try:
            if not result_url and task_id:
                st = _sportai_status(task_id); result_url = st.get("result_url")
            if result_url:
                r = requests.get(result_url, timeout=300); r.raise_for_status()
                payload, src_hint = r.json(), (result_url or "webhook:get")
        except Exception:
            payload, src_hint = None, None

    if payload is None:
        return jsonify({"ok": True, "ingested": False, "reason": "no payload/result_url yet", "task_id": task_id}), 200

    try:
        with engine.begin() as conn:
            _run_bronze_init(conn)
            res = ingest_bronze_strict(
                conn,
                payload,
                replace=DEFAULT_REPLACE_ON_INGEST,
                forced_uid=forced_uid,
                src_hint=src_hint,
            )
            sid = res.get("session_id")
            if task_id:
                conn.execute(sql_text(
                    "UPDATE submission_context SET session_id=:sid WHERE task_id=:t"
                ), {"sid": sid, "t": task_id})
            if task_id:
                conn.execute(sql_text("""
                    INSERT INTO bronze.submission_context (session_id, data)
                    SELECT
                      sc.session_id,
                      to_jsonb(sc.*)
                        - 'ingest_error' - 'ingest_started_at' - 'ingest_finished_at'
                        - 'last_status'  - 'last_status_at'    - 'last_result_url'
                    FROM submission_context sc
                    WHERE sc.task_id = :t AND sc.session_id = :sid
                    ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
                """), {"t": task_id, "sid": sid})

            counts = conn.execute(sql_text("""
                SELECT
                (SELECT COUNT(*) FROM bronze.rally           WHERE session_id=:sid),
                (SELECT COUNT(*) FROM bronze.ball_bounce     WHERE session_id=:sid),
                (SELECT COUNT(*) FROM bronze.ball_position   WHERE session_id=:sid),
                (SELECT COUNT(*) FROM bronze.player_position WHERE session_id=:sid),
                (SELECT COUNT(*) FROM bronze.swing           WHERE session_id=:sid)
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