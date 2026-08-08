# ============================================================
# ingest_worker_app.py — Dedicated ingest worker service (Render, 3600s timeout).
#
# Receives POST /ingest from upload_app.py, returns 202 immediately, and runs the
# full ingest pipeline in a background thread. Self-contained — does NOT import upload_app.
#
# Pipeline steps (sequential, all idempotent):
#   1. Download SportAI result JSON (gzip-aware, up to 900s timeout)
#   2. Bronze ingest — parse JSON into typed bronze tables via ingest_bronze_strict()
#   3. Silver build — run build_silver_v2 to compute point_detail analytics
#   4. Video trim trigger — fire-and-forget POST to video worker service
#   5. Billing sync — sync completed task into billing consumption records
#   6. Mark complete — set ingest_finished_at on submission_context
#
# Business rules:
#   - Duplicate prevention: in-memory thread lock prevents concurrent ingests for same task_id
#   - Each step is wrapped in try/except so failures in trim/billing don't block completion
#   - Ingest errors are persisted to submission_context.ingest_error for ops visibility
#   - Auth: requires Authorization: Bearer <INGEST_WORKER_OPS_KEY> header
#
# Endpoints:
#   POST /ingest         — accepts {task_id, result_url}, returns 202
#   GET  /ingest/status  — lightweight status check from submission_context
#   GET  /               — service identity
#   GET  /healthz        — liveness probe
# ============================================================

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify
from sqlalchemy import text as sql_text
from sqlalchemy.exc import OperationalError, InterfaceError

app = Flask(__name__)
log = logging.getLogger(__name__)

# ============================================================
# CONFIG
# ============================================================

INGEST_WORKER_OPS_KEY = (os.getenv("INGEST_WORKER_OPS_KEY") or "").strip()
OPS_KEY = (os.getenv("OPS_KEY") or "").strip()

if not INGEST_WORKER_OPS_KEY:
    raise RuntimeError("INGEST_WORKER_OPS_KEY env var is required")

DEFAULT_REPLACE_ON_INGEST = (
    os.getenv("INGEST_REPLACE_EXISTING")
    or os.getenv("DEFAULT_REPLACE_ON_INGEST")
    or "1"
).strip().lower() in ("1", "true", "yes", "y")

# Video worker config
VIDEO_WORKER_BASE_URL = (os.getenv("VIDEO_WORKER_BASE_URL") or "").strip().rstrip("/")
VIDEO_WORKER_OPS_KEY = (os.getenv("VIDEO_WORKER_OPS_KEY") or "").strip()

# ============================================================
# IMPORTS — heavy modules loaded here (worker has 3600s timeout)
# ============================================================

from db_init import engine, log_task_event  # noqa: E402
from ingest_bronze import ingest_bronze_strict, _run_bronze_init  # noqa: E402
from build_silver_v2 import build_silver_v2 as build_silver_point_detail  # noqa: E402
from billing_import_from_bronze import sync_usage_for_task_id  # noqa: E402
from ingest_quality import assess as assess_payload, should_reject  # noqa: E402


# ============================================================
# AUTH
# ============================================================

def _auth_ok(req) -> bool:
    import hmac
    auth = (req.headers.get("Authorization") or "").strip()
    expected = f"Bearer {INGEST_WORKER_OPS_KEY}"
    return hmac.compare_digest(auth, expected)


# ============================================================
# DB HELPERS (self-contained, no upload_app dependency)
# ============================================================

def _ensure_schema(conn):
    """Idempotent schema bootstrap for submission_context columns we touch."""
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze"))
    for ddl in (
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_started_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_error TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS session_id TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_status TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_status_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_result_url TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notified_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notify_status TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notify_error TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
    ):
        conn.execute(sql_text(ddl))


