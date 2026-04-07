# upload_app.py — Clean S3 → SportAI → Bronze (task_id-only)
# - Keeps: S3 upload, SportAI submit/status/cancel, presign, check-video
# - On status=completed: fetch result_url JSON and ingest via ingest_bronze_strict (task_id-only)
# - Uses bronze.submission_context keyed by task_id (no public schema)

import os, json, time, socket, sys, inspect, hashlib, re, threading, subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, Response
from werkzeug.utils import secure_filename
from sqlalchemy import text as sql_text
from video_pipeline.video_trim_api import trigger_video_trim


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

from coaches_api import bp as coaches_bp
app.register_blueprint(coaches_bp)

from members_api import members_bp
from subscriptions_api import subscriptions_bp
from usage_api import usage_bp
from entitlements_api import entitlements_bp
from client_api import client_bp

app.register_blueprint(members_bp)
app.register_blueprint(subscriptions_bp)
app.register_blueprint(usage_bp)
app.register_blueprint(entitlements_bp)
app.register_blueprint(client_bp)

# Manual CORS for /api/client/* (more reliable than flask-cors with blueprints)
@app.after_request
def add_cors_headers(response):
    if request.path.startswith("/api/client/") or request.path.startswith("/upload/api/") or request.path.startswith("/api/submit_s3_task"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Client-Key, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    return response


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
SPORTAI_STATUS_PATHS = [
    "/api/statistics/tennis/{task_id}/status",
]

# ---------- DB engine / bronze ingest ----------
from db_init import engine  # noqa: E402
from ingest_bronze import ingest_bronze, ingest_bronze_strict, _run_bronze_init  # noqa: E402
from build_silver_v2 import build_silver_v2 as build_silver_point_detail, DEFAULT_SPORT_TYPE  # noqa: E402
from billing_import_from_bronze import sync_usage_for_task_id  # noqa: E402
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


# ---------- Ingest worker service ----------
INGEST_WORKER_BASE_URL = (os.getenv("INGEST_WORKER_BASE_URL") or "").strip().rstrip("/")
INGEST_WORKER_OPS_KEY = (os.getenv("INGEST_WORKER_OPS_KEY") or "").strip()
INGEST_WORKER_TIMEOUT_S = int(os.getenv("INGEST_WORKER_TIMEOUT_S", "10"))

# ---------- Power BI service ----------
PBI_SERVICE_BASE = (os.getenv("POWERBI_SERVICE_BASE_URL") or "").strip().rstrip("/")
PBI_SERVICE_OPS_KEY = (os.getenv("POWERBI_SERVICE_OPS_KEY") or OPS_KEY or "").strip()
PBI_REFRESH_POLL_S = int(os.getenv("PBI_REFRESH_POLL_S", "15"))
PBI_REFRESH_MAX_WAIT_S = int(os.getenv("PBI_REFRESH_MAX_WAIT_S", "1800"))
PBI_REFRESH_TRIGGER_TIMEOUT_S = int(os.getenv("PBI_REFRESH_TRIGGER_TIMEOUT_S", "60"))
PBI_REFRESH_STATUS_TIMEOUT_S = int(os.getenv("PBI_REFRESH_STATUS_TIMEOUT_S", "60"))
PBI_SUSPEND_AFTER_REFRESH = os.getenv("PBI_SUSPEND_AFTER_REFRESH", "1").lower() in ("1","true","yes","y")
INGEST_STALE_AFTER_S = int(os.getenv("INGEST_STALE_AFTER_S", "1800"))  # 30 min

# ==========================
# HELPERS
# ==========================
def _guard() -> bool:
    """
    Header-only ops auth.
    Prevents OPS_KEY leakage into access logs via query strings.
    Accepted headers:
      - X-Ops-Key
      - X-OPS-Key
      - Authorization: Bearer <key>
    """
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()

    supplied = (hk or "").strip()
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

def _pbi_headers():
    if not PBI_SERVICE_BASE:
        raise RuntimeError("POWERBI_SERVICE_BASE_URL not set")
    if not PBI_SERVICE_OPS_KEY:
        raise RuntimeError("POWERBI_SERVICE_OPS_KEY/OPS_KEY not set")
    return {
        "Content-Type": "application/json",
        "x-ops-key": PBI_SERVICE_OPS_KEY,
    }


def _pbi_post(path: str, body: dict | None = None, timeout: int = 60) -> dict:
    url = f"{PBI_SERVICE_BASE}{path}"
    r = requests.post(url, headers=_pbi_headers(), json=(body or {}), timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"PBI POST failed {path}: HTTP {r.status_code}: {r.text}")
    return r.json() if r.text else {}

def _pbi_get(path: str, timeout: int = 60) -> dict:
    url = f"{PBI_SERVICE_BASE}{path}"
    r = requests.get(url, headers=_pbi_headers(), timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"PBI GET failed {path}: HTTP {r.status_code}: {r.text}")
    return r.json() if r.text else {}

def _poll_powerbi_refresh_until_terminal(task_id: str) -> dict:
    """
    Async-safe refresh orchestration:
    - trigger refresh once
    - poll latest status in short-lived HTTP calls
    - persist state changes into bronze.submission_context
    - always attempt suspend after terminal state
    """
    trigger_out = _pbi_post(
        "/dataset/refresh_once",
        {"task_id": task_id},
        timeout=PBI_REFRESH_TRIGGER_TIMEOUT_S,
    )
    trigger_started_at_epoch = time.time()

    app.logger.info("PBI refresh trigger accepted task_id=%s out=%s", task_id, {
        "ok": trigger_out.get("ok"),
        "accepted": trigger_out.get("accepted"),
        "status": trigger_out.get("status"),
        "triggered_at": trigger_out.get("triggered_at"),
    })

    poll_started_at = time.time()
    deadline = poll_started_at + PBI_REFRESH_MAX_WAIT_S
    last_status = None
    last_out = None

    try:
        while time.time() < deadline:
            out = _pbi_get("/dataset/refresh_status", timeout=PBI_REFRESH_STATUS_TIMEOUT_S) or {}
            last_out = out

            status = str(out.get("status") or "").strip().lower()
            is_terminal = bool(out.get("is_terminal"))
            error_message = (out.get("error_message") or "").strip() or None
            started_at_raw = out.get("started_at")

            started_at_epoch = None
            if started_at_raw:
                try:
                    started_at_epoch = datetime.fromisoformat(
                        str(started_at_raw).replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    started_at_epoch = None

            if started_at_epoch is not None and started_at_epoch < (trigger_started_at_epoch - 10):
                time.sleep(3)
                continue

            should_persist = (
                status != last_status
                or is_terminal
                or bool(error_message)
            )

            if should_persist:
                with engine.begin() as conn:
                    _ensure_submission_context_schema(conn)
                    _set_pbi_refresh_state(
                        conn,
                        task_id,
                        status=status or "unknown",
                        error=error_message,
                        started=True,
                        finished=is_terminal,
                        clear_error=not error_message,
                    )

            if status != last_status:
                app.logger.info(
                    "PBI refresh status change task_id=%s status=%s terminal=%s",
                    task_id, status, is_terminal
                )
                last_status = status

            if is_terminal:
                return {
                    "ok": bool(out.get("is_success") is True),
                    "status": status,
                    "terminal": True,
                    "error": error_message,
                    "raw": out,
                }

            elapsed = time.time() - poll_started_at

            if elapsed < 15:
                sleep_s = 2
            elif elapsed < 30:
                sleep_s = 3
            elif elapsed < 60:
                sleep_s = 5
            else:
                sleep_s = max(8, PBI_REFRESH_POLL_S)

            time.sleep(sleep_s)

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            _set_pbi_refresh_state(
                conn,
                task_id,
                status="timeout",
                error=f"refresh_timeout after {PBI_REFRESH_MAX_WAIT_S}s",
                started=True,
                finished=True,
                clear_error=False,
            )

        return {
            "ok": False,
            "status": "timeout",
            "terminal": True,
            "error": f"refresh_timeout after {PBI_REFRESH_MAX_WAIT_S}s",
            "raw": last_out,
        }

    finally:
        if PBI_SUSPEND_AFTER_REFRESH:
            try:
                _pbi_post("/capacity/suspend", {}, timeout=60)
                app.logger.info("PBI capacity suspend ok task_id=%s", task_id)
            except Exception as e:
                app.logger.exception("PBI capacity suspend failed task_id=%s: %s", task_id, e)

# ==========================
# BILLING + ROLE GATE (RENDER SSoT)
# ==========================

def _upload_entitlement_gate(email: str) -> tuple[bool, str]:
    e = (email or "").strip().lower()
    if not e:
        return False, "email_required"

    with engine.begin() as conn:
        row = conn.execute(sql_text("""
            SELECT
              a.id AS account_id,
              COALESCE(m.role, 'player_parent') AS role,
              COALESCE(v.matches_remaining, 0)  AS matches_remaining
            FROM billing.account a
            LEFT JOIN billing.member m
              ON m.account_id = a.id AND m.is_primary = true
            LEFT JOIN billing.vw_customer_usage v
              ON v.account_id = a.id
            WHERE a.email = :email
            LIMIT 1
        """), {"email": e}).mappings().first()

        if not row:
            return False, "account_not_found"

        role = (row.get("role") or "player_parent").strip().lower()
        remaining = int(row.get("matches_remaining") or 0)
        account_id = int(row.get("account_id"))

        try:
            sub = conn.execute(sql_text("""
                SELECT COALESCE(status, 'NONE') AS subscription_status
                FROM billing.subscription_state
                WHERE account_id = :account_id
                LIMIT 1
            """), {"account_id": account_id}).mappings().first()
        except Exception:
            return False, "subscription_state_unavailable"

    subscription_status = str((sub or {}).get("subscription_status") or "NONE").strip().upper()

    if role == "coach":
        return False, "coach_cannot_upload"
    if subscription_status != "ACTIVE":
        return False, "subscription_inactive"
    if remaining <= 0:
        return False, "insufficient_credits"

    return True, "ok"


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

          -- Power BI refresh audit
          pbi_refresh_started_at  TIMESTAMPTZ,
          pbi_refresh_finished_at TIMESTAMPTZ,
          pbi_refresh_status      TEXT,
          pbi_refresh_error       TEXT,
                                          
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
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS pbi_refresh_started_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS pbi_refresh_finished_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS pbi_refresh_status TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS pbi_refresh_error TEXT",

        # --- NEW: typed score + timing + SR fields (idempotent) ---
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set1_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set1_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set2_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set2_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set3_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set3_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS first_server TEXT",
        f"ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS sport_type TEXT DEFAULT '{DEFAULT_SPORT_TYPE}'",
    ):
        conn.execute(sql_text(ddl))


def _as_int(x):
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s == "":
            return None
        return int(s)
    except Exception:
        return None


def _as_ts_from_text(v):
    """
    Accepts:
      - None / "" -> None
      - ISO datetime string -> TIMESTAMPTZ-ready string
      - "HH:MM" or "HH:MM:SS" -> NOT a true timestamp without a date => returns None
    Conservative: only parse full ISO-like values; else None.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # Only accept ISO-ish timestamps; otherwise return None
    if "T" in s:
        return s
    return None


def _norm_first_server(v):
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("S", "R"):
        return s
    return None


def _store_submission_context(
    task_id: str,
    email: str,
    meta: dict | None,
    video_url: str,
    share_url: str | None = None,
    s3_bucket: str | None = None,
    s3_key: str | None = None,
):
    if not engine:
        return

    m = meta or {}

    # ---------------------------
    # Extract scores from meta["score"]
    # ---------------------------
    score = m.get("score") if isinstance(m.get("score"), dict) else {}
    set1 = score.get("set1") if isinstance(score.get("set1"), dict) else {}
    set2 = score.get("set2") if isinstance(score.get("set2"), dict) else {}
    set3 = score.get("set3") if isinstance(score.get("set3"), dict) else {}

    a1 = _as_int(set1.get("a"))
    b1 = _as_int(set1.get("b"))
    a2 = _as_int(set2.get("a"))
    b2 = _as_int(set2.get("b"))
    a3 = _as_int(set3.get("a"))
    b3 = _as_int(set3.get("b"))

    # ---------------------------
    # Extract first_server + times
    # ---------------------------
    wix_payload = m.get("wix_payload") if isinstance(m.get("wix_payload"), dict) else {}

    first_server = _norm_first_server(m.get("first_server") or wix_payload.get("firstServer"))

    # Keep raw string (table has start_time TEXT — seconds from video start to first point)
    start_time_txt = m.get("start_time") or wix_payload.get("startTime") or None
    if isinstance(start_time_txt, str):
        start_time_txt = start_time_txt.strip() or None

    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)

        conn.execute(sql_text("""
            INSERT INTO bronze.submission_context (
              task_id, email, customer_name, match_date, start_time, location,
              player_a_name, player_b_name, player_a_utr, player_b_utr,
              video_url, share_url, raw_meta,
              s3_bucket, s3_key,

              player_a_set1_games, player_b_set1_games,
              player_a_set2_games, player_b_set2_games,
              player_a_set3_games, player_b_set3_games,
              first_server,
              sport_type
            ) VALUES (
              :task_id, :email, :customer_name, :match_date, :start_time, :location,
              :player_a_name, :player_b_name, :player_a_utr, :player_b_utr,
              :video_url, :share_url, :raw_meta,
              :s3_bucket, :s3_key,

              :a1, :b1,
              :a2, :b2,
              :a3, :b3,
              :first_server,
              :sport_type
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
              raw_meta=EXCLUDED.raw_meta,                            
              s3_bucket=EXCLUDED.s3_bucket,
              s3_key=EXCLUDED.s3_key,                

              player_a_set1_games=EXCLUDED.player_a_set1_games,
              player_b_set1_games=EXCLUDED.player_b_set1_games,
              player_a_set2_games=EXCLUDED.player_a_set2_games,
              player_b_set2_games=EXCLUDED.player_b_set2_games,
              player_a_set3_games=EXCLUDED.player_a_set3_games,
              player_b_set3_games=EXCLUDED.player_b_set3_games,
              first_server=EXCLUDED.first_server,
              sport_type=EXCLUDED.sport_type
        """), {
            "task_id": task_id,
            "email": email,
            "customer_name": m.get("customer_name"),
            "match_date": m.get("match_date"),
            "start_time": start_time_txt,
            "location": m.get("location"),
            "player_a_name": m.get("player_a_name") or "Player A",
            "player_b_name": m.get("player_b_name") or "Player B",
            "player_a_utr": m.get("player_a_utr"),
            "player_b_utr": m.get("player_b_utr"),
            "video_url": video_url,
            "share_url": share_url,
            "raw_meta": json.dumps(m),
            "s3_bucket": s3_bucket,
            "s3_key": s3_key,

            "a1": a1, "b1": b1,
            "a2": a2, "b2": b2,
            "a3": a3, "b3": b3,
            "first_server": first_server,
            "sport_type": DEFAULT_SPORT_TYPE,
        })


def _set_status_cache(conn, task_id: str, status: str | None, result_url: str | None):
    conn.execute(sql_text("""
        UPDATE bronze.submission_context
           SET last_status     = :s,
               last_status_at  = now(),
               last_result_url = :r
         WHERE task_id = :t
    """), {"t": task_id, "s": status, "r": result_url})

def _set_pbi_refresh_state(
    conn,
    task_id: str,
    status: str | None = None,
    error: str | None = None,
    started: bool = False,
    finished: bool = False,
    clear_error: bool = False,
):
    sets = []
    params = {"t": task_id}

    if started:
        sets.append("pbi_refresh_started_at = COALESCE(pbi_refresh_started_at, now())")
        if not finished:
            sets.append("pbi_refresh_finished_at = NULL")

    if finished:
        sets.append("pbi_refresh_finished_at = now()")

    if status is not None:
        sets.append("pbi_refresh_status = :s")
        params["s"] = status

    if clear_error:
        sets.append("pbi_refresh_error = NULL")
    elif error is not None:
        sets.append("pbi_refresh_error = :e")
        params["e"] = error

    if not sets:
        return

    conn.execute(sql_text(f"""
        UPDATE bronze.submission_context
           SET {", ".join(sets)}
         WHERE task_id = :t
    """), params)

def _get_pbi_refresh_state(conn, task_id: str) -> dict:
    row = conn.execute(sql_text("""
        SELECT
          pbi_refresh_started_at,
          pbi_refresh_finished_at,
          pbi_refresh_status,
          pbi_refresh_error
        FROM bronze.submission_context
        WHERE task_id = :t
        LIMIT 1
    """), {"t": task_id}).mappings().first() or {}

    return {
        "pbi_refresh_started_at": row.get("pbi_refresh_started_at"),
        "pbi_refresh_finished_at": row.get("pbi_refresh_finished_at"),
        "pbi_refresh_status": row.get("pbi_refresh_status"),
        "pbi_refresh_error": row.get("pbi_refresh_error"),
    }    

def _load_submission_context_row(task_id: str) -> dict:
    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        return conn.execute(sql_text("""
            SELECT
              session_id,
              last_status,
              last_result_url,
              ingest_started_at,
              ingest_finished_at,
              ingest_error,
              pbi_refresh_started_at,
              pbi_refresh_finished_at,
              pbi_refresh_status,
              pbi_refresh_error,
              wix_notified_at,
              wix_notify_status,
              wix_notify_error
            FROM bronze.submission_context
            WHERE task_id = :t
            LIMIT 1
        """), {"t": task_id}).mappings().first() or {}


def _resolve_result_url_for_task(task_id: str) -> str | None:
    """
    Resolve result_url in the safest order:
    1) fresh SportAI status lookup
    2) cached DB value
    """
    try:
        st = _sportai_status(task_id)
        fresh = str(st.get("result_url") or "").strip()
        if fresh:
            with engine.begin() as conn:
                _ensure_submission_context_schema(conn)
                _set_status_cache(conn, task_id, st.get("status"), fresh)
            return fresh
    except Exception:
        pass

    sc = _load_submission_context_row(task_id)
    cached = str(sc.get("last_result_url") or "").strip()
    if cached:
        return cached

    return None

def _derive_pipeline_stage(
    sportai_status: str | None,
    ingest_started: bool,
    ingest_finished: bool,
    ingest_error: str | None,
    pbi_refresh_started: bool,
    pbi_refresh_finished: bool,
    pbi_refresh_status: str | None,
    pbi_refresh_error: str | None,
    dashboard_ready: bool,
) -> str:
    s = _normalize_sportai_status(sportai_status)

    if s == "failed":
        return "failed"

    if s == "canceled":
        return "canceled"

    if dashboard_ready:
        return "complete"

    if ingest_error:
        return "failed"

    if pbi_refresh_started and not dashboard_ready:
        return "refreshing_dashboard"

    if ingest_started and not ingest_finished:
        return "building_analytics"

    if ingest_finished and not dashboard_ready:
        return "building_analytics"

    if s == "processing":
        return "match_analysis_in_progress"

    if s == "queued":
        return "queued_for_analysis"

    return "queued_for_analysis"


def _derive_display_progress_pct(
    sportai_progress_pct: int | None,
    pipeline_stage: str,
    dashboard_ready: bool,
) -> int:
    if dashboard_ready or pipeline_stage == "complete":
        return 100

    if pipeline_stage == "refreshing_dashboard":
        return 95

    if pipeline_stage == "building_analytics":
        return 90

    if pipeline_stage == "match_analysis_in_progress":
        if sportai_progress_pct is None:
            return 10
        return max(10, min(85, int(sportai_progress_pct)))

    if pipeline_stage == "queued_for_analysis":
        return 5

    if pipeline_stage in {"failed", "canceled"}:
        return int(sportai_progress_pct or 0)

    return int(sportai_progress_pct or 0)

def _mirror_submission_to_bronze_by_task(conn, task_id: str):
    """No-op: submission_context now lives directly in bronze.submission_context."""
    return

# ==========================
# WIX NOTIFY (RENDER → WIX → AUTOMATION)
# ==========================
def _wix_payload(task_id: str, status: str, session_id: str | None, result_url: str | None, error: str | None):
    # Wix automation trigger expects: customer_email, customer_name, task_id
    # We source email/name from bronze.submission_context for the task_id.
    customer_email = None
    customer_name = None

    try:
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            row = conn.execute(sql_text("""
                SELECT email, customer_name
                  FROM bronze.submission_context
                 WHERE task_id = :t
                 LIMIT 1
            """), {"t": task_id}).mappings().first()
            if row:
                customer_email = (row.get("email") or "").strip() or None
                customer_name = (row.get("customer_name") or "").strip() or None
    except Exception:
        pass

    return {
        "customer_email": customer_email,
        "customer_name": customer_name,
        "task_id": task_id,
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

def _normalize_sportai_status(v: str | None) -> str | None:
    s = str(v or "").strip().lower()
    if not s:
        return None

    mapping = {
        "queued": "queued",
        "pending": "queued",
        "submitted": "queued",

        "processing": "processing",
        "running": "processing",
        "in_progress": "processing",
        "inprogress": "processing",

        "completed": "completed",
        "done": "completed",
        "success": "completed",
        "succeeded": "completed",

        "failed": "failed",
        "failure": "failed",
        "error": "failed",

        "canceled": "canceled",
        "cancelled": "canceled",
    }
    return mapping.get(s, s)


def _is_success_terminal_status(status: str | None) -> bool:
    return _normalize_sportai_status(status) == "completed"


def _is_terminal_status(status: str | None) -> bool:
    return _normalize_sportai_status(status) in {"completed", "failed", "canceled"}


def _coerce_progress_pct(raw) -> int | None:
    try:
        if raw is None:
            return None
        v = float(raw)
        if 0 <= v <= 1:
            v = v * 100
        pct = int(round(v))
        return max(0, min(100, pct))
    except Exception:
        return None


def _first_non_null(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def _sportai_status(task_id: str) -> dict:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")

    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}
    last_err = None
    j = None

    # Canonical live-status endpoint only
    url = f"{SPORTAI_BASE.rstrip('/')}/api/statistics/tennis/{task_id}/status"

    try:
        r = requests.get(url, headers=headers, timeout=30)

        if r.status_code == 404:
            j = {"message": "Task not visible yet (404)."}
        else:
            r.raise_for_status()
            j = r.json() or {}

    except Exception as e:
        last_err = f"{url} -> {e}"

    if j is None:
        raise RuntimeError(f"SportAI status failed: {last_err}")

    root = j if isinstance(j, dict) else {}
    data = root.get("data") if isinstance(root.get("data"), dict) else {}

    # Use exactly what SportAI gives us
    raw_status = str(data.get("task_status") or root.get("task_status") or "").strip()
    raw_progress = data.get("task_progress")
    if raw_progress is None:
        raw_progress = root.get("task_progress")

    msg = str(root.get("message") or "").strip().lower()
    if not raw_status and "still being processed" in msg:
        raw_status = "processing"

    status = _normalize_sportai_status(raw_status)
    progress_pct = _coerce_progress_pct(raw_progress)

    result_url = _first_non_null(
        data.get("result_url"),
        data.get("resultUrl"),
        (data.get("result") or {}).get("url") if isinstance(data.get("result"), dict) else None,
        root.get("result_url"),
        root.get("resultUrl"),
        (root.get("result") or {}).get("url") if isinstance(root.get("result"), dict) else None,
    )

    # If status is complete but canonical endpoint has no result_url, resolve it separately
    if _is_success_terminal_status(status) and not result_url:
        result_url = _sportai_result_url(task_id)

    terminal = _is_terminal_status(status)

    if _is_success_terminal_status(status) and (progress_pct is None or progress_pct < 100):
        progress_pct = 100

    if progress_pct is None or not status:
        app.logger.info(
            "SPORTAI STATUS PARSE task_id=%s status=%s progress=%s keys_root=%s keys_data=%s",
            task_id,
            status,
            progress_pct,
            sorted(list(root.keys()))[:20],
            sorted(list(data.keys()))[:20],
        )

    return {
        "task_id": task_id,
        "status": status,
        "sportai_status": status,
        "sportai_status_raw": raw_status or None,
        "result_url": result_url,
        "sportai_progress_pct": progress_pct,
        "terminal": terminal,
        "success_terminal": _is_success_terminal_status(status),
        "data": {
            "task_id": data.get("task_id"),
            "video_url": data.get("video_url"),
            "task_status": data.get("task_status"),
            "task_progress": data.get("task_progress"),
            "total_subtask_progress": data.get("total_subtask_progress"),
            "subtask_progress": data.get("subtask_progress") or {},
        },
    }

def _sportai_result_url(task_id: str) -> str | None:
    """
    Resolve SportAI result_url from the broader result/status endpoints.

    We keep live progress/status parsing simple and explicit via the canonical
    /status endpoint, but result_url may only appear on alternate endpoints.
    """
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")

    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}

    candidate_paths = [
        "/api/statistics/tennis/{task_id}",
        "/api/statistics/{task_id}",
        "/api/tasks/{task_id}",
        "/api/statistics/tennis/{task_id}/status",
    ]

    for base in SPORTAI_BASES:
        for path in candidate_paths:
            url = f"{base.rstrip('/')}/{path.lstrip('/').format(task_id=task_id)}"
            try:
                r = requests.get(url, headers=headers, timeout=30)

                if r.status_code == 404:
                    continue
                if r.status_code >= 500:
                    continue

                r.raise_for_status()
                root = r.json() or {}
                if not isinstance(root, dict):
                    continue

                data = root.get("data") if isinstance(root.get("data"), dict) else {}

                result_url = _first_non_null(
                    data.get("result_url"),
                    data.get("resultUrl"),
                    (data.get("result") or {}).get("url") if isinstance(data.get("result"), dict) else None,
                    root.get("result_url"),
                    root.get("resultUrl"),
                    (root.get("result") or {}).get("url") if isinstance(root.get("result"), dict) else None,
                )

                if result_url:
                    app.logger.info("SPORTAI RESULT URL FOUND task_id=%s url=%s", task_id, url)
                    return str(result_url).strip()

            except Exception:
                continue

    return None

def _sportai_check(video_url: str) -> dict:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")

    url = f"{SPORTAI_BASE.rstrip('/')}/{SPORTAI_CHECK_PATH.lstrip('/')}"
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"}

    # SportAI docs: expects POST with video_urls[]
    payload = {"video_urls": [video_url], "version": "latest"}

    r = requests.post(url, headers=headers, json=payload, timeout=60)

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
    if content_type:
        cli.upload_fileobj(fobj, S3_BUCKET, key, ExtraArgs={"ContentType": content_type})
    else:
        cli.upload_fileobj(fobj, S3_BUCKET, key)

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

def _s3_head_object(key: str) -> dict:
    cli = _s3_client()
    out = cli.head_object(Bucket=S3_BUCKET, Key=key)
    return {
        "content_length": int(out.get("ContentLength") or 0),
        "content_type": str(out.get("ContentType") or "").strip(),
        "etag": out.get("ETag"),
    }

def _validate_uploaded_s3_object_for_submit(key: str) -> tuple[bool, str | None, dict | None]:
    try:
        meta = _s3_head_object(key)
    except Exception as e:
        return False, f"s3_object_not_found: {e}", None

    size = int(meta.get("content_length") or 0)
    ctype = str(meta.get("content_type") or "").lower().strip()

    if size <= 0:
        return False, "s3_object_empty", meta

    allowed_ctypes = {
        "video/mp4",
        "video/quicktime",
        "video/x-m4v",
        "video/mpeg",
    }

    if ctype and ctype not in allowed_ctypes and not ctype.startswith("video/"):
        return False, f"invalid_s3_content_type:{ctype}", meta

    max_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024 * 1024)))
    if size > max_bytes:
        return False, f"s3_object_exceeds_max_upload_bytes:{size}", meta

    return True, None, meta

# ==========================
# S3 MULTIPART HELPERS
# ==========================
MULTIPART_PART_SIZE_MB = int(os.getenv("MULTIPART_PART_SIZE_MB", "25"))
MULTIPART_PART_SIZE = MULTIPART_PART_SIZE_MB * 1024 * 1024

def _s3_create_multipart_upload(key: str, content_type: str | None = None) -> dict:
    cli = _s3_client()
    kwargs = {
        "Bucket": S3_BUCKET,
        "Key": key,
    }
    if content_type:
        kwargs["ContentType"] = content_type
    return cli.create_multipart_upload(**kwargs)

def _s3_presign_upload_part(key: str, upload_id: str, part_number: int, expires: int = 3600) -> str:
    cli = _s3_client()
    return cli.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": S3_BUCKET,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": int(part_number),
        },
        ExpiresIn=int(expires),
    )

def _s3_complete_multipart_upload(key: str, upload_id: str, parts: list[dict]) -> dict:
    cli = _s3_client()
    return cli.complete_multipart_upload(
        Bucket=S3_BUCKET,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )

def _s3_abort_multipart_upload(key: str, upload_id: str) -> dict:
    cli = _s3_client()
    return cli.abort_multipart_upload(
        Bucket=S3_BUCKET,
        Key=key,
        UploadId=upload_id,
    )

def _s3_list_multipart_parts(key: str, upload_id: str) -> list[dict]:
    cli = _s3_client()

    parts = []
    kwargs = {
        "Bucket": S3_BUCKET,
        "Key": key,
        "UploadId": upload_id,
    }

    while True:
        out = cli.list_parts(**kwargs)
        batch = out.get("Parts") or []

        for p in batch:
            parts.append({
                "PartNumber": int(p["PartNumber"]),
                "ETag": str(p["ETag"]),
                "Size": int(p.get("Size") or 0),
            })

        if not out.get("IsTruncated"):
            break

        kwargs["PartNumberMarker"] = out.get("NextPartNumberMarker")

    parts.sort(key=lambda x: x["PartNumber"])
    return parts


# ==========================
# INGEST WORKER (TASK_ID-ONLY)
# ==========================
def _do_ingest(task_id: str, result_url: str) -> bool:
    sid = None

    try:
        app.logger.info("INGEST START task_id=%s result_url=%s", task_id, result_url)

        # mark started
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_started_at = COALESCE(ingest_started_at, now()),
                       ingest_finished_at = NULL,
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"t": task_id})

        # -------------------------
        # STEP 1: DOWNLOAD RESULT JSON
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=download_result_start", task_id)

        import gzip

        r = requests.get(result_url, timeout=900, stream=True)
        r.raise_for_status()

        content_encoding = (r.headers.get("Content-Encoding") or "").lower().strip()
        content_length = r.headers.get("Content-Length")
        content_type = r.headers.get("Content-Type")

        app.logger.info(
            "INGEST STEP task_id=%s step=download_result_headers status=%s content_length=%s content_type=%s content_encoding=%s",
            task_id,
            r.status_code,
            content_length,
            content_type,
            content_encoding,
        )

        if "gzip" in content_encoding:
            payload = json.load(gzip.GzipFile(fileobj=r.raw))
        else:
            payload = json.load(r.raw)

        app.logger.info("INGEST STEP task_id=%s step=download_result_done", task_id)

        # -------------------------
        # STEP 2: BRONZE INGEST
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=bronze_ingest_start", task_id)

        with engine.begin() as conn:
            _run_bronze_init(conn)
            res = ingest_bronze_strict(
                conn,
                payload,
                replace=DEFAULT_REPLACE_ON_INGEST,
                src_hint=result_url,
                task_id=task_id,
            )
            sid = res.get("session_id")

            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET session_id         = :sid,
                       ingest_error       = NULL,
                       last_result_url    = :url,
                       last_status        = 'completed',
                       last_status_at     = now()
                 WHERE task_id = :t
            """), {"sid": sid, "t": task_id, "url": result_url})

            _mirror_submission_to_bronze_by_task(conn, task_id)

        app.logger.info(
            "INGEST STEP task_id=%s step=bronze_ingest_done session_id=%s",
            task_id, sid
        )

        try:
            del payload
        except Exception:
            pass

        # -------------------------
        # STEP 3: SILVER BUILD
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=silver_build_start", task_id)

        build_silver_point_detail(task_id=task_id, replace=True)

        app.logger.info("INGEST STEP task_id=%s step=silver_build_done", task_id)

        # -------------------------
        # STEP 4: VIDEO TRIM TRIGGER (ASYNC / NON-BLOCKING)
        # Must NOT fail ingest if trim trigger fails
        # -------------------------
        
        app.logger.info("INGEST STEP task_id=%s step=video_trim_trigger_start", task_id)

        try:
            trim_out = trigger_video_trim(task_id)
            app.logger.info(
                "INGEST STEP task_id=%s step=video_trim_trigger_done out=%s",
                task_id,
                trim_out,
            )
        except Exception as e:
            app.logger.exception(
                "INGEST STEP task_id=%s step=video_trim_trigger_failed error=%s",
                task_id,
                e,
            )

        # -------------------------
        # STEP 5: BILLING SYNC
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=billing_sync_start", task_id)

        try:
            out = sync_usage_for_task_id(task_id, dry_run=False)
            app.logger.info(
                "INGEST STEP task_id=%s step=billing_sync_done inserted=%s",
                task_id,
                out.get("inserted"),
            )
        except Exception as e:
            app.logger.exception("Billing consume failed task_id=%s: %s", task_id, e)

        # -------------------------
        # STEP 6: POWER BI REFRESH (WAIT TO TERMINAL)
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=pbi_refresh_wait_start", task_id)

        refresh_out = _poll_powerbi_refresh_until_terminal(task_id)

        pbi_ok = bool(refresh_out.get("ok"))
        pbi_status = str(refresh_out.get("status") or "").strip().lower()
        pbi_err = (refresh_out.get("error") or "").strip() or None

        app.logger.info(
            "INGEST STEP task_id=%s step=pbi_refresh_wait_done ok=%s status=%s error=%s",
            task_id, pbi_ok, pbi_status, pbi_err
        )

        if not pbi_ok:
            raise RuntimeError(f"Power BI refresh failed: status={pbi_status} error={pbi_err or 'unknown'}")

        # -------------------------
        # STEP 7: WIX NOTIFY (ONLY AFTER CONFIRMED PBI SUCCESS)
        # -------------------------
        try:
            _notify_wix(
                task_id,
                status="completed",
                session_id=sid,
                result_url=result_url,
                error=None,
            )
        except Exception as e:
            app.logger.exception("Wix notify failed task_id=%s: %s", task_id, e)
            raise RuntimeError(f"Wix notify failed: {e}")

        # -------------------------
        # STEP 8: FINAL SUCCESS
        # -------------------------
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_finished_at = now(),
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"t": task_id})

        app.logger.info("INGEST COMPLETE task_id=%s", task_id)
        return True

    except Exception as e:
        app.logger.exception("INGEST FAILED task_id=%s result_url=%s", task_id, result_url)

        err_txt = f"{e.__class__.__name__}: {e}"
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_error = :err,
                       ingest_finished_at = now()
                 WHERE task_id = :t
            """), {"t": task_id, "err": err_txt})

        # IMPORTANT:
        # do NOT send "completed" Wix notify on failure
        # leave failed handling to ops / later explicit failure-email design
        return False

INGEST_STALE_AFTER_S = int(os.getenv("INGEST_STALE_AFTER_S", "1800"))  # 30 minutes


def _is_stale_ingest_row(row) -> bool:
    started_at = row.get("ingest_started_at") if row else None
    finished_at = row.get("ingest_finished_at") if row else None

    if not started_at or finished_at:
        return False

    try:
        now_utc = datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        age_s = (now_utc - started_at).total_seconds()
        return age_s >= INGEST_STALE_AFTER_S
    except Exception:
        return False


def _delegate_to_ingest_worker(task_id: str, result_url: str) -> dict:
    """
    POST to the ingest-worker service. Returns immediately (202).
    The worker runs the full pipeline in a background thread.
    """
    if not INGEST_WORKER_BASE_URL:
        raise RuntimeError("INGEST_WORKER_BASE_URL not set — cannot delegate ingest")
    if not INGEST_WORKER_OPS_KEY:
        raise RuntimeError("INGEST_WORKER_OPS_KEY not set — cannot delegate ingest")

    resp = requests.post(
        f"{INGEST_WORKER_BASE_URL}/ingest",
        json={"task_id": task_id, "result_url": result_url},
        headers={
            "Authorization": f"Bearer {INGEST_WORKER_OPS_KEY}",
            "Content-Type": "application/json",
        },
        timeout=INGEST_WORKER_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _start_ingest_background(task_id: str, result_url: str) -> bool:
    """
    Delegate ingest to the ingest-worker service.

    Idempotent: checks DB state before delegating.
    Falls back to in-process _do_ingest if the worker is unreachable.

    Returns True if ingest was triggered, False if already done/running.
    """
    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        row = conn.execute(sql_text("""
            SELECT session_id, ingest_started_at, ingest_finished_at, ingest_error
              FROM bronze.submission_context
             WHERE task_id = :t
             LIMIT 1
        """), {"t": task_id}).mappings().first()

        if row and row.get("session_id") and row.get("ingest_finished_at"):
            return False  # already done

        if row and row.get("ingest_started_at") and not row.get("ingest_finished_at"):
            if not _is_stale_ingest_row(row):
                return False  # already running
            app.logger.warning(
                "INGEST STALE DETECTED task_id=%s ingest_started_at=%s — re-triggering",
                task_id, row.get("ingest_started_at"),
            )

    # Delegate to ingest worker service
    try:
        out = _delegate_to_ingest_worker(task_id, result_url)
        app.logger.info(
            "INGEST DELEGATED task_id=%s worker_response=%s",
            task_id, out,
        )
        return True

    except Exception as e:
        app.logger.exception(
            "INGEST WORKER UNREACHABLE task_id=%s error=%s — falling back to in-process",
            task_id, e,
        )
        # Fallback: run in-process so ingest still happens
        ok = _do_ingest(task_id, result_url)
        app.logger.info("INGEST FALLBACK DONE task_id=%s ok=%s", task_id, ok)
        return True

# ==========================
# MEDIA ROOM (served same-origin for upload API access)
# ==========================
@app.get("/media-room")
def media_room():
    from flask import send_file
    return send_file("media_room.html")


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

#=============================================================
# NEW CALLBACK ENDPOINT FOR VIDEO WORKER
#==============================================================
@app.post("/internal/video_trim_complete")
def video_trim_complete():
    if not _guard():
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True) or {}

    task_id = str(body.get("task_id") or "").strip()
    status = str(body.get("status") or "").strip().lower()

    if not task_id or status not in {"completed", "failed"}:
        return jsonify({"error": "invalid_payload"}), 400

    try:
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)

            if status == "completed":
                # Idempotent: do not overwrite a previously completed trim
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                       SET trim_status = 'completed',
                           trim_finished_at = now(),
                           trim_output_s3_key = :output_s3_key,
                           trim_source_duration_s = :source_duration_s,
                           trim_duration_s = :trimmed_duration_s,
                           trim_segment_count = :segment_count,
                           trim_seconds_removed = :seconds_removed,
                           trim_error = NULL
                     WHERE task_id = :task_id
                       AND trim_status != 'completed'
                """), {
                    "task_id": task_id,
                    "output_s3_key": body.get("output_s3_key"),
                    "source_duration_s": body.get("source_duration_s"),
                    "trimmed_duration_s": body.get("trimmed_duration_s"),
                    "segment_count": body.get("segment_count"),
                    "seconds_removed": body.get("seconds_removed"),
                })

            else:
                # Idempotent: do not overwrite a completed trim with a failure
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                       SET trim_status = 'failed',
                           trim_finished_at = now(),
                           trim_error = LEFT(:error, 4000)
                     WHERE task_id = :task_id
                       AND trim_status != 'completed'
                """), {
                    "task_id": task_id,
                    "error": body.get("error"),
                })

        app.logger.info(
            "VIDEO TRIM CALLBACK task_id=%s status=%s", task_id, status,
        )

    except Exception as e:
        app.logger.exception(
            "VIDEO TRIM CALLBACK DB ERROR task_id=%s status=%s error=%s",
            task_id, status, e,
        )
        return jsonify({"error": "db_update_failed"}), 500

    return jsonify({"ok": True})


# ==========================
# PRESIGN (OPTIONAL)
# ==========================
@app.post("/upload/api/s3-presign")
def api_s3_presign():
    _require_s3()
    body = request.get_json(silent=True) or {}

    email = (body.get("email") or "").strip().lower()
    allowed, reason = _upload_entitlement_gate(email)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 403

    name = (body.get("name") or "video.mp4").strip()
    ctype = (body.get("content_type") or "application/octet-stream").strip()
    clean = secure_filename(name) or "video.mp4"

    if not ctype.lower().startswith("video/"):
        return jsonify({"ok": False, "error": "invalid_content_type"}), 400

    _, ext = os.path.splitext(clean.lower())
    allowed_ext = {".mp4", ".mov", ".m4v", ".mpg", ".mpeg"}
    if ext not in allowed_ext:
        return jsonify({"ok": False, "error": "unsupported_file_extension"}), 400

    max_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024 * 1024)))  # default 2GB

    key = f"{S3_PREFIX}/{int(time.time())}_{clean}"
    cli = _s3_client()

    post = cli.generate_presigned_post(
        Bucket=S3_BUCKET,
        Key=key,
        Fields={"Content-Type": ctype},
        Conditions=[
            {"Content-Type": ctype},
            ["content-length-range", 1, max_bytes]
        ],
        ExpiresIn=600,
    )

    return jsonify({
        "ok": True,
        "bucket": S3_BUCKET,
        "key": key,
        "post": post,
        "get_url": _s3_presigned_get_url(key),
        "max_upload_bytes": max_bytes
    })


# ==========================
# MULTIPART INITIATE / PART / COMPLETE / ABORT
# ==========================
@app.post("/upload/api/multipart/initiate")
def api_multipart_initiate():
    _require_s3()
    body = request.get_json(silent=True) or {}

    email = (body.get("email") or "").strip().lower()
    allowed, reason = _upload_entitlement_gate(email)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 403

    name = (body.get("name") or "video.mp4").strip()
    ctype = (body.get("content_type") or "application/octet-stream").strip()
    size = int(body.get("size") or 0)

    clean = secure_filename(name) or "video.mp4"

    if not ctype.lower().startswith("video/"):
        return jsonify({"ok": False, "error": "invalid_content_type"}), 400

    _, ext = os.path.splitext(clean.lower())
    allowed_ext = {".mp4", ".mov", ".m4v", ".mpg", ".mpeg"}
    if ext not in allowed_ext:
        return jsonify({"ok": False, "error": "unsupported_file_extension"}), 400

    max_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024 * 1024)))  # 5GB ceiling for multipart v1
    if size <= 0:
        return jsonify({"ok": False, "error": "invalid_size"}), 400
    if size > max_bytes:
        return jsonify({"ok": False, "error": f"file_too_large:{size}"}), 400

    key = f"{S3_PREFIX}/{int(time.time())}_{clean}"

    try:
        out = _s3_create_multipart_upload(key, content_type=ctype)
        upload_id = out.get("UploadId")
        if not upload_id:
            return jsonify({"ok": False, "error": "missing_upload_id"}), 500

        part_size = MULTIPART_PART_SIZE
        part_count = (size + part_size - 1) // part_size

        return jsonify({
            "ok": True,
            "bucket": S3_BUCKET,
            "key": key,
            "upload_id": upload_id,
            "part_size": part_size,
            "part_count": part_count,
            "max_upload_bytes": max_bytes,
            "get_url": _s3_presigned_get_url(key),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"multipart_initiate_failed: {e}"}), 500


@app.post("/upload/api/multipart/presign-part")
def api_multipart_presign_part():
    _require_s3()
    body = request.get_json(silent=True) or {}

    email = (body.get("email") or "").strip().lower()
    allowed, reason = _upload_entitlement_gate(email)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 403

    key = (body.get("key") or "").strip()
    upload_id = (body.get("upload_id") or "").strip()
    part_number = int(body.get("part_number") or 0)

    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    if not upload_id:
        return jsonify({"ok": False, "error": "upload_id required"}), 400
    if part_number < 1:
        return jsonify({"ok": False, "error": "invalid_part_number"}), 400

    try:
        url = _s3_presign_upload_part(key, upload_id, part_number)
        return jsonify({
            "ok": True,
            "url": url,
            "part_number": part_number,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"presign_part_failed: {e}"}), 500


@app.post("/upload/api/multipart/complete")
def api_multipart_complete():
    _require_s3()
    body = request.get_json(silent=True) or {}

    email = (body.get("email") or "").strip().lower()
    allowed, reason = _upload_entitlement_gate(email)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 403

    key = (body.get("key") or "").strip()
    upload_id = (body.get("upload_id") or "").strip()
    parts = body.get("parts") or []

    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    if not upload_id:
        return jsonify({"ok": False, "error": "upload_id required"}), 400
    if not isinstance(parts, list) or not parts:
        return jsonify({"ok": False, "error": "parts required"}), 400

    # =========================
    # NORMALISE PARTS (FIX ETag)
    # =========================
    norm_parts = []
    for p in parts:
        try:
            pn = int(p.get("PartNumber"))
            et = str(p.get("ETag") or "").strip()

            if pn < 1 or not et:
                raise ValueError("bad part")

            # 🔥 CRITICAL: ensure quotes
            if not et.startswith('"'):
                et = f'"{et}"'

            norm_parts.append({"PartNumber": pn, "ETag": et})

        except Exception as e:
            app.logger.error("INVALID PART: %s ERROR: %s", p, e)
            return jsonify({"ok": False, "error": f"invalid_part:{p}"}), 400

    norm_parts.sort(key=lambda x: x["PartNumber"])

    # =========================
    # LOG START
    # =========================
    app.logger.info(
        "MULTIPART COMPLETE START key=%s upload_id=%s parts=%s",
        key, upload_id, len(norm_parts)
    )

    # =========================
    # COMPLETE MULTIPART
    # =========================
    try:
        out = _s3_complete_multipart_upload(key, upload_id, norm_parts)

    except Exception as e:
        app.logger.exception(
            "MULTIPART COMPLETE FAILED key=%s upload_id=%s parts=%s",
            key, upload_id, norm_parts[:3]
        )
        return jsonify({
            "ok": False,
            "error": f"multipart_complete_failed: {e}",
            "debug_parts_count": len(norm_parts)
        }), 500

    # =========================
    # VALIDATE S3 OBJECT
    # =========================
    head_ok, head_err, head_meta = _validate_uploaded_s3_object_for_submit(key)
    if not head_ok:
        return jsonify({
            "ok": False,
            "error": head_err,
            "s3_meta": head_meta
        }), 400

    # =========================
    # SUCCESS
    # =========================
    return jsonify({
        "ok": True,
        "key": key,
        "location": out.get("Location"),
        "etag": out.get("ETag"),
        "get_url": _s3_presigned_get_url(key),
        "s3_meta": head_meta,
    })


@app.post("/upload/api/multipart/abort")
def api_multipart_abort():
    _require_s3()
    body = request.get_json(silent=True) or {}

    email = (body.get("email") or "").strip().lower()
    allowed, reason = _upload_entitlement_gate(email)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 403

    key = (body.get("key") or "").strip()
    upload_id = (body.get("upload_id") or "").strip()

    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    if not upload_id:
        return jsonify({"ok": False, "error": "upload_id required"}), 400

    try:
        _s3_abort_multipart_upload(key, upload_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"multipart_abort_failed: {e}"}), 500


@app.post("/upload/api/multipart/list-parts")
def api_multipart_list_parts():
    """Return parts already uploaded for a multipart upload (for resume & ETag retrieval)."""
    _require_s3()
    body = request.get_json(silent=True) or {}

    key = (body.get("key") or "").strip()
    upload_id = (body.get("upload_id") or "").strip()

    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    if not upload_id:
        return jsonify({"ok": False, "error": "upload_id required"}), 400

    try:
        parts = _s3_list_multipart_parts(key, upload_id)
        return jsonify({"ok": True, "parts": parts})
    except Exception as e:
        return jsonify({"ok": False, "error": f"list_parts_failed: {e}"}), 500


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
        # ---------- JSON path (Wix) ----------
        if request.is_json:
            body = request.get_json(silent=True) or {}
            video_url = (body.get("video_url") or "").strip()
            if not video_url:
                return jsonify({"ok": False, "error": "video_url required"}), 400

            # NEW: entitlement gate BEFORE SportAI check
            email = (body.get("email") or "").strip().lower()
            allowed, reason = _upload_entitlement_gate(email)
            if not allowed:
                return jsonify({"ok": False, "error": reason}), 403

            chk = _sportai_check(video_url)
            return jsonify({"ok": True, "video_url": video_url, "check": chk, "check_passed": _passed(chk)})

        # ---------- multipart fallback ----------
        # NEW: entitlement gate BEFORE any S3 upload
        email = (request.form.get("email") or "").strip().lower()
        allowed, reason = _upload_entitlement_gate(email)
        if not allowed:
            return jsonify({"ok": False, "error": reason}), 403

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
        chk = _sportai_check(video_url)
        return jsonify({"ok": True, "video_url": video_url, "check": chk, "check_passed": _passed(chk)})

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
        video_url = (body.get("video_url") or "").strip()
        email = (body.get("email") or "").strip().lower()
        allowed, reason = _upload_entitlement_gate(email)
        if not allowed:
            return jsonify({"ok": False, "error": reason}), 403

        meta = body.get("meta") or body.get("metadata") or {}
        if video_url:
            try:
                task_id = _sportai_submit(video_url, email=email, meta=meta)
                share_url = (body.get("share_url") or "").strip() or None
                _store_submission_context(task_id, email, meta, video_url, share_url=share_url)
                with engine.begin() as conn:
                    _ensure_submission_context_schema(conn)
                    _set_status_cache(conn, task_id, "queued", None)
                return jsonify({"ok": True, "task_id": task_id, "video_url": video_url})
            except Exception as e:
                return jsonify({"ok": False, "error": f"SportAI submit failed: {e}"}), 502

    # Multipart path: browser → server → S3 (fallback)
    f = request.files.get("file") or request.files.get("video")
    email = (request.form.get("email") or "").strip().lower()
    allowed, reason = _upload_entitlement_gate(email)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 403

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
        "player_a_utr": _norm(player_utr) if str(player_utr).strip().isdigit() else None,
        "player_b_utr": _norm(opponent_utr) if str(opponent_utr).strip().isdigit() else None,

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

        email = (body.get("customer_email") or body.get("email") or "").strip().lower()
        allowed, reason = _upload_entitlement_gate(email)
        if not allowed:
            return jsonify({"ok": False, "error": reason}), 403

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


        task_id = _sportai_submit(s3_video_url, email=email, meta=meta)

        _store_submission_context(
            task_id=task_id,
            email=email,
            meta=meta,
            video_url=s3_video_url,
            share_url=source_url,  # original Wix download URL
            s3_bucket=S3_BUCKET,
            s3_key=key,
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

    email = (body.get("customer_email") or body.get("email") or "").strip().lower()
    allowed, reason = _upload_entitlement_gate(email)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 403

    ok_obj, obj_err, obj_meta = _validate_uploaded_s3_object_for_submit(s3_key)
    if not ok_obj:
        return jsonify({
            "ok": False,
            "error": obj_err,
            "s3_meta": obj_meta
        }), 400

    s3_video_url = _s3_presigned_get_url(s3_key)

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
        if v is None:
            return None
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
        "s3_upload": {
            "key": s3_key,
            "content_length": (obj_meta or {}).get("content_length"),
            "content_type": (obj_meta or {}).get("content_type"),
            "etag": (obj_meta or {}).get("etag"),
        }
    }

    try:
        task_id = _sportai_submit(s3_video_url, email=email, meta=meta)
        _store_submission_context(
            task_id=task_id,
            email=email,
            meta=meta,
            video_url=s3_video_url,
            share_url=s3_key,
            s3_bucket=S3_BUCKET,
            s3_key=s3_key,
        )

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            _set_status_cache(conn, task_id, "queued", None)

        return jsonify({
            "ok": True,
            "task_id": task_id,
            "s3_verified": True,
            "s3_meta": obj_meta
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"SportAI submit failed: {e}"}), 502

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

    live_error = None
    live = None

    try:
        live = _sportai_status(tid)

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            _set_status_cache(conn, tid, live.get("status"), live.get("result_url"))

    except Exception as e:
        live_error = f"{e.__class__.__name__}: {e}"

    sc = _load_submission_context_row(tid)

    status = _normalize_sportai_status(
        (live or {}).get("status") or sc.get("last_status") or "unknown"
    )
    result_url = (live or {}).get("result_url") or sc.get("last_result_url")
    sportai_progress_pct = (live or {}).get("sportai_progress_pct")
    terminal = _is_terminal_status(status)
    success_terminal = _is_success_terminal_status(status)

    session_id = sc.get("session_id")
    ingest_started = sc.get("ingest_started_at") is not None
    ingest_finished = sc.get("ingest_finished_at") is not None
    ingest_error = sc.get("ingest_error")
    ingest_running = ingest_started and not ingest_finished

    # IMPORTANT:
    # SportAI may expose result_url before it gives us a clean terminal status.
    # For ingestion orchestration, result_url is sufficient to begin backend processing.
    # This MUST be separate from the customer-facing display state.
    ingest_ready = bool(result_url)

    if AUTO_INGEST_ON_COMPLETE and ingest_ready and not session_id and not ingest_running:
        app.logger.info(
            "AUTO INGEST CHECK task_id=%s ingest_ready=%s session_id=%s ingest_running=%s result_url_present=%s status=%s",
            tid,
            ingest_ready,
            bool(session_id),
            ingest_running,
            bool(result_url),
            status,
        )
        try:
            started = _start_ingest_background(tid, result_url)
            app.logger.info(
                "AUTO INGEST START RESULT task_id=%s started=%s",
                tid,
                started,
            )
            if started:
                sc = _load_submission_context_row(tid)
                session_id = sc.get("session_id")
                ingest_started = sc.get("ingest_started_at") is not None
                ingest_finished = sc.get("ingest_finished_at") is not None
                ingest_error = sc.get("ingest_error")
                ingest_running = ingest_started and not ingest_finished
        except Exception as e:
            app.logger.exception("AUTO INGEST START FAILED task_id=%s: %s", tid, e)

    auto_ingested = bool(session_id and ingest_finished and not ingest_error)

    pbi_refresh_started = sc.get("pbi_refresh_started_at") is not None
    pbi_refresh_finished = sc.get("pbi_refresh_finished_at") is not None
    pbi_refresh_status = sc.get("pbi_refresh_status")
    pbi_refresh_error = sc.get("pbi_refresh_error")
    pbi_status_norm = str(pbi_refresh_status or "").lower().strip()

    # Lightweight PBI status sync: if refresh was triggered but not yet
    # terminal, do a single quick GET to check if it finished.
    # This replaces the old 30-min blocking poll — one fast check per
    # client poll cycle (~5s) until PBI reports terminal.
    if (
        pbi_refresh_started
        and not pbi_refresh_finished
        and pbi_status_norm in {"triggered", "running", "queued", "unknown"}
        and PBI_SERVICE_BASE
    ):
        try:
            pbi_out = _pbi_get("/dataset/refresh_status", timeout=PBI_REFRESH_STATUS_TIMEOUT_S)
            pbi_live_status = str(pbi_out.get("status") or "").strip().lower()
            pbi_is_terminal = bool(pbi_out.get("is_terminal"))
            pbi_error_msg = (pbi_out.get("error_message") or "").strip() or None

            if pbi_live_status and pbi_live_status != pbi_status_norm:
                with engine.begin() as conn:
                    _ensure_submission_context_schema(conn)
                    _set_pbi_refresh_state(
                        conn, tid,
                        status=pbi_live_status,
                        error=pbi_error_msg,
                        started=True,
                        finished=pbi_is_terminal,
                        clear_error=not pbi_error_msg,
                    )
                pbi_refresh_status = pbi_live_status
                pbi_status_norm = pbi_live_status
                pbi_refresh_error = pbi_error_msg
                if pbi_is_terminal:
                    pbi_refresh_finished = True

                    # Auto-suspend capacity after terminal
                    if PBI_SUSPEND_AFTER_REFRESH:
                        try:
                            _pbi_post("/capacity/suspend", {}, timeout=60)
                        except Exception:
                            pass

        except Exception as e:
            app.logger.debug("PBI status check failed task_id=%s: %s", tid, e)

    dashboard_ready = bool(
        session_id
        and ingest_finished
        and not ingest_error
        and pbi_refresh_finished
        and pbi_status_norm == "completed"
        and not pbi_refresh_error
    )

    # Auto-fire Wix notify once dashboard is ready (idempotent)
    if dashboard_ready:
        try:
            _notify_wix(
                tid,
                status="completed",
                session_id=session_id,
                result_url=result_url,
                error=None,
            )
        except Exception as e:
            app.logger.exception("WIX NOTIFY FAILED task_id=%s: %s", tid, e)

    pipeline_stage = _derive_pipeline_stage(
        sportai_status=status,
        ingest_started=ingest_started,
        ingest_finished=ingest_finished,
        ingest_error=ingest_error,
        pbi_refresh_started=pbi_refresh_started,
        pbi_refresh_finished=pbi_refresh_finished,
        pbi_refresh_status=pbi_refresh_status,
        pbi_refresh_error=pbi_refresh_error,
        dashboard_ready=dashboard_ready,
    )

    display_progress_pct = _derive_display_progress_pct(
        sportai_progress_pct=sportai_progress_pct,
        pipeline_stage=pipeline_stage,
        dashboard_ready=dashboard_ready,
    )

    return jsonify({
        "ok": True,
        "task_id": tid,

        # Canonical fields
        "sportai_status": status,
        "sportai_progress_pct": sportai_progress_pct,
        "pipeline_stage": pipeline_stage,
        "display_progress_pct": display_progress_pct,

        # Backward-compatible fields
        "status": status,
        "progress_pct": display_progress_pct,
        "progress": display_progress_pct,
        "stage": pipeline_stage,

        "terminal": terminal,
        "success_terminal": success_terminal,

        "fallback": live is None,
        "live_status_error": live_error,

        "session_id": session_id,
        "auto_ingested": auto_ingested,
        "auto_ingest_error": ingest_error,
        "ingest_started": ingest_started,
        "ingest_running": ingest_running,
        "ingest_finished": ingest_finished,

        "wix_notified_at": sc.get("wix_notified_at"),
        "wix_notify_status": sc.get("wix_notify_status"),
        "wix_notify_error": sc.get("wix_notify_error"),

        "pbi_refresh_started": pbi_refresh_started,
        "pbi_refresh_finished": pbi_refresh_finished,
        "pbi_refresh_status": pbi_refresh_status,
        "pbi_refresh_error": pbi_refresh_error,

        "dashboard_ready": dashboard_ready,        
    }), 200


# ==========================
# MANUAL INGEST HELPER (TASK_ID-ONLY)
# ==========================
@app.post("/ops/ingest-task")
def ops_ingest_task():
    if not _guard():
        return Response("Forbidden", 403)

    body = request.get_json(silent=True) or {}
    tid = (body.get("task_id") or "").strip()
    mode = (body.get("mode") or "sync").strip().lower()

    if not tid:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    if mode not in {"worker", "sync"}:
        return jsonify({"ok": False, "error": "mode must be 'worker' or 'sync'"}), 400

    try:
        app.logger.info("OPS INGEST START task_id=%s mode=%s", tid, mode)

        result_url = _resolve_result_url_for_task(tid)
        if not result_url:
            return jsonify({
                "ok": False,
                "error": "result_url_not_available",
                "task_id": tid,
            }), 400

        app.logger.info("OPS INGEST RESOLVED task_id=%s result_url=%s", tid, result_url)

        if mode == "worker":
            out = _delegate_to_ingest_worker(tid, result_url)
            launched = bool(out.get("accepted"))

            with engine.begin() as conn:
                _ensure_submission_context_schema(conn)
                row = conn.execute(sql_text("""
                    SELECT
                      session_id,
                      ingest_started_at,
                      ingest_finished_at,
                      ingest_error,
                      pbi_refresh_status,
                      pbi_refresh_error,
                      wix_notify_status,
                      wix_notify_error,
                      trim_requested_at,
                      trim_finished_at,
                      trim_status,
                      trim_error,
                      trim_output_s3_key,
                      trim_source_duration_s,
                      trim_duration_s,
                      trim_segment_count,
                      trim_seconds_removed
                    FROM bronze.submission_context
                    WHERE task_id = :tid
                    LIMIT 1
                """), {"tid": tid}).mappings().first() or {}

            return jsonify({
                "ok": True,
                "accepted": bool(launched),
                "mode": "worker",
                "task_id": tid,
                "result_url": result_url,
                "session_id": row.get("session_id"),
                "ingest_started_at": row.get("ingest_started_at"),
                "ingest_finished_at": row.get("ingest_finished_at"),
                "ingest_error": row.get("ingest_error"),
                "pbi_refresh_status": row.get("pbi_refresh_status"),
                "pbi_refresh_error": row.get("pbi_refresh_error"),
                "wix_notify_status": row.get("wix_notify_status"),
                "wix_notify_error": row.get("wix_notify_error"),
                "trim_requested_at": row.get("trim_requested_at"),
                "trim_finished_at": row.get("trim_finished_at"),
                "trim_status": row.get("trim_status"),
                "trim_error": row.get("trim_error"),
                "trim_output_s3_key": row.get("trim_output_s3_key"),
                "trim_source_duration_s": row.get("trim_source_duration_s"),
                "trim_duration_s": row.get("trim_duration_s"),
                "trim_segment_count": row.get("trim_segment_count"),
                "trim_seconds_removed": row.get("trim_seconds_removed"),
            }), 202

        # Explicit sync mode = deep debug only
        ok = _do_ingest(tid, result_url)

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            row = conn.execute(sql_text("""
                SELECT
                  session_id,
                  ingest_started_at,
                  ingest_finished_at,
                  ingest_error,
                  pbi_refresh_status,
                  pbi_refresh_error,
                  wix_notify_status,
                  wix_notify_error,
                  trim_requested_at,
                  trim_finished_at,
                  trim_status,
                  trim_error,
                  trim_output_s3_key,
                  trim_source_duration_s,
                  trim_duration_s,
                  trim_segment_count,
                  trim_seconds_removed
                FROM bronze.submission_context
                WHERE task_id = :tid
                LIMIT 1
            """), {"tid": tid}).mappings().first() or {}

        return jsonify({
            "ok": bool(ok),
            "mode": "sync",
            "task_id": tid,
            "result_url": result_url,
            "session_id": row.get("session_id"),
            "ingest_started_at": row.get("ingest_started_at"),
            "ingest_finished_at": row.get("ingest_finished_at"),
            "ingest_error": row.get("ingest_error"),
            "pbi_refresh_status": row.get("pbi_refresh_status"),
            "pbi_refresh_error": row.get("pbi_refresh_error"),
            "wix_notify_status": row.get("wix_notify_status"),
            "wix_notify_error": row.get("wix_notify_error"),
            "trim_requested_at": row.get("trim_requested_at"),
            "trim_finished_at": row.get("trim_finished_at"),
            "trim_status": row.get("trim_status"),
            "trim_error": row.get("trim_error"),
            "trim_output_s3_key": row.get("trim_output_s3_key"),
            "trim_source_duration_s": row.get("trim_source_duration_s"),
            "trim_duration_s": row.get("trim_duration_s"),
            "trim_segment_count": row.get("trim_segment_count"),
            "trim_seconds_removed": row.get("trim_seconds_removed"),
        }), (200 if ok else 500)

    except Exception as e:
        app.logger.exception("OPS INGEST FAILED task_id=%s mode=%s", tid, mode)
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
# SIMPLE BILLING (AUDITABLE, NO LEGACY TABLES)
# Source of truth: bronze.submission_context
# Per-match now; also returns total_minutes for future pivot.
# ==========================

def _hms_to_seconds(s: str | None) -> int | None:
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})$", s)
    if not m:
        return None
    hh, mm, ss = map(int, m.groups())
    return hh * 3600 + mm * 60 + ss

def _billing_guard() -> bool:
    expected = (os.getenv("BILLING_OPS_KEY") or OPS_KEY or "").strip()
    if not expected:
        return False
    hk = request.headers.get("X-Ops-Key") or request.headers.get("X-Ops-Key".lower()) or request.headers.get("X-OPS-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    return (hk or "").strip() == expected


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