def _abort_if_deleted(task_id: str, stage: str) -> bool:
    """Return True (and persist abort status) if submission_context.deleted_at is set.

    Caller is expected to short-circuit when this returns True — it stops the
    worker re-populating bronze rows after a user-initiated delete races with
    an in-flight ingest. The cleanup sweep mops up any partial writes.
    """
    try:
        with engine.connect() as conn:
            deleted = conn.execute(sql_text(
                "SELECT 1 FROM bronze.submission_context "
                "WHERE task_id = :t AND deleted_at IS NOT NULL"
            ), {"t": task_id}).scalar() is not None
    except Exception:
        app.logger.exception("INGEST WORKER deleted_at check failed task_id=%s stage=%s", task_id, stage)
        return False

    if not deleted:
        return False

    app.logger.warning("INGEST WORKER aborting stage=%s task_id=%s — match soft-deleted", stage, task_id)
    try:
        with engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_error = 'aborted: match deleted by user',
                       ingest_finished_at = COALESCE(ingest_finished_at, now())
                 WHERE task_id = :t
            """), {"t": task_id})
    except Exception:
        app.logger.exception("INGEST WORKER abort-status update failed task_id=%s", task_id)
    return True


# ============================================================
# VIDEO TRIM TRIGGER
# ============================================================

def _trigger_video_trim(task_id: str) -> None:
    """Trigger video trim via the dedicated video_trim_api module."""
    try:
        from video_pipeline.video_trim_api import trigger_video_trim
        out = trigger_video_trim(task_id)
        app.logger.info("INGEST WORKER video trim triggered task_id=%s out=%s", task_id, out)
    except Exception as e:
        app.logger.exception("INGEST WORKER video trim failed task_id=%s: %s", task_id, e)


# ============================================================
# CORE INGEST PIPELINE
# ============================================================

# Bounded retry for TRANSIENT DB unavailability (e.g. a Render Postgres
# failover/maintenance blip → "the database system is in recovery mode"). These
# are connectivity failures, NOT data/logic failures, so re-running the whole
# (idempotent) ingest after a short backoff rides out the window instead of
# stranding the match. pool_pre_ping already recycles DEAD connections; this
# covers the case pre-ping can't — a live endpoint that is itself in recovery.
INGEST_DB_RETRY_MAX = int(os.getenv("INGEST_DB_RETRY_MAX", "5"))
INGEST_DB_RETRY_BASE_S = float(os.getenv("INGEST_DB_RETRY_BASE_S", "5"))

_TRANSIENT_DB_MARKERS = (
    "in recovery mode",
    "the database system is starting up",
    "the database system is shutting down",
    "could not connect",
    "connection refused",
    "server closed the connection",
    "terminating connection",
    "connection timed out",
    "could not translate host name",
    "consuming input failed",
    "ssl connection has been closed",
)


def _notify_ops_bad_analysis(task_id: str, verdict) -> None:
    """Ops email for a rejected or degraded analysis. Best-effort: an alerting
    failure must never change the ingest outcome."""
    try:
        from coach_invite.video_complete_email import send_ops_email
        rejected = not verdict.ok
        head = "REJECTED — empty analysis" if rejected else "DEGRADED — ingested with warnings"
        lines = [
            f"Task {task_id}: {head}.",
            "",
            f"SportAI stats: {verdict.stats}",
        ]
        if verdict.detail:
            lines += ["", verdict.detail]
        if verdict.warnings:
            lines += ["", "Warnings:"] + [f"  - {w}" for w in verdict.warnings]
        if rejected:
            lines += [
                "",
                "No bronze/silver was built, no trim was fired, no completion email "
                "was sent, and no credit was consumed. The raw payload is archived "
                "at raw-json/<task_id>.json.gz for diagnosis.",
            ]
        send_ops_email(
            subject=f"[ingest {'rejected' if rejected else 'warning'}] {task_id[:8]} — "
                    f"{verdict.reason or 'degraded analysis'}",
            text_body="\n".join(lines),
        )
    except Exception as e:  # noqa: BLE001
        app.logger.warning("SANITY GATE ops email failed task_id=%s: %s", task_id, e)


def _is_transient_db_error(exc: BaseException) -> bool:
    """True for DB-connectivity/recovery errors worth retrying, vs a genuine
    data/logic failure (ProgrammingError/IntegrityError/DataError — not retried).
    OperationalError/InterfaceError from psycopg are connection-level by nature."""
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_DB_MARKERS)


def _do_ingest(task_id: str, result_url: str) -> bool:
    """
    Run the full ingest pipeline.

    Steps:
      1. Download SportAI result JSON
      2. Bronze ingest
      3. Silver build
      3b. Analytics tables         (fitness / movement / quality, best-effort)
      4. Video trim trigger        (fire-and-forget)
      5. Billing sync              (fire-and-forget)
      6. Wix notify                (data is ready after silver)
      7. Mark complete
    """
    sid = None

    try:
        app.logger.info("INGEST START task_id=%s result_url=%s", task_id, result_url)

        if _abort_if_deleted(task_id, "pre_start"):
            log_task_event(task_id, "bronze", "skipped", detail="aborted: match deleted by user")
            return False

        # Mark started
        with engine.begin() as conn:
            _ensure_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_started_at = COALESCE(ingest_started_at, now()),
                       ingest_finished_at = NULL,
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"t": task_id})
        log_task_event(task_id, "bronze", "started")

        # -------------------------
        # STEP 1: DOWNLOAD RESULT JSON
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=download_result_start", task_id)

        r = requests.get(result_url, timeout=900, stream=True)
        r.raise_for_status()

        content_encoding = (r.headers.get("Content-Encoding") or "").lower().strip()
        app.logger.info(
            "INGEST STEP task_id=%s step=download_result_headers status=%s content_length=%s encoding=%s",
            task_id, r.status_code, r.headers.get("Content-Length"), content_encoding,
        )

        if "gzip" in content_encoding:
            payload = json.load(gzip.GzipFile(fileobj=r.raw))
        else:
            payload = json.load(r.raw)

        app.logger.info("INGEST STEP task_id=%s step=download_result_done", task_id)

        # -------------------------
        # STEP 1b: ARCHIVE RAW + SCHEMA-DRIFT CHECK (best-effort, never fatal)
        # Keep the source of truth (the re-fetch URL expires in an hour) and
        # shout if SportAI added a top-level key we don't handle.
        # -------------------------
        try:
            from raw_archive import archive_raw, detect_drift
            archive_raw(task_id, payload)
            detect_drift(task_id, payload)
        except Exception as _arch_e:
            app.logger.warning("RAW ARCHIVE/drift step failed task_id=%s: %s", task_id, _arch_e)

        # -------------------------
        # STEP 1c: POST-ANALYSIS SANITY GATE
        # Runs AFTER the archive on purpose — a rejected payload is exactly the
        # one we want kept for diagnosis. A failed SportAI analysis returns 200
        # with a well-formed but empty payload; without this the ingest
        # "succeeds", silver gets nothing, the customer is emailed a ready
        # dashboard with no data, and a credit is consumed. See ingest_quality/.
        # -------------------------
        _verdict = assess_payload(payload)
        app.logger.info("INGEST STEP task_id=%s step=sanity_gate ok=%s stats=%s",
                        task_id, _verdict.ok, _verdict.stats)
        for _w in _verdict.warnings:
            app.logger.warning("INGEST QUALITY WARNING task_id=%s: %s", task_id, _w)

        if should_reject(_verdict):
            _err = f"empty_analysis: {_verdict.detail}"
            log_task_event(task_id, "bronze", "failed", error=_err)
            with engine.begin() as conn:
                _ensure_schema(conn)
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                       SET ingest_error       = :err,
                           ingest_finished_at = now(),
                           last_status        = 'failed',
                           last_status_at     = now()
                     WHERE task_id = :t
                """), {"t": task_id, "err": _err})
            _notify_ops_bad_analysis(task_id, _verdict)
            app.logger.error("INGEST REJECTED task_id=%s reason=%s", task_id, _verdict.reason)
            # Returning here deliberately skips bronze, silver, trim, the billing
            # sync (STEP 5) and the customer notify (STEP 6) — an unanalysable
            # match must not be billed or announced as ready.
            return False

        if _verdict.warnings:
            _notify_ops_bad_analysis(task_id, _verdict)

        # -------------------------
        # STEP 2: BRONZE INGEST
        # -------------------------
        if _abort_if_deleted(task_id, "pre_bronze"):
            log_task_event(task_id, "bronze", "skipped", detail="aborted: match deleted by user")
            return False

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
                   SET session_id      = :sid,
                       ingest_error    = NULL,
                       last_result_url = :url,
                       last_status     = 'completed',
                       last_status_at  = now()
                 WHERE task_id = :t
            """), {"sid": sid, "t": task_id, "url": result_url})

        app.logger.info("INGEST STEP task_id=%s step=bronze_ingest_done session_id=%s", task_id, sid)
        log_task_event(task_id, "bronze", "ok", detail=f"session_id={sid}")

        try:
            del payload
        except Exception:
            pass

        # -------------------------
        # STEP 3: SILVER BUILD
        # -------------------------
        if _abort_if_deleted(task_id, "pre_silver"):
            log_task_event(task_id, "silver", "skipped", detail="aborted: match deleted by user")
            return False

        app.logger.info("INGEST STEP task_id=%s step=silver_build_start", task_id)
        log_task_event(task_id, "silver", "started")
        build_silver_point_detail(task_id=task_id, replace=True)
        app.logger.info("INGEST STEP task_id=%s step=silver_build_done", task_id)
        log_task_event(task_id, "silver", "ok")

        # -------------------------
        # STEP 3b: ANALYTICS TABLES (best-effort, never fatal)
        # Fitness / movement-grid / match-quality from bronze data point_detail
        # doesn't read. Enrichment for dashboards — a failure here must NOT fail
        # the ingest, so it's wrapped and its own build_all is per-table safe.
        # -------------------------
        try:
            from silver_analytics import build_all as build_analytics
            counts = build_analytics(engine, task_id)
            app.logger.info("INGEST STEP task_id=%s step=analytics_done counts=%s", task_id, counts)
            log_task_event(task_id, "analytics", "ok", detail=str(counts))
        except Exception as ax:  # noqa: BLE001
            app.logger.warning("INGEST STEP task_id=%s step=analytics_failed err=%s", task_id, ax)
            log_task_event(task_id, "analytics", "skipped", detail=f"{ax.__class__.__name__}: {ax}")

        # -------------------------
        # STEP 4: VIDEO TRIM TRIGGER (fire-and-forget)
        # -------------------------
        if _abort_if_deleted(task_id, "pre_trim"):
            log_task_event(task_id, "trim", "skipped", detail="aborted: match deleted by user")
            return False

        app.logger.info("INGEST STEP task_id=%s step=video_trim_trigger_start", task_id)
        _trigger_video_trim(task_id)

        # -------------------------
        # STEP 5: BILLING SYNC (fire-and-forget)
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=billing_sync_start", task_id)
        try:
            out = sync_usage_for_task_id(task_id, dry_run=False)
            app.logger.info(
                "INGEST STEP task_id=%s step=billing_sync_done inserted=%s",
                task_id, out.get("inserted"),
            )
        except Exception as e:
            app.logger.exception("INGEST STEP task_id=%s billing_sync_failed: %s", task_id, e)

        # -------------------------
        # STEP 6: FINAL SUCCESS
        # -------------------------
        with engine.begin() as conn:
            _ensure_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_finished_at = now(),
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"t": task_id})

        # Product lifecycle event (fire-and-forget; no-op unless TRACKING_ENABLED=1).
        # MATCH_PROCESSED fires on successful SportAI ingest. Technique uploads never
        # reach this worker (they get TECHNIQUE_UPLOADED at submit instead) but we
        # guard on sport_type defensively so we never double-tag a technique row.
        try:
            _email = None
            _sport = ""
            try:
                with engine.connect() as conn:
                    _row = conn.execute(sql_text(
                        "SELECT email, sport_type FROM bronze.submission_context WHERE task_id = :t"
                    ), {"t": task_id}).mappings().first() or {}
                    _email = _row.get("email")
                    _sport = (_row.get("sport_type") or "")
            except Exception:
                pass
            if _sport != "technique_analysis":
                from marketing_crm.tracking import track
                from marketing_crm.tracking.events import MATCH_PROCESSED
                track(MATCH_PROCESSED, email=_email, ref_type="match", ref_id=task_id,
                      properties={"pipeline": "sportai", "sport_type": _sport})
        except Exception:
            pass

        app.logger.info("INGEST COMPLETE task_id=%s", task_id)
        return True

    except Exception as e:
        # A TRANSIENT DB error (e.g. Render Postgres failover → "database system
        # is in recovery mode") is not a data failure and — critically — we can't
        # even persist it right now because the DB is the thing that's down. Do
        # NOT stamp the match failed; re-raise so _run_ingest_with_retry backs off
        # and retries the whole idempotent pipeline once the DB is back.
        if _is_transient_db_error(e):
            app.logger.warning(
                "INGEST TRANSIENT DB ERROR task_id=%s — will retry: %s", task_id, e)
            raise

        app.logger.exception("INGEST FAILED task_id=%s result_url=%s", task_id, result_url)

        err_txt = f"{e.__class__.__name__}: {e}"
        log_task_event(task_id, "bronze", "failed", error=err_txt)
        try:
            with engine.begin() as conn:
                _ensure_schema(conn)
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                       SET ingest_error = :err,
                           ingest_finished_at = now()
                     WHERE task_id = :t
                """), {"t": task_id, "err": err_txt})
        except Exception:
            app.logger.exception("INGEST FAILED — could not persist error for task_id=%s", task_id)

        # MATCH_FAILED lifecycle event (fire-and-forget; no-op unless TRACKING_ENABLED=1)
        try:
            _email = None
            try:
                with engine.connect() as conn:
                    _email = conn.execute(sql_text(
                        "SELECT email FROM bronze.submission_context WHERE task_id = :t"
                    ), {"t": task_id}).scalar()
            except Exception:
                pass
            from marketing_crm.tracking import track
            from marketing_crm.tracking.events import MATCH_FAILED
            track(MATCH_FAILED, email=_email, ref_type="match", ref_id=task_id,
                  properties={"pipeline": "sportai", "error": err_txt[:300]})
        except Exception:
            pass

        return False


# ============================================================
# BACKGROUND RUNNER
# ============================================================

def _persist_transient_giveup(task_id: str, exc: BaseException) -> None:
    """After exhausting retries on a transient DB outage, record a clear error so
    the failure is visible (not silent). Best-effort with a few tries of its own,
    since the DB may have come back by now."""
    err_txt = (f"transient DB unavailable after {INGEST_DB_RETRY_MAX} retries "
               f"(likely a Render Postgres failover/recovery window): "
               f"{exc.__class__.__name__}: {exc}")[:1500]
    app.logger.error("INGEST GAVE UP (transient DB) task_id=%s: %s", task_id, err_txt)
    for _ in range(3):
        try:
            log_task_event(task_id, "bronze", "failed", error=err_txt)
            with engine.begin() as conn:
                _ensure_schema(conn)
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                       SET ingest_error = :err, ingest_finished_at = now()
                     WHERE task_id = :t AND ingest_finished_at IS NULL
                """), {"t": task_id, "err": err_txt})
            return
        except Exception:
            time.sleep(3)


def _run_ingest_with_retry(task_id: str, result_url: str) -> bool:
    """Run _do_ingest, retrying the WHOLE pipeline on a TRANSIENT DB error with
    exponential backoff. _do_ingest is idempotent (bronze replace + advisory
    locks, silver replace), so re-running from the top is safe. Non-transient
    failures are already handled/marked inside _do_ingest and are not retried."""
    delay = INGEST_DB_RETRY_BASE_S
    for attempt in range(1, INGEST_DB_RETRY_MAX + 1):
        try:
            return _do_ingest(task_id, result_url)
        except Exception as exc:  # noqa: BLE001
            if not _is_transient_db_error(exc):
                raise  # genuine failure that slipped past _do_ingest's handler
            if attempt >= INGEST_DB_RETRY_MAX:
                _persist_transient_giveup(task_id, exc)
                return False
            app.logger.warning(
                "INGEST retry %s/%s for task_id=%s after transient DB error "
                "(sleeping %.0fs): %s", attempt, INGEST_DB_RETRY_MAX, task_id, delay, exc)
            time.sleep(delay)
            delay = min(delay * 2, 120)
    return False


# Track in-flight ingests to prevent duplicate launches
_active_ingests: Dict[str, threading.Thread] = {}
_active_lock = threading.Lock()


def _run_ingest_background(task_id: str, result_url: str) -> bool:
    """
    Launch ingest in a background thread within this process.
    Returns False if already running for this task_id.
    """
    with _active_lock:
        existing = _active_ingests.get(task_id)
        if existing and existing.is_alive():
            return False  # already running

        def _worker():
            try:
                _run_ingest_with_retry(task_id, result_url)
            finally:
                with _active_lock:
                    _active_ingests.pop(task_id, None)

        t = threading.Thread(target=_worker, name=f"ingest-{task_id[:8]}", daemon=True)
        _active_ingests[task_id] = t
        t.start()
        return True


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/")
def root_ok():
    return jsonify({"ok": True, "service": "nextpoint-ingest-worker"})


@app.get("/healthz")
def healthz_ok():
    return "OK", 200


@app.post("/ingest")
def ingest():
    if not _auth_ok(request):
        app.logger.warning("INGEST WORKER unauthorized request")
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    task_id = str(body.get("task_id") or "").strip()
    result_url = str(body.get("result_url") or "").strip()

    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    if not result_url:
        return jsonify({"ok": False, "error": "result_url required", "task_id": task_id}), 400

    launched = _run_ingest_background(task_id, result_url)

    if not launched:
        app.logger.info("INGEST WORKER already running task_id=%s", task_id)
        return jsonify({
            "ok": True,
            "accepted": False,
            "task_id": task_id,
            "status": "already_running",
        }), 200

    app.logger.info("INGEST WORKER ACCEPTED task_id=%s", task_id)
    return jsonify({
        "ok": True,
        "accepted": True,
        "task_id": task_id,
        "status": "accepted",
    }), 202


@app.get("/ingest/status")
def ingest_status():
    """Lightweight status check — reads from submission_context."""
    if not _auth_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    task_id = (request.args.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    with engine.begin() as conn:
        _ensure_schema(conn)
        row = conn.execute(sql_text("""
            SELECT
              session_id,
              ingest_started_at,
              ingest_finished_at,
              ingest_error,
              wix_notify_status,
              trim_status,
              trim_error,
              trim_output_s3_key
            FROM bronze.submission_context
            WHERE task_id = :t
            LIMIT 1
        """), {"t": task_id}).mappings().first()

    if not row:
        return jsonify({"ok": False, "error": "task_not_found", "task_id": task_id}), 404

    row = dict(row)
    ingest_started = row.get("ingest_started_at") is not None
    ingest_finished = row.get("ingest_finished_at") is not None
    ingest_error = row.get("ingest_error")

    if ingest_error:
        status = "failed"
    elif ingest_finished:
        status = "completed"
    elif ingest_started:
        status = "running"
    else:
        status = "pending"

    # Check if in-flight in this worker
    with _active_lock:
        active_here = task_id in _active_ingests

    return jsonify({
        "ok": True,
        "task_id": task_id,
        "ingest_status": status,
        "active_in_worker": active_here,
        "session_id": row.get("session_id"),
        "ingest_error": ingest_error,
        "wix_notify_status": row.get("wix_notify_status"),
        "trim_status": row.get("trim_status"),
    })
