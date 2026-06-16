# upload_app.py — Clean S3 → SportAI → Bronze (task_id-only)
# - Keeps: S3 upload, SportAI submit/status/cancel, presign, check-video
# - On status=completed: fetch result_url JSON and ingest via ingest_bronze_strict (task_id-only)
# - Uses bronze.submission_context keyed by task_id (no public schema)

import os, json, time, socket, sys, hashlib, re, threading, subprocess, uuid
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

from coach_invite import accept_bp as coach_accept_bp
app.register_blueprint(coach_accept_bp)

try:
    from ml_pipeline.api import ml_analysis_bp
    app.register_blueprint(ml_analysis_bp)
except ImportError:
    pass  # ML deps (cv2, torch) not available on Render — local-only

# ── CORS (cross-origin support for Wix iframe embeds) ──────────────
# Covers: /api/client/*, /api/support/*, /upload/api/*, /api/submit_s3_task, /media-room, etc.
CORS_PATHS = ("/api/client/", "/api/support/", "/upload/api/", "/api/submit_s3_task", "/api/coaches/accept-token", "/media-room", "/backoffice", "/analytics", "/portal", "/pricing", "/coach-accept")

@app.before_request
def handle_cors_preflight():
    """Return 204 for OPTIONS preflight on all CORS-enabled paths."""
    if request.method == "OPTIONS" and any(request.path.startswith(p) or request.path == p for p in CORS_PATHS):
        resp = app.make_response(("", 204))
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Client-Key, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

@app.after_request
def add_cors_headers(response):
    if any(request.path.startswith(p) or request.path == p for p in CORS_PATHS):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Client-Key, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    return response


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

# ---------- Technique API config ----------
TECHNIQUE_API_BASE = (os.getenv("TECHNIQUE_API_BASE") or "").strip().rstrip("/")
TECHNIQUE_API_TOKEN = (os.getenv("TECHNIQUE_API_TOKEN") or "").strip()
TECHNIQUE_API_TIMEOUT_S = int(os.getenv("TECHNIQUE_API_TIMEOUT_S", "300"))

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

# ---------- ml_analysis schema (idempotent on boot) ----------
# Creates ml_analysis.video_analysis_jobs, ball_detections, player_detections,
# match_analytics, training_corpus (Phase 5c.2). Previously called lazily from
# _t5_submit() — moved here so that gold.vw_dual_submit_pairs has its target
# schema available and the Phase 5c.2 pair-completion hook can INSERT into
# training_corpus from the very first deploy without waiting for a T5 submit.
try:
    from ml_pipeline.db_schema import ml_analysis_init  # noqa: E402
    ml_analysis_init(engine)
except Exception:
    app.logger.exception("ml_analysis_init() failed on boot — T5 / corpus tables may be missing")

# ---------- Gold presentation views (idempotent on boot) ----------
# Creates gold.vw_player, gold.vw_point, and all match_* presentation views
# used by the match analysis dashboards and the upcoming LLM coach.
# Each view is individually try/except'd inside gold_init_presentation() so
# a single failure cannot kill the service.
try:
    from gold_init import gold_init_presentation  # noqa: E402
    gold_init_presentation()
except Exception:
    app.logger.exception("gold_init_presentation() failed on boot — gold views may be stale")

# Legacy gold.vw_client_match_summary (feeds /api/client/matches sidebar). Will
# eventually be replaced by gold.match_kpi but currently live. Wrapped so a
# single view failure doesn't kill the service.
try:
    from db_init import gold_init as _gold_init_legacy  # noqa: E402
    _gold_init_legacy()
except Exception:
    app.logger.exception("legacy gold_init() (vw_client_match_summary) failed on boot")

# ---------- LLM Tennis Coach (idempotent on boot) ----------
try:
    from tennis_coach.coach_api import coach_bp
    from tennis_coach.init import init_tennis_coach
    init_tennis_coach()
    app.register_blueprint(coach_bp)
except Exception:
    app.logger.exception("tennis_coach init failed on boot")

# ---------- Support Bot (idempotent on boot) ----------
try:
    from support_bot.init import init_support_bot
    from support_bot.support_api import support_bp
    init_support_bot()
    app.register_blueprint(support_bp)
except Exception:
    app.logger.exception("support_bot init failed on boot")

# ---------- Orphan sweep (POST /ops/orphan-sweep) ----------
try:
    from cleanup import orphan_sweep_bp
    app.register_blueprint(orphan_sweep_bp)
except Exception:
    app.logger.exception("cleanup.orphan_sweep_bp register failed on boot")

# ---------- Read-only diagnostic SQL (POST /ops/diag/sql) ----------
# Tier-2 autonomy infrastructure — see diag_sql/sql_endpoint.py and
# docs/north_star.md §Autonomy infrastructure. OPS_KEY-gated, header-only,
# SELECT-only enforced via sqlparse + keyword denylist.
try:
    from diag_sql import diag_sql_bp
    app.register_blueprint(diag_sql_bp)
except Exception:
    app.logger.exception("diag_sql.diag_sql_bp register failed on boot")

# ---------- Internal admin cockpit (marketing_crm) — DARK by default ----------
# Registers /api/client/backoffice/cockpit/* only when COCKPIT_ENABLED=1. Creating the
# cockpit views on boot is additive + safe (core.* never touches billing/bronze data).
try:
    from marketing_crm.backoffice import register as register_cockpit
    if register_cockpit(app):
        from marketing_crm.backoffice import init_cockpit_views
        init_cockpit_views()
        app.logger.info("marketing_crm cockpit registered (COCKPIT_ENABLED=1)")
except Exception:
    app.logger.exception("marketing_crm cockpit register failed on boot")

# ---------- In-app feedback + NPS (marketing_crm) — DARK by default ----------
# Registers /api/client/feedback/* only when FEEDBACK_ENABLED=1.
try:
    from marketing_crm.feedback import register as register_feedback
    if register_feedback(app):
        app.logger.info("marketing_crm feedback registered (FEEDBACK_ENABLED=1)")
except Exception:
    app.logger.exception("marketing_crm feedback register failed on boot")

# ---------- Consent capture (marketing_crm) — DARK by default ----------
# Registers /api/client/consent/* only when CONSENT_ENABLED=1. Recording consent also creates the
# core identity (account/user/person) — the forward write-path into core.*.
try:
    from marketing_crm.consent import register as register_consent
    if register_consent(app):
        app.logger.info("marketing_crm consent registered (CONSENT_ENABLED=1)")
except Exception:
    app.logger.exception("marketing_crm consent register failed on boot")

# ---------- Page-view beacon (marketing_crm) — self-gates on TRACKING_ENABLED ----------
# Public POST /api/track/page for navigation analytics (sendBeacon). Records nothing unless
# TRACKING_ENABLED=1; always registered so the route exists.
try:
    from marketing_crm.tracking import register_beacon
    register_beacon(app)
except Exception:
    app.logger.exception("marketing_crm page beacon register failed on boot")

# ---------- Technique Analysis (idempotent on boot) ----------
try:
    from technique.db_schema import technique_bronze_init
    from technique.silver_technique import ensure_silver_schema
    from technique.gold_technique import init_technique_gold_views
    technique_bronze_init(engine)
    ensure_silver_schema(engine)
    init_technique_gold_views()
except Exception:
    app.logger.exception("technique init failed on boot")

# ---------- Bounce detector schema (ADR-01, idempotent on boot) ----------
try:
    from ml_pipeline.bounce_detector.db import init_bounce_schema
    with engine.begin() as conn:
        init_bounce_schema(conn)
except Exception:
    app.logger.exception("bounce_detector init failed on boot")

# ---------- Identity detector schema (ADR-03, idempotent on boot) ----------
try:
    from ml_pipeline.identity_detector.db import init_identity_schema
    with engine.begin() as conn:
        init_identity_schema(conn)
except Exception:
    app.logger.exception("identity_detector init failed on boot")


# ---------- S3 config (MANDATORY) ----------
AWS_REGION = os.getenv("AWS_REGION", "").strip() or None
S3_BUCKET  = os.getenv("S3_BUCKET", "").strip() or None
S3_PREFIX  = (os.getenv("S3_PREFIX", "incoming") or "incoming").strip().strip("/")
S3_GET_EXPIRES = int(os.getenv("S3_GET_EXPIRES", "604800"))  # 7 days

def _require_s3():
    if not (AWS_REGION and S3_BUCKET):
        raise RuntimeError("S3 is required: set AWS_REGION and S3_BUCKET env vars")

# ---------- T5 ML Pipeline (AWS Batch) ----------
BATCH_JOB_QUEUE = os.getenv("BATCH_JOB_QUEUE", "ten-fifty5-ml-queue")
BATCH_JOB_DEF = os.getenv("BATCH_JOB_DEF", "ten-fifty5-ml-pipeline")
BATCH_REGION = os.getenv("BATCH_REGION", "") or AWS_REGION  # primary Batch region

# Region failover priority for T5 Batch submission.
# Override via BATCH_REGIONS_PRIORITY env var (comma-separated, e.g. "eu-north-1,us-east-1").
# Default: primary BATCH_REGION first, then us-east-1 as fallback.
def _resolve_batch_regions_priority() -> list:
    raw = os.getenv("BATCH_REGIONS_PRIORITY", "").strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    fallbacks = []
    if BATCH_REGION:
        fallbacks.append(BATCH_REGION)
    if "us-east-1" not in fallbacks:
        fallbacks.append("us-east-1")
    return fallbacks
BATCH_REGIONS_PRIORITY = _resolve_batch_regions_priority()
T5_SPORT_TYPES = {"serve_practice", "rally_practice", "tennis_singles_t5"}
TECHNIQUE_SPORT_TYPES = {"technique_analysis"}
AUTO_DUAL_SUBMIT_T5 = os.getenv("AUTO_DUAL_SUBMIT_T5", "0").lower() in ("1", "true", "yes", "y")
# Phase 5c.2 — when T5 ingest completes for a `tennis_singles_t5` row whose SA
# pair is also complete, fire `export_sa_ball_positions` and record the result
# in ml_analysis.training_corpus. Default OFF so the code can ship dark; Tomo
# flips it on Render once the SA pair (8a5e0b5e / 2c1ad953) backfill is run.
AUTO_LABEL_DUAL_SUBMIT_PAIRS = os.getenv("AUTO_LABEL_DUAL_SUBMIT_PAIRS", "0").lower() in ("1", "true", "yes", "y")

# ---------- Ingest worker service ----------
INGEST_WORKER_BASE_URL = (os.getenv("INGEST_WORKER_BASE_URL") or "").strip().rstrip("/")
INGEST_WORKER_OPS_KEY = (os.getenv("INGEST_WORKER_OPS_KEY") or "").strip()
INGEST_WORKER_TIMEOUT_S = int(os.getenv("INGEST_WORKER_TIMEOUT_S", "10"))

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

# ==========================
# BILLING + ROLE GATE (RENDER SSoT)
# ==========================

def _upload_entitlement_gate(email: str, sport_type: str | None = None) -> tuple[bool, str]:
    """Server-side upload gate. See docs/pricing_strategy.md §5.

    - Match uploads (default) are gated by matches_remaining.
    - Technique uploads (sport_type in TECHNIQUE_SPORT_TYPES) are gated by
      techniques_remaining — which the free-trial signup bonus seeds with 5.
    - Paid subscribers pass as long as they have credits of the relevant type.
      (Subscription-ACTIVE alone is not sufficient — the plan's monthly matches
      are granted as credits; a subscriber who's used all their matches must
      top up or wait for refill, same as today.)
    - Coaches never upload.
    """
    e = (email or "").strip().lower()
    if not e:
        return False, "email_required"

    is_technique = (sport_type or "").strip().lower() in TECHNIQUE_SPORT_TYPES
    sport_known = bool((sport_type or "").strip())

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
        match_remaining = int(row.get("matches_remaining") or 0)
        account_id = int(row.get("account_id"))

        # Techniques come from a separate credit pool — not surfaced by
        # vw_customer_usage (match-only). Sum directly from grant/consumption.
        technique_remaining = 0
        try:
            trow = conn.execute(sql_text("""
                WITH g AS (
                  SELECT COALESCE(SUM(techniques_granted), 0) AS granted
                  FROM billing.entitlement_grant
                  WHERE account_id = :aid
                    AND is_active = true
                    AND (valid_from IS NULL OR valid_from <= now())
                    AND (valid_to   IS NULL OR now() < valid_to)
                ),
                c AS (
                  SELECT COALESCE(SUM(consumed_techniques), 0) AS consumed
                  FROM billing.entitlement_consumption
                  WHERE account_id = :aid
                )
                SELECT GREATEST((SELECT granted FROM g) - (SELECT consumed FROM c), 0) AS remaining
            """), {"aid": account_id}).mappings().first()
            technique_remaining = int((trow or {}).get("remaining") or 0)
        except Exception:
            technique_remaining = 0

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

    # With a known sport_type, check the matching credit pool precisely.
    # Without sport_type (multipart/presign before submit), pass if the user
    # has credits of EITHER type — the submit endpoint re-checks precisely.
    if sport_known:
        pool_remaining = technique_remaining if is_technique else match_remaining
    else:
        pool_remaining = match_remaining + technique_remaining

    if pool_remaining > 0:
        return True, "ok"
    if subscription_status == "ACTIVE":
        # Active subscriber, 0 credits of the relevant kind — monthly cap hit.
        return False, "insufficient_credits"
    return False, "insufficient_credits"


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
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ses_notified_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ses_notify_error TEXT",

        # --- NEW: typed score + timing + SR fields (idempotent) ---
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set1_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set1_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set2_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set2_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set3_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set3_games INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS first_server TEXT",
        f"ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS sport_type TEXT DEFAULT '{DEFAULT_SPORT_TYPE}'",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS a_starts_near BOOLEAN DEFAULT TRUE",

        # PowerBI integration removed 2026-05-20. DROP IF EXISTS is idempotent
        # (no-op after the first deploy drops them). Safe to delete this block
        # once we're confident every running replica has run boot at least once.
        "ALTER TABLE bronze.submission_context DROP COLUMN IF EXISTS pbi_refresh_status",
        "ALTER TABLE bronze.submission_context DROP COLUMN IF EXISTS pbi_refresh_started_at",
        "ALTER TABLE bronze.submission_context DROP COLUMN IF EXISTS pbi_refresh_finished_at",
        "ALTER TABLE bronze.submission_context DROP COLUMN IF EXISTS pbi_refresh_error",
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
    sport_type: str | None = None,
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
              sport_type,
              a_starts_near
            ) VALUES (
              :task_id, :email, :customer_name, :match_date, :start_time, :location,
              :player_a_name, :player_b_name, :player_a_utr, :player_b_utr,
              :video_url, :share_url, :raw_meta,
              :s3_bucket, :s3_key,

              :a1, :b1,
              :a2, :b2,
              :a3, :b3,
              :first_server,
              :sport_type,
              :a_starts_near
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
              sport_type=EXCLUDED.sport_type,
              a_starts_near=EXCLUDED.a_starts_near
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
            "sport_type": sport_type or DEFAULT_SPORT_TYPE,
            "a_starts_near": (m.get("a_starts_near") if m.get("a_starts_near") is not None else True),
        })


def _set_status_cache(conn, task_id: str, status: str | None, result_url: str | None):
    conn.execute(sql_text("""
        UPDATE bronze.submission_context
           SET last_status     = :s,
               last_status_at  = now(),
               last_result_url = :r
         WHERE task_id = :t
    """), {"t": task_id, "s": status, "r": result_url})

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
              sport_type
            FROM bronze.submission_context
            WHERE task_id = :t
            LIMIT 1
        """), {"t": task_id}).mappings().first() or {}


def _resolve_result_url_for_task(task_id: str) -> str | None:
    """
    Resolve result_url in the safest order:
    - Technique jobs: check submission_context status, return sentinel URL
    - T5 jobs: check ml_analysis status, return sentinel URL
    - SportAI jobs: fresh status lookup, then cached DB value
    """
    sc = _load_submission_context_row(task_id)

    # Technique jobs don't use result_url — the background thread handles everything.
    if sc.get("sport_type") in TECHNIQUE_SPORT_TYPES:
        return None

    if sc.get("sport_type") in T5_SPORT_TYPES:
        live = _t5_status(task_id)
        if live.get("status") == "completed":
            return f"t5://complete/{task_id}"
        return None

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

    cached = str(sc.get("last_result_url") or "").strip()
    if cached:
        return cached

    return None

def _derive_pipeline_stage(
    sportai_status: str | None,
    ingest_started: bool,
    ingest_finished: bool,
    ingest_error: str | None,
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

# ==========================
# CUSTOMER EMAIL NOTIFICATION (SES)
# ==========================
def _notify_ses_completion(task_id: str) -> None:
    """Send video analysis complete email via SES. Idempotent via ses_notified_at."""
    try:
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            row = conn.execute(sql_text("""
                SELECT ses_notified_at FROM bronze.submission_context
                WHERE task_id = :t LIMIT 1
            """), {"t": task_id}).mappings().first()
            if row and row.get("ses_notified_at"):
                return

        # Fetch customer + match details for the email
        with engine.connect() as conn:
            row = conn.execute(sql_text("""
                SELECT email, customer_name, player_a_name, player_b_name,
                       match_date, location
                FROM bronze.submission_context
                WHERE task_id = :t
                LIMIT 1
            """), {"t": task_id}).mappings().first()

        if not row or not row.get("email"):
            app.logger.warning("SES notify: no email for task_id=%s", task_id)
            return

        from coach_invite.video_complete_email import send_completion_email
        result = send_completion_email(
            task_id=task_id,
            customer_email=(row["email"] or "").strip(),
            customer_name=(row.get("customer_name") or "").strip(),
            player_a=(row.get("player_a_name") or "").strip(),
            player_b=(row.get("player_b_name") or "").strip(),
            match_date=str(row["match_date"]) if row.get("match_date") else "",
            location=(row.get("location") or "").strip(),
        )

        if result.get("ok"):
            app.logger.info("SES completion email sent task_id=%s", task_id)
            with engine.begin() as conn:
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                    SET ses_notified_at = now(), ses_notify_error = NULL
                    WHERE task_id = :t
                """), {"t": task_id})
        else:
            err = result.get("error", "unknown")
            app.logger.warning("SES completion email failed task_id=%s: %s", task_id, err)
            with engine.begin() as conn:
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                    SET ses_notify_error = :e
                    WHERE task_id = :t
                """), {"t": task_id, "e": str(err)[:4000]})

    except Exception as e:
        app.logger.exception("SES notify error task_id=%s: %s", task_id, e)
        try:
            with engine.begin() as conn:
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                    SET ses_notify_error = :e
                    WHERE task_id = :t
                """), {"t": task_id, "e": f"{e.__class__.__name__}: {e}"[:4000]})
        except Exception:
            pass


# ==========================
# SPORTAI HTTP
# ==========================
def _iter_submit_endpoints():
    for base in SPORTAI_BASES:
        for path in SPORTAI_SUBMIT_PATHS:
            yield f"{base.rstrip('/')}/{path.lstrip('/')}"

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


def _t5_submit(s3_key: str, email: str = None, meta: dict = None,
               sport_type: str = "serve_practice") -> str:
    """
    Submit a T5 ML pipeline job via AWS Batch.
    Returns a job_id that doubles as the task_id for bronze.submission_context.
    """
    import uuid
    from sqlalchemy import text as sql_text

    job_id = str(uuid.uuid4())

    # 1. Ensure ml_analysis schema exists
    try:
        from ml_pipeline.db_schema import ml_analysis_init
        ml_analysis_init(engine)
    except ImportError:
        pass  # ml_pipeline DB deps not available — schema must already exist

    # 2. Create job row
    with engine.begin() as conn:
        conn.execute(sql_text("""
            INSERT INTO ml_analysis.video_analysis_jobs
                (job_id, task_id, s3_key, status, current_stage, progress_pct)
            VALUES (:job_id, :job_id, :s3_key, 'queued', 'queued', 0)
            ON CONFLICT (job_id) DO NOTHING
        """), {"job_id": job_id, "s3_key": s3_key})

    # 3. Submit AWS Batch job — try regions in priority order (eu-north-1 first by default)
    is_practice = sport_type in {"serve_practice", "rally_practice"}
    cmd = ["--job-id", job_id, "--s3-key", s3_key]
    if is_practice:
        cmd.append("--practice")

    # Pass env from the Render-side main app through to the Batch container at submit time.
    # Keeps Render as the single source of truth for DB/S3 config and avoids baking secrets
    # into the job definition. Overrides the `environment` block from the registered job def.
    #
    # Note: AWS Batch runs outside Render's VPC, so it CANNOT use the internal-hostname
    # `DATABASE_URL` (short form, e.g. `...@dpg-xxx-a/db`). It needs the external URL with
    # full FQDN + sslmode. Prefer `EXTERNAL_DATABASE_URL` (set explicitly in Render for this
    # purpose); fall back to `DATABASE_URL` only for local/dev environments where one URL
    # serves both roles.
    env_overrides = [
        {"name": "DATABASE_URL",
         "value": (os.getenv("EXTERNAL_DATABASE_URL")
                   or os.getenv("DATABASE_URL")
                   or os.getenv("POSTGRES_URL")
                   or os.getenv("DB_URL") or "")},
        {"name": "S3_BUCKET",  "value": os.getenv("S3_BUCKET")  or ""},
        {"name": "AWS_REGION", "value": os.getenv("AWS_REGION") or ""},
    ]

    batch_job_id = None
    used_region = None
    last_error = None
    for region in BATCH_REGIONS_PRIORITY:
        try:
            batch = boto3.client("batch", region_name=region)
            resp = batch.submit_job(
                jobName=f"t5-{sport_type[:5]}-{job_id[:8]}",
                jobQueue=BATCH_JOB_QUEUE,
                jobDefinition=BATCH_JOB_DEF,
                containerOverrides={"command": cmd, "environment": env_overrides},
                tags={
                    "Project": "TEN-FIFTY5",
                    "Environment": "production",
                    "JobId": job_id,
                    "SportType": sport_type,
                },
            )
            batch_job_id = resp["jobId"]
            used_region = region
            app.logger.info("T5 SUBMIT region=%s OK job_id=%s batch_job_id=%s",
                             region, job_id, batch_job_id)
            break
        except Exception as e:
            last_error = e
            app.logger.warning("T5 SUBMIT region=%s FAILED job_id=%s err=%s — trying next region",
                                region, job_id, e)
            continue

    if batch_job_id is None:
        raise RuntimeError(
            f"T5 SUBMIT failed across all regions {BATCH_REGIONS_PRIORITY}: {last_error}"
        )

    # 4. Store Batch job ID + region
    with engine.begin() as conn:
        conn.execute(sql_text("""
            UPDATE ml_analysis.video_analysis_jobs
            SET batch_job_id = :bid, submitted_region = :region, updated_at = now()
            WHERE job_id = :jid
        """), {"jid": job_id, "bid": batch_job_id, "region": used_region})

    app.logger.info("T5 SUBMIT job_id=%s batch_job_id=%s region=%s s3_key=%s sport_type=%s",
                     job_id, batch_job_id, used_region, s3_key, sport_type)
    return job_id


def _t5_status(task_id: str) -> dict:
    """
    Poll T5 job status from ml_analysis.video_analysis_jobs.
    Returns the same shape as _sportai_status() for unified polling.
    """
    with engine.connect() as conn:
        row = conn.execute(sql_text("""
            SELECT status, current_stage, progress_pct, error_message
            FROM ml_analysis.video_analysis_jobs
            WHERE job_id = :jid OR task_id = :jid
            ORDER BY created_at DESC LIMIT 1
        """), {"jid": task_id}).mappings().first()

    if not row:
        return {"status": "unknown", "sportai_progress_pct": None, "result_url": None}

    status_map = {"queued": "queued", "processing": "processing",
                  "complete": "completed", "failed": "failed"}
    mapped = status_map.get(row["status"], row["status"])

    # Sentinel result_url triggers existing auto-ingest logic
    result_url = f"t5://complete/{task_id}" if mapped == "completed" else None

    return {
        "task_id": task_id,
        "status": mapped,
        "result_url": result_url,
        "sportai_progress_pct": row["progress_pct"] or 0,
        "message": row["error_message"],
    }


def _t5_cancel(task_id: str) -> dict:
    """Cancel a T5 AWS Batch job — checks the region the job was actually submitted to."""
    with engine.connect() as conn:
        row = conn.execute(sql_text("""
            SELECT job_id, batch_job_id, submitted_region FROM ml_analysis.video_analysis_jobs
            WHERE job_id = :jid OR task_id = :jid
            ORDER BY created_at DESC LIMIT 1
        """), {"jid": task_id}).mappings().first()

    if not row:
        raise RuntimeError(f"T5 job not found for task_id={task_id}")

    batch_job_id = row.get("batch_job_id")
    # Use the region the job was submitted to, fall back to first priority region
    region = row.get("submitted_region") or (BATCH_REGIONS_PRIORITY[0] if BATCH_REGIONS_PRIORITY else BATCH_REGION)
    if batch_job_id:
        batch = boto3.client("batch", region_name=region)
        batch.terminate_job(jobId=batch_job_id, reason="Cancelled by user")
        app.logger.info("T5 CANCEL batch_job_id=%s task_id=%s region=%s", batch_job_id, task_id, region)

    with engine.begin() as conn:
        conn.execute(sql_text("""
            UPDATE ml_analysis.video_analysis_jobs
            SET status = 'failed', error_message = 'Cancelled by user', updated_at = now()
            WHERE job_id = :jid
        """), {"jid": row["job_id"]})

    return {"status": "cancelled", "batch_job_id": batch_job_id}


# ==========================
# TECHNIQUE API
# ==========================

def _technique_submit(s3_key: str, email: str = None, meta: dict = None) -> str:
    """
    Submit a technique analysis job. Creates a task_id, then spawns a single
    background thread that runs the entire pipeline end-to-end:
    download video → call technique API → bronze → silver → gold → trim → notify.

    Same pattern as _do_ingest_t5: one background thread, no intermediate S3 storage,
    status tracked via bronze.submission_context (same columns as SportAI/T5).
    """
    task_id = str(uuid.uuid4())

    m = meta or {}
    technique_meta = {
        "sport": m.get("sport") or "tennis",
        "swing_type": m.get("swing_type") or "forehand_drive",
        "dominant_hand": m.get("dominant_hand") or "right",
        "player_height_mm": int(m.get("player_height_mm") or 1800),
    }

    def _worker():
        try:
            _technique_run_pipeline(task_id, s3_key, technique_meta)
        except Exception as e:
            app.logger.exception("TECHNIQUE PIPELINE FAILED task_id=%s: %s", task_id, e)
            try:
                with engine.begin() as conn:
                    _ensure_submission_context_schema(conn)
                    conn.execute(sql_text("""
                        UPDATE bronze.submission_context
                        SET last_status = 'failed', last_status_at = now(),
                            ingest_error = :err, ingest_finished_at = now()
                        WHERE task_id = :t
                    """), {"t": task_id, "err": f"{e.__class__.__name__}: {e}"[:4000]})
            except Exception:
                pass

    t = threading.Thread(target=_worker, name=f"technique-{task_id[:8]}", daemon=True)
    t.start()

    app.logger.info(
        "TECHNIQUE SUBMIT task_id=%s s3_key=%s sport=%s swing_type=%s",
        task_id, s3_key, technique_meta["sport"], technique_meta["swing_type"],
    )
    return task_id


def _technique_run_pipeline(task_id: str, s3_key: str, technique_meta: dict):
    """
    Single background thread that runs the full technique pipeline.
    Mirrors the SportAI ingest pattern: API call → bronze → silver → trim → notify.
    No intermediate S3 storage — the JSON payload stays in memory.
    """
    from technique.api_client import call_technique_api

    # Mark started
    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        conn.execute(sql_text("""
            UPDATE bronze.submission_context
            SET last_status = 'processing', last_status_at = now(),
                ingest_started_at = COALESCE(ingest_started_at, now()),
                ingest_finished_at = NULL, ingest_error = NULL
            WHERE task_id = :t
        """), {"t": task_id})

    # ── STEP 1: Download video from S3 ────────────────────────
    app.logger.info("TECHNIQUE task_id=%s step=download_video", task_id)
    s3 = _s3_client()
    s3_obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
    video_bytes = s3_obj["Body"].read()
    filename = s3_key.rsplit("/", 1)[-1] if "/" in s3_key else s3_key

    # ── STEP 2: Call technique API (streaming, 30-120s) ───────
    app.logger.info("TECHNIQUE task_id=%s step=call_api file_size=%d", task_id, len(video_bytes))
    payload = call_technique_api(
        video_bytes=video_bytes,
        filename=filename,
        uid=task_id,
        **technique_meta,
    )
    del video_bytes  # free memory

    api_status = (payload.get("status") or "").lower()
    if api_status in ("failed", "error"):
        errors = payload.get("errors") or []
        err_msg = "; ".join(str(e) for e in errors) or "technique API returned failure"
        with engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                SET last_status = 'failed', last_status_at = now(),
                    ingest_error = :err, ingest_finished_at = now()
                WHERE task_id = :t
            """), {"t": task_id, "err": err_msg[:4000]})
        return

    # ── STEP 3: Bronze ingest (same pattern as ingest_bronze_strict) ──
    app.logger.info("TECHNIQUE task_id=%s step=bronze_ingest", task_id)
    from technique.bronze_ingest_technique import ingest_technique_bronze
    with engine.begin() as conn:
        ingest_technique_bronze(conn, payload, task_id=task_id, replace=True)

    # Store technique-specific metadata from the request
    with engine.begin() as conn:
        conn.execute(sql_text("""
            UPDATE bronze.technique_analysis_metadata
            SET sport = :sport, swing_type = :swing_type,
                dominant_hand = :hand, player_height_mm = :height
            WHERE task_id = :t
        """), {
            "t": task_id,
            "sport": technique_meta["sport"],
            "swing_type": technique_meta["swing_type"],
            "hand": technique_meta["dominant_hand"],
            "height": technique_meta["player_height_mm"],
        })

    del payload  # free memory

    # ── STEP 4: Silver build (same pattern as build_silver_v2) ──
    app.logger.info("TECHNIQUE task_id=%s step=silver_build", task_id)
    from technique.silver_technique import build_silver_technique
    build_silver_technique(task_id=task_id, engine=engine, replace=True)

    # ── STEP 5: Video — copy to trim folder (fire-and-forget) ──
    try:
        _technique_store_footage(task_id)
    except Exception as e:
        app.logger.warning("TECHNIQUE task_id=%s trim failed (non-fatal): %s", task_id, e)

    # ── STEP 6: Mark complete ─────────────────────────────────
    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        conn.execute(sql_text("""
            UPDATE bronze.submission_context
            SET session_id = :task_id,
                last_status = 'completed', last_status_at = now(),
                ingest_finished_at = now(), ingest_error = NULL
            WHERE task_id = :t
        """), {"t": task_id, "task_id": task_id})

    # ── STEP 7: Customer notification (same as SportAI) ───────
    try:
        _notify_ses_completion(task_id)
    except Exception as e:
        app.logger.warning("TECHNIQUE task_id=%s notify failed (non-fatal): %s", task_id, e)

    app.logger.info("TECHNIQUE PIPELINE COMPLETE task_id=%s", task_id)


def _technique_status(task_id: str) -> dict:
    """
    Poll technique job status from bronze.submission_context.
    Same columns as SportAI/T5 — no separate tracking table needed.
    """
    with engine.connect() as conn:
        row = conn.execute(sql_text("""
            SELECT last_status, ingest_error, session_id, ingest_finished_at
            FROM bronze.submission_context
            WHERE task_id = :t
        """), {"t": task_id}).mappings().first()

    if not row:
        return {"status": "unknown", "sportai_progress_pct": None, "result_url": None}

    status = _normalize_sportai_status(row.get("last_status")) or "unknown"

    # Technique doesn't use result_url — the background thread handles everything.
    # But dashboard_ready is derived from session_id + ingest_finished_at,
    # so we don't need result_url for the auto-ingest gate.
    return {
        "task_id": task_id,
        "status": status,
        "result_url": None,
        "sportai_progress_pct": 100 if status == "completed" else (50 if status == "processing" else 0),
        "message": row.get("ingest_error"),
    }


def _technique_cancel(task_id: str) -> dict:
    """Cancel a technique analysis job."""
    with engine.begin() as conn:
        conn.execute(sql_text("""
            UPDATE bronze.submission_context
            SET last_status = 'failed', last_status_at = now(),
                ingest_error = 'Cancelled by user'
            WHERE task_id = :t
        """), {"t": task_id})
    return {"status": "cancelled"}


def _technique_store_footage(task_id: str):
    """Copy the original technique video to the trim folder for online storage."""
    with engine.connect() as conn:
        row = conn.execute(sql_text("""
            SELECT s3_bucket, s3_key FROM bronze.submission_context WHERE task_id = :t
        """), {"t": task_id}).mappings().first()

    if not row or not row.get("s3_key"):
        return

    src_key = row["s3_key"]
    bucket = row.get("s3_bucket") or S3_BUCKET
    dest_key = f"trimmed/{task_id}/technique.mp4"

    s3 = _s3_client()
    s3.copy_object(
        Bucket=bucket,
        Key=dest_key,
        CopySource={"Bucket": bucket, "Key": src_key},
    )
    with engine.begin() as conn:
        conn.execute(sql_text("""
            UPDATE bronze.submission_context
            SET trim_status = 'completed',
                trim_output_s3_key = :dest,
                trim_finished_at = now()
            WHERE task_id = :t
        """), {"t": task_id, "dest": dest_key})
    app.logger.info("TECHNIQUE FOOTAGE STORED task_id=%s dest=%s", task_id, dest_key)


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
        # STEP 6: NOTIFY CUSTOMER (SES email)
        # -------------------------
        _notify_ses_completion(task_id)

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


def _auto_dual_submit_t5(task_id: str) -> None:
    """
    Fire-and-forget: submit the same video as a T5 job after SportAI ingest is triggered.

    Guards:
    - AUTO_DUAL_SUBMIT_T5 env flag must be enabled (default OFF).
    - Only for sport_type='tennis_singles' — never practice or T5 jobs.
    - Idempotent: skips if a T5 job already exists for this s3_key in ml_analysis.video_analysis_jobs.

    Errors are logged but never propagate — must not affect the SportAI ingest flow.
    """
    if not AUTO_DUAL_SUBMIT_T5:
        return

    try:
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            row = conn.execute(sql_text("""
                SELECT s3_key, sport_type, email, player_a_name, player_b_name
                  FROM bronze.submission_context
                 WHERE task_id = :t
                 LIMIT 1
            """), {"t": task_id}).mappings().first()

        if not row:
            app.logger.warning(
                "AUTO_DUAL_SUBMIT_T5 task_id=%s — no submission_context row found, skipping", task_id
            )
            return

        sport_type = row.get("sport_type") or ""
        s3_key = row.get("s3_key") or ""
        email = row.get("email") or ""

        if sport_type != "tennis_singles":
            app.logger.debug(
                "AUTO_DUAL_SUBMIT_T5 task_id=%s — sport_type=%s is not tennis_singles, skipping",
                task_id, sport_type,
            )
            return

        if not s3_key:
            app.logger.warning(
                "AUTO_DUAL_SUBMIT_T5 task_id=%s — no s3_key in submission_context, skipping", task_id
            )
            return

        t5_job_id = _manual_dual_submit_t5_core(s3_key, email, row.get("player_a_name"), row.get("player_b_name"))
        if t5_job_id:
            app.logger.info(
                "AUTO_DUAL_SUBMIT_T5 task_id=%s — T5 job submitted t5_job_id=%s", task_id, t5_job_id
            )

    except Exception as e:
        app.logger.exception(
            "AUTO_DUAL_SUBMIT_T5 task_id=%s — error during dual-submit (SportAI flow unaffected): %s",
            task_id, e,
        )


def _manual_dual_submit_t5_core(
    s3_key: str,
    email: str,
    player_a_name: str | None = None,
    player_b_name: str | None = None,
) -> str | None:
    """
    Shared logic for auto and manual dual-submit:
    1. Idempotency check — skip if a T5 job already exists for this s3_key.
    2. Submit Batch job via _t5_submit().
    3. Create submission_context row for the T5 job so auto-ingest can fire.

    Returns the new T5 task_id (job_id), or None if skipped.
    """
    # Idempotency: skip if a T5 job already exists for this s3_key
    try:
        with engine.connect() as conn:
            existing = conn.execute(sql_text("""
                SELECT job_id FROM ml_analysis.video_analysis_jobs
                 WHERE s3_key = :key
                 LIMIT 1
            """), {"key": s3_key}).mappings().first()
    except Exception:
        # ml_analysis schema might not exist yet — treat as no existing job
        existing = None

    if existing:
        app.logger.info(
            "DUAL_SUBMIT_T5 s3_key=%s — T5 job already exists (job_id=%s), skipping",
            s3_key, existing["job_id"],
        )
        return None

    app.logger.info("DUAL_SUBMIT_T5 s3_key=%s email=%s — submitting to T5 pipeline", s3_key, email)
    t5_job_id = _t5_submit(s3_key, sport_type="tennis_singles_t5")

    # Build a minimal meta dict so _store_submission_context can copy player names
    meta = {}
    if player_a_name:
        meta["player_a_name"] = player_a_name
    if player_b_name:
        meta["player_b_name"] = player_b_name

    _store_submission_context(
        task_id=t5_job_id,
        email=email,
        meta=meta,
        video_url=s3_key,   # no separate video_url for T5 dual-submit
        share_url=s3_key,
        s3_bucket=S3_BUCKET,
        s3_key=s3_key,
        sport_type="tennis_singles_t5",
    )

    with engine.begin() as conn:
        _ensure_submission_context_schema(conn)
        _set_status_cache(conn, t5_job_id, "queued", None)

    app.logger.info(
        "DUAL_SUBMIT_T5 s3_key=%s — submission_context created for t5_job_id=%s", s3_key, t5_job_id
    )
    return t5_job_id


def _manual_dual_submit_t5(sportai_task_id: str) -> dict:
    """
    Manually trigger a T5 dual-submit for an existing SportAI task_id.
    Reads the s3_key and player names from the SportAI submission_context,
    then calls _manual_dual_submit_t5_core().

    Returns a dict with keys: status ('submitted' or 'skipped'), t5_task_id (if submitted),
    reason (if skipped), or raises on error.
    """
    with engine.connect() as conn:
        _ensure_submission_context_schema(conn)
        row = conn.execute(sql_text("""
            SELECT s3_key, sport_type, email, player_a_name, player_b_name
              FROM bronze.submission_context
             WHERE task_id = :t
             LIMIT 1
        """), {"t": sportai_task_id}).mappings().first()

    if not row:
        return {"status": "skipped", "reason": "no submission_context row for that task_id"}

    sport_type = row.get("sport_type") or ""
    s3_key = row.get("s3_key") or ""
    email = row.get("email") or ""

    if sport_type != "tennis_singles":
        return {
            "status": "skipped",
            "reason": f"sport_type={sport_type!r} is not tennis_singles",
        }

    if not s3_key:
        return {"status": "skipped", "reason": "no s3_key in submission_context"}

    t5_task_id = _manual_dual_submit_t5_core(
        s3_key, email, row.get("player_a_name"), row.get("player_b_name")
    )

    if t5_task_id is None:
        return {"status": "skipped", "reason": "T5 job already exists for this s3_key"}

    return {"status": "submitted", "t5_task_id": t5_task_id}


def _label_one_kind(
    sa_task_id: str,
    t5_task_id: str,
    video_s3_key: str,
    label_kind: str,
    exporter,
    s3_key: str,
) -> dict:
    """Idempotent export of ONE label_kind for one (SA, T5) pair.

    `exporter` is a callable matching the
    `export_sa_ball_positions(t5_task_id, sa_task_id, engine) -> dict` shape;
    its returned dict must carry `label_count` and `role_breakdown`.
    `s3_key` is the destination key under S3_BUCKET (no leading slash).

    Returns a status dict: {status, label_count?, label_s3_uri?, reason?}.
    Never raises — orchestrator expects a dict.
    """
    try:
        with engine.connect() as conn:
            existing = conn.execute(sql_text("""
                SELECT id FROM ml_analysis.training_corpus
                 WHERE sa_task_id = :sa AND t5_task_id = :t5 AND label_kind = :kind
                 LIMIT 1
            """), {"sa": sa_task_id, "t5": t5_task_id, "kind": label_kind}).first()
        if existing:
            return {"status": "skipped", "reason": "training_corpus row already exists"}

        labels = exporter(t5_task_id=t5_task_id, sa_task_id=sa_task_id, engine=engine)

        if not S3_BUCKET:
            return {"status": "error", "reason": "S3_BUCKET not configured"}

        body = json.dumps(labels, indent=2).encode("utf-8")
        _s3_client().put_object(
            Bucket=S3_BUCKET, Key=s3_key, Body=body, ContentType="application/json",
        )

        label_s3_uri = f"s3://{S3_BUCKET}/{s3_key}"
        video_s3_uri = f"s3://{S3_BUCKET}/{video_s3_key}" if video_s3_key else ""
        with engine.begin() as conn:
            conn.execute(sql_text("""
                INSERT INTO ml_analysis.training_corpus (
                    sa_task_id, t5_task_id, label_kind,
                    label_s3_key, video_s3_key, label_count, role_breakdown
                ) VALUES (
                    :sa, :t5, :kind,
                    :label_uri, :video_uri, :label_count, CAST(:role_breakdown AS JSONB)
                )
                ON CONFLICT (sa_task_id, t5_task_id, label_kind) DO NOTHING
            """), {
                "sa": sa_task_id,
                "t5": t5_task_id,
                "kind": label_kind,
                "label_uri": label_s3_uri,
                "video_uri": video_s3_uri,
                "label_count": labels["label_count"],
                "role_breakdown": json.dumps(labels.get("role_breakdown") or {}),
            })

        return {
            "status": "labeled",
            "label_count": labels["label_count"],
            "label_s3_uri": label_s3_uri,
        }
    except Exception as e:
        app.logger.exception(
            "PAIR_LABEL kind=%s sa=%s t5=%s — error during label export: %s",
            label_kind, sa_task_id, t5_task_id, e,
        )
        return {"status": "error", "reason": f"{e.__class__.__name__}: {e}"}


def _label_pair_now(t5_task_id: str) -> dict:
    """
    Phase 5c.2 — ungated worker. Idempotent label export for one completed
    (SA, T5) pair. Used by both the auto hook (gated by env flag) and the
    /ops/backfill-pair-labels endpoint (ungated, explicit).

    Exports three label kinds per pair, each idempotent independently:
      - 'ball_position'      — bronze.ball_bounce              -> TrackNet ball-position trainer
      - 'stroke_classifier'  — bronze.player_swing             -> ADR-02 swing-type classifier
      - 'serve'              — bronze.player_swing serves      -> serve_detector v2 (Stream 3)

    Returns: {status, sa_task_id?, t5_task_id?, reason?, kinds: {kind: {...}},
              label_count?, label_s3_uri?}. The top-level `label_count` /
    `label_s3_uri` mirror the FIRST newly-labeled kind for back-compat with
    existing log lines; per-kind detail lives under `kinds`. Never raises.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(sql_text("""
                SELECT sa_task_id, t5_task_id, s3_key
                  FROM gold.vw_dual_submit_pairs
                 WHERE t5_task_id = :t
                   AND pair_complete = TRUE
                 LIMIT 1
            """), {"t": t5_task_id}).mappings().first()

        if not row:
            return {"status": "skipped", "reason": "no completed SA pair"}

        sa_task_id = row["sa_task_id"]
        video_s3_key = row["s3_key"] or ""

        from ml_pipeline.training.label_ball_positions import export_sa_ball_positions
        from ml_pipeline.training.label_swing_types import export_sa_swing_types
        from ml_pipeline.training.label_serves import export_sa_serves

        ball = _label_one_kind(
            sa_task_id, t5_task_id, video_s3_key,
            label_kind="ball_position",
            exporter=export_sa_ball_positions,
            s3_key=f"training/labels/{t5_task_id}_ball_positions.json",
        )
        swing = _label_one_kind(
            sa_task_id, t5_task_id, video_s3_key,
            label_kind="stroke_classifier",
            exporter=export_sa_swing_types,
            s3_key=f"training/labels/{t5_task_id}_swing_types.json",
        )
        serve = _label_one_kind(
            sa_task_id, t5_task_id, video_s3_key,
            label_kind="serve",
            exporter=export_sa_serves,
            s3_key=f"training/labels/{t5_task_id}_serves.json",
        )

        kinds = {"ball_position": ball, "stroke_classifier": swing, "serve": serve}
        any_new = any(k["status"] == "labeled" for k in kinds.values())
        any_error = any(k["status"] == "error" for k in kinds.values())
        overall = "labeled" if any_new else ("error" if any_error else "skipped")

        out = {
            "status": overall,
            "sa_task_id": sa_task_id,
            "t5_task_id": t5_task_id,
            "kinds": kinds,
        }
        # Back-compat: hoist the first newly-labeled kind's fields to top level
        primary = next(
            (k for k in (ball, swing, serve) if k["status"] == "labeled"), None
        )
        if primary is not None:
            out["label_count"] = primary.get("label_count")
            out["label_s3_uri"] = primary.get("label_s3_uri")
        if overall == "skipped":
            out["reason"] = "training_corpus rows already exist for all kinds"
        elif overall == "error":
            # Surface the first error reason for callers that only read top-level
            first_err = next(
                (k for k in kinds.values() if k["status"] == "error"), {}
            )
            out["reason"] = first_err.get("reason", "unknown error")
        return out

    except Exception as e:
        app.logger.exception("PAIR_LABEL t5=%s — error during label export: %s", t5_task_id, e)
        return {
            "status": "error",
            "reason": f"{e.__class__.__name__}: {e}",
            "t5_task_id": t5_task_id,
        }


def _dual_submit_pair_complete_hook(t5_task_id: str) -> None:
    """
    Phase 5c.2 pair-completion hook. Fired fire-and-forget from the end of
    `_do_ingest_t5` whenever a `tennis_singles_t5` row finishes ingest.

    Guards:
    - `AUTO_LABEL_DUAL_SUBMIT_PAIRS` env flag must be enabled (default OFF).
    - Idempotent via the UNIQUE (sa, t5, label_kind) constraint on
      ml_analysis.training_corpus.
    - All errors swallowed — must not affect the T5 ingest flow.

    The actual work lives in `_label_pair_now`; this is the env-flag gate
    and result-logging wrapper.
    """
    if not AUTO_LABEL_DUAL_SUBMIT_PAIRS:
        return

    try:
        result = _label_pair_now(t5_task_id)
        if result["status"] == "labeled":
            kinds = result.get("kinds") or {}
            per_kind = ", ".join(
                f"{k}={v.get('label_count', '?')}"
                for k, v in kinds.items() if v.get("status") == "labeled"
            ) or f"label_count={result.get('label_count')}"
            app.logger.info(
                "PAIR_LABEL_HOOK sa=%s t5=%s — exported %s",
                result["sa_task_id"], result["t5_task_id"], per_kind,
            )
        else:
            app.logger.info(
                "PAIR_LABEL_HOOK t5=%s — %s (%s)",
                t5_task_id, result["status"], result.get("reason", ""),
            )
    except Exception as e:
        app.logger.exception(
            "PAIR_LABEL_HOOK t5=%s — error (T5 ingest flow unaffected): %s",
            t5_task_id, e,
        )


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

    # T5 jobs: lightweight ingest (data already in ml_analysis.*)
    if str(result_url or "").startswith("t5://"):
        ok = _do_ingest_t5(task_id)
        app.logger.info("T5 INGEST DONE task_id=%s ok=%s", task_id, ok)
        return True

    # SportAI jobs: delegate to ingest worker service
    try:
        out = _delegate_to_ingest_worker(task_id, result_url)
        app.logger.info(
            "INGEST DELEGATED task_id=%s worker_response=%s",
            task_id, out,
        )
        threading.Thread(target=_auto_dual_submit_t5, args=(task_id,), daemon=True).start()
        return True

    except Exception as e:
        app.logger.exception(
            "INGEST WORKER UNREACHABLE task_id=%s error=%s — falling back to in-process",
            task_id, e,
        )
        # Fallback: run in-process so ingest still happens
        ok = _do_ingest(task_id, result_url)
        app.logger.info("INGEST FALLBACK DONE task_id=%s ok=%s", task_id, ok)
        threading.Thread(target=_auto_dual_submit_t5, args=(task_id,), daemon=True).start()
        return True


def _t5_abort_if_deleted(task_id: str, stage: str) -> bool:
    """Mirror of ingest_worker_app._abort_if_deleted for the T5 in-process path.

    Returns True (and persists abort status) if submission_context.deleted_at is set,
    so the caller short-circuits before re-populating bronze rows.
    """
    try:
        with engine.connect() as conn:
            deleted = conn.execute(sql_text(
                "SELECT 1 FROM bronze.submission_context "
                "WHERE task_id = :t AND deleted_at IS NOT NULL"
            ), {"t": task_id}).scalar() is not None
    except Exception:
        app.logger.exception("T5 INGEST deleted_at check failed task_id=%s stage=%s", task_id, stage)
        return False

    if not deleted:
        return False

    app.logger.warning("T5 INGEST aborting stage=%s task_id=%s — match soft-deleted", stage, task_id)
    try:
        with engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_error = 'aborted: match deleted by user',
                       ingest_finished_at = COALESCE(ingest_finished_at, now())
                 WHERE task_id = :t
            """), {"t": task_id})
    except Exception:
        app.logger.exception("T5 INGEST abort-status update failed task_id=%s", task_id)
    return True


def _do_ingest_t5(task_id: str) -> bool:
    """
    Lightweight ingest for T5 ML pipeline jobs.
    Steps: mark started → bronze ingest → silver build → trim → email → mark done.
    session_id is only set when silver build succeeds, so failed ingests can be
    retried by the task-status auto-ingest gate.
    """
    try:
        app.logger.info("T5 INGEST START task_id=%s", task_id)

        if _t5_abort_if_deleted(task_id, "pre_start"):
            return False

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            # Mark started but DO NOT set session_id yet — we set that only after
            # silver build succeeds, so failed ingests can be retried.
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                SET ingest_started_at = COALESCE(ingest_started_at, now()),
                    ingest_finished_at = NULL,
                    ingest_error = NULL,
                    last_status = 'completed',
                    last_status_at = now()
                WHERE task_id = :t
            """), {"t": task_id})

        if _t5_abort_if_deleted(task_id, "pre_bronze"):
            return False

        # Bronze ingest: download gzipped JSON from S3 and bulk-load into ml_analysis.*
        # Mirrors SportAI's pattern — same region as DB, fast COPY bulk insert.
        try:
            from ml_pipeline.bronze_ingest_t5 import ingest_bronze_t5
            bronze_result = ingest_bronze_t5(job_id=task_id, engine=engine, replace=True)
            app.logger.info("T5 INGEST task_id=%s bronze loaded: %s", task_id, bronze_result)
        except Exception as e:
            app.logger.warning("T5 INGEST task_id=%s bronze load failed (non-fatal): %s", task_id, e)

        # Determine sport type for routing
        with engine.connect() as conn:
            _st = conn.execute(sql_text(
                "SELECT sport_type FROM bronze.submission_context WHERE task_id = :t"
            ), {"t": task_id}).scalar() or ""

        is_practice = _st in {"serve_practice", "rally_practice"}
        is_singles_t5 = _st == "tennis_singles_t5"

        if _t5_abort_if_deleted(task_id, "pre_silver"):
            return False

        # Fix (C) from docs/_investigation/court_calibration_silent_degeneracy.md
        # — fail-loud if the Batch court calibration locked a degenerate
        # homography. Symptom: 0% of bronze rows have court_x populated (every
        # to_court_coords() projection returned None because the locked
        # homography mapped pixels outside the ±5m sanity band). The downstream
        # silver build would produce 0 rows and silently mark complete; the
        # user gets an SES email saying their match is "ready" when it's empty.
        # Match 4 (ca475740) hit this on 2026-05-28 with 0/824 bounces and
        # 0/52,433 player rows holding court coords. The failure is
        # deterministic — re-running on the same bronze.json.gz produces the
        # same degenerate output — so we terminate and set ingest_finished_at
        # =now() to keep the sweep cron from re-firing. The source-side fix
        # is the H_diag sanity gate in court_detector (A) + post-lock
        # projection self-test (B); this is the Render-side safety net.
        try:
            with engine.connect() as _cal_conn:
                cal = _cal_conn.execute(sql_text("""
                    SELECT
                      (SELECT COUNT(*) FROM ml_analysis.ball_detections
                        WHERE job_id = :t) AS ball_total,
                      (SELECT COUNT(*) FROM ml_analysis.ball_detections
                        WHERE job_id = :t AND court_x IS NOT NULL) AS ball_court,
                      (SELECT COUNT(*) FROM ml_analysis.player_detections
                        WHERE job_id = :t) AS player_total,
                      (SELECT COUNT(*) FROM ml_analysis.player_detections
                        WHERE job_id = :t AND court_x IS NOT NULL) AS player_court
                """), {"t": task_id}).mappings().first()
            ball_total = int(cal["ball_total"] or 0)
            player_total = int(cal["player_total"] or 0)
            ball_court = int(cal["ball_court"] or 0)
            player_court = int(cal["player_court"] or 0)
            # Fix (C+) 2026-05-28: generalised from "exactly 0% both sides" to
            # "below an env-tunable floor on BOTH sides". A degenerate
            # calibration collapses court coverage on both ball AND player;
            # the healthy partial tail (25-32% ball) keeps player coverage high
            # (76-92%), so requiring BOTH below the floor cleanly separates a
            # true degenerate (match 4 / f11eed2c, both ~0%) from a weak-but-
            # usable partial. This also catches the "plausible-but-wrong"
            # degenerate that projects a few points in-band but mostly out
            # (which the old exact-0% check would have missed). Thresholds are
            # env-tunable so they can be retuned without a redeploy.
            # NOTE: this is the Render backstop; the source-side fix is the
            # Fix G frame-selection + Fix B degeneracy gate in court_detector
            # (a degenerate H should no longer lock in the first place).
            import os as _os
            min_cov = float(_os.environ.get("T5_CALIB_MIN_COVERAGE", "0.05"))
            weak_cov = float(_os.environ.get("T5_CALIB_WEAK_COVERAGE", "0.20"))
            ball_cov = (ball_court / ball_total) if ball_total else 0.0
            player_cov = (player_court / player_total) if player_total else 0.0
            if (
                ball_total >= 100 and player_total >= 100
                and ball_cov < min_cov and player_cov < min_cov
            ):
                err_msg = (
                    "calibration_degenerate_low_court_coverage "
                    f"(ball {ball_court}/{ball_total}={ball_cov:.1%}, "
                    f"player {player_court}/{player_total}={player_cov:.1%}, "
                    f"floor={min_cov:.0%})"
                )
                app.logger.error(
                    "T5 INGEST task_id=%s CALIBRATION DEGENERATE — court "
                    "coverage below %.0f%% on BOTH sides; skipping silver/serve/"
                    "stroke + notify. %s",
                    task_id, 100.0 * min_cov, err_msg,
                )
                with engine.begin() as _cal_w:
                    _ensure_submission_context_schema(_cal_w)
                    _cal_w.execute(sql_text("""
                        UPDATE bronze.submission_context
                        SET ingest_error = :err,
                            last_status = 'failed_calibration',
                            last_status_at = now(),
                            ingest_finished_at = now()
                        WHERE task_id = :t
                    """), {"err": err_msg, "t": task_id})
                return False
            # Weak-but-usable: some coverage on at least one side. Silver still
            # builds; log a warning so the trend stays greppable in CloudWatch
            # and future investigations can correlate against video metadata.
            if ball_total >= 100 and ball_cov < weak_cov:
                app.logger.warning(
                    "T5 INGEST task_id=%s CALIBRATION WEAK — only %.1f%% of "
                    "ball_detections have court coords (%d/%d); silver quality "
                    "reduced.",
                    task_id, 100.0 * ball_cov, ball_court, ball_total,
                )
        except Exception as _cal_e:
            # Non-fatal: a check failure shouldn't stop the ingest. The
            # check is purely a safety net; missing it falls back to
            # current behaviour (silver might build 0 rows and silently
            # complete, which is what we had pre-fix).
            app.logger.warning(
                "T5 INGEST task_id=%s calibration check failed "
                "(non-fatal, continuing): %s", task_id, _cal_e,
            )

        # Silver: build from ml_analysis detections. Track success — we only
        # set session_id below if silver actually built, so failures retry.
        silver_built = False
        if is_practice:
            try:
                from ml_pipeline.build_silver_practice import build_silver_practice
                silver_result = build_silver_practice(task_id=task_id, replace=True, engine=engine)
                app.logger.info("T5 INGEST task_id=%s silver practice built: %s", task_id, silver_result)
                silver_built = True
            except ImportError:
                app.logger.warning("T5 INGEST task_id=%s silver builder not available (ml deps missing)", task_id)
            except Exception as e:
                app.logger.warning("T5 INGEST task_id=%s silver build failed (non-fatal): %s", task_id, e)
        elif is_singles_t5:
            # Pose-first serve detection — runs between bronze ingest and
            # silver build, consumes ml_analysis.player_detections + ball_
            # detections, persists ml_analysis.serve_events. Failure here
            # is non-fatal: the silver builder has its own legacy serve
            # logic as fallback. See ml_pipeline/serve_detector/.
            try:
                from ml_pipeline.serve_detector import detect_serves_for_task
                with engine.begin() as conn:
                    serve_events = detect_serves_for_task(conn, task_id, replace=True)
                app.logger.info(
                    "T5 INGEST task_id=%s serve detector fired %d events",
                    task_id, len(serve_events),
                )
            except ImportError:
                app.logger.warning("T5 INGEST task_id=%s serve_detector module not available", task_id)
            except Exception as e:
                app.logger.warning("T5 INGEST task_id=%s serve detection failed (non-fatal): %s", task_id, e)

            # Rule-based A/B identity (ADR-03) — runs AFTER the serve detector
            # (consumes ml_analysis.serve_events for game-boundary derivation) and
            # BEFORE the silver build (silver maps per-game side->A/B so player_id
            # is stable across changeovers, matching SA's person-based id). Failure
            # is non-fatal: silver falls back to the side-based player_id.
            try:
                from ml_pipeline.identity_detector import detect_identity_for_task
                with engine.begin() as conn:
                    identity_segments = detect_identity_for_task(conn, task_id, replace=True)
                app.logger.info(
                    "T5 INGEST task_id=%s identity detector produced %d segments",
                    task_id, len(identity_segments),
                )
            except ImportError:
                app.logger.warning("T5 INGEST task_id=%s identity_detector module not available", task_id)
            except Exception as e:
                app.logger.warning("T5 INGEST task_id=%s identity detection failed (non-fatal): %s", task_id, e)

            # Pose-first stroke detection — wrist-velocity peak detector;
            # mirrors serve_detector. Populates ml_analysis.stroke_events.
            # Failure is non-fatal; the current silver builder is bounce-
            # driven and does not yet consume stroke_events, so an empty
            # table only loses an analytics surface, not match correctness.
            try:
                from ml_pipeline.stroke_detector import detect_strokes_for_task
                with engine.begin() as conn:
                    stroke_events = detect_strokes_for_task(conn, task_id, replace=True)
                app.logger.info(
                    "T5 INGEST task_id=%s stroke detector fired %d events",
                    task_id, len(stroke_events),
                )
            except ImportError:
                app.logger.warning("T5 INGEST task_id=%s stroke_detector module not available", task_id)
            except Exception as e:
                app.logger.warning("T5 INGEST task_id=%s stroke detection failed (non-fatal): %s", task_id, e)

            # Swing type is a BRONZE fact produced BATCH-side by the v2 classifier
            # (ml_pipeline/stroke_classifier/inference_v2.classify_strokes_v2 ->
            # ml_analysis.player_detections.stroke_class), which silver projects
            # verbatim. The old Render-side detect_swing_types_for_task ->
            # ml_analysis.swing_type_events path was a no-op parallel write with no
            # consumer and was removed 2026-06-15 (cleanup sprint).

            try:
                from ml_pipeline.build_silver_match_t5 import build_silver_match_t5
                silver_result = build_silver_match_t5(task_id=task_id, replace=True, engine=engine)
                app.logger.info("T5 INGEST task_id=%s silver match built: %s", task_id, silver_result)
                silver_built = True
            except ImportError:
                app.logger.warning("T5 INGEST task_id=%s silver match builder not available (ml deps missing)", task_id)
            except Exception as e:
                app.logger.warning("T5 INGEST task_id=%s silver match build failed (non-fatal): %s", task_id, e)

        if _t5_abort_if_deleted(task_id, "pre_trim"):
            return False

        # Video trim: reuse match trim pipeline
        try:
            from video_pipeline.video_trim_api import trigger_video_trim
            trim_result = trigger_video_trim(task_id)
            app.logger.info("T5 INGEST task_id=%s trim triggered: %s", task_id, trim_result)
        except Exception as e:
            app.logger.warning("T5 INGEST task_id=%s trim failed (non-fatal): %s", task_id, e)

        # Skip: billing (T5 is free — no credit consumption for now)

        # Only mark complete (and set session_id) if silver actually built.
        # Otherwise leave session_id NULL so the next task-status poll re-fires
        # the ingest. This makes T5 ingest self-healing on transient failures.
        if silver_built:
            with engine.begin() as conn:
                _ensure_submission_context_schema(conn)
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                    SET session_id = :task_id,
                        ingest_finished_at = now(),
                        ingest_error = NULL
                    WHERE task_id = :t
                """), {"t": task_id, "task_id": task_id})

            # Customer notification (only when silver is built — otherwise email
            # tells the user their match is "ready" when it actually isn't)
            try:
                _notify_ses_completion(task_id)
            except Exception as e:
                app.logger.warning("T5 INGEST task_id=%s email notify failed (non-fatal): %s", task_id, e)

            # Phase 5c.2 — fire-and-forget pair-completion hook. No-op unless
            # AUTO_LABEL_DUAL_SUBMIT_PAIRS=1 and this is a tennis_singles_t5
            # row whose SA pair has also completed. Errors are swallowed
            # inside the helper; this thread cannot affect the T5 ingest flow.
            try:
                threading.Thread(
                    target=_dual_submit_pair_complete_hook,
                    args=(task_id,),
                    daemon=True,
                ).start()
            except Exception as e:
                app.logger.warning("T5 INGEST task_id=%s pair-label hook spawn failed (non-fatal): %s", task_id, e)

            app.logger.info("T5 INGEST COMPLETE task_id=%s", task_id)
            return True
        else:
            # Silver build failed — leave ingest_finished_at NULL so the next
            # task-status poll re-fires the ingest. Stale check kicks in after
            # INGEST_STALE_AFTER_S seconds (default 1800).
            with engine.begin() as conn:
                _ensure_submission_context_schema(conn)
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                    SET ingest_error = 'silver_build_failed',
                        ingest_finished_at = NULL
                    WHERE task_id = :t
                """), {"t": task_id})
            app.logger.warning("T5 INGEST INCOMPLETE task_id=%s — silver build failed, will retry", task_id)
            return False

    except Exception as e:
        app.logger.exception("T5 INGEST FAILED task_id=%s", task_id)
        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                SET ingest_error = :err, ingest_finished_at = now()
                WHERE task_id = :t
            """), {"t": task_id, "err": f"{e.__class__.__name__}: {e}"})
        return False


# ==========================
# SPA HTML ROUTES (served same-origin so API calls don't need CORS)
# All HTML lives in frontend/ — resolved by absolute path.
# ==========================
_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")


def _html(name: str):
    from flask import send_file
    return send_file(os.path.join(_FRONTEND_DIR, name))


@app.get("/media-room")
def media_room():
    return _html("media_room.html")


@app.get("/backoffice")
def backoffice():
    return _html("backoffice.html")


@app.get("/practice")
def practice_page():
    return _html("practice.html")


@app.get("/match-analysis")
def match_analysis_page():
    return _html("match_analysis.html")


@app.get("/portal")
def portal():
    return _html("portal.html")


@app.get("/pricing")
def pricing():
    return _html("pricing.html")


@app.get("/help")
def help_page():
    return _html("support.html")


# Public marketing pages — same-origin backups to Wix hosting
@app.get("/home")
def public_home_page():
    return _html("home.html")


@app.get("/how-it-works")
def public_how_it_works_page():
    return _html("how_it_works.html")


@app.get("/pricing-public")
def public_pricing_page():
    return _html("pricing_public.html")


@app.get("/for-coaches")
def public_for_coaches_page():
    return _html("for_coaches.html")


# ==========================
# PUBLIC ENDPOINTS (UPLOADS + STATUS + OPS)
# ==========================
@app.get("/")
def root_ok():
    return jsonify({"service": "NextPoint Upload/Ingester v3 (S3-only)", "ok": True})

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

@app.get("/ops/routes")
def ops_routes():
    if not _guard():
        return Response("Forbidden", 403)
    routes = [{"rule": r.rule, "endpoint": r.endpoint,
               "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})}
              for r in app.url_map.iter_rules()]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

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
        sc = _load_submission_context_row(tid)
        if sc.get("sport_type") in TECHNIQUE_SPORT_TYPES:
            out = _technique_cancel(str(tid))
        elif sc.get("sport_type") in T5_SPORT_TYPES:
            out = _t5_cancel(str(tid))
        else:
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

    # Resolve sport_type up-front so the entitlement gate can check the right
    # credit pool (techniques vs matches). See docs/pricing_strategy.md §5.
    _game_type = (body.get("gameType") or "singles").strip().lower()
    _SPORT_TYPE_MAP = {
        "singles": "tennis_singles",
        "singles_t5": "tennis_singles_t5",
        "serve": "serve_practice",
        "serve_practice": "serve_practice",
        "rally": "rally_practice",
        "rally_practice": "rally_practice",
        "technique": "technique_analysis",
        "technique_analysis": "technique_analysis",
    }
    _resolved_sport_type = _SPORT_TYPE_MAP.get(_game_type, "tennis_singles")

    allowed, reason = _upload_entitlement_gate(email, sport_type=_resolved_sport_type)
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
        "a_starts_near": (bool(body["a_starts_near"]) if "a_starts_near" in body and body["a_starts_near"] is not None else True),
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

    # Technique-specific metadata (sport, swing type, dominant hand, height)
    if body.get("sport"):
        meta["sport"] = body["sport"]
    if body.get("swing_type"):
        meta["swing_type"] = body["swing_type"]
    if body.get("dominant_hand"):
        meta["dominant_hand"] = body["dominant_hand"]
    if body.get("player_height_mm"):
        try:
            meta["player_height_mm"] = int(body["player_height_mm"])
        except (ValueError, TypeError):
            pass

    # ── Route to T5 or SportAI based on game type ──
    game_type = (body.get("gameType") or "singles").strip().lower()
    SPORT_TYPE_MAP = {
        "singles": "tennis_singles",
        "singles_t5": "tennis_singles_t5",
        "serve": "serve_practice",
        "serve_practice": "serve_practice",
        "rally": "rally_practice",
        "rally_practice": "rally_practice",
        "technique": "technique_analysis",
        "technique_analysis": "technique_analysis",
    }
    sport_type = SPORT_TYPE_MAP.get(game_type, "tennis_singles")
    is_t5 = sport_type in T5_SPORT_TYPES
    is_technique = sport_type in TECHNIQUE_SPORT_TYPES

    try:
        if is_technique:
            task_id = _technique_submit(s3_key, email=email, meta=meta)
        elif is_t5:
            task_id = _t5_submit(s3_key, email=email, meta=meta, sport_type=sport_type)
        else:
            task_id = _sportai_submit(s3_video_url, email=email, meta=meta)

        _store_submission_context(
            task_id=task_id,
            email=email,
            meta=meta,
            video_url=s3_video_url,
            share_url=s3_key,
            s3_bucket=S3_BUCKET,
            s3_key=s3_key,
            sport_type=sport_type,
        )

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            _set_status_cache(conn, task_id, "queued", None)

        pipeline = "technique" if is_technique else ("t5" if is_t5 else "sportai")

        # Product event (fire-and-forget; no-op unless TRACKING_ENABLED=1)
        try:
            from marketing_crm.tracking import track
            from marketing_crm.tracking.events import MATCH_UPLOADED
            track(MATCH_UPLOADED, email=email, ref_type="match", ref_id=task_id,
                  properties={"sport_type": sport_type, "pipeline": pipeline})
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "task_id": task_id,
            "pipeline": pipeline,
            "s3_verified": True,
            "s3_meta": obj_meta
        })
    except Exception as e:
        label = "Technique" if is_technique else ("T5" if is_t5 else "SportAI")
        return jsonify({"ok": False, "error": f"{label} submit failed: {e}"}), 502

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

    # Load submission context first to determine pipeline type
    sc = _load_submission_context_row(tid)
    is_t5 = sc.get("sport_type") in T5_SPORT_TYPES
    is_technique = sc.get("sport_type") in TECHNIQUE_SPORT_TYPES

    try:
        if is_technique:
            live = _technique_status(tid)
        elif is_t5:
            live = _t5_status(tid)
        else:
            live = _sportai_status(tid)

        with engine.begin() as conn:
            _ensure_submission_context_schema(conn)
            _set_status_cache(conn, tid, live.get("status"), live.get("result_url"))

    except Exception as e:
        live_error = f"{e.__class__.__name__}: {e}"

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

    dashboard_ready = bool(
        session_id
        and ingest_finished
        and not ingest_error
    )

    # Auto-fire notify once dashboard is ready (idempotent)
    if dashboard_ready:
        _notify_ses_completion(tid)

    pipeline_stage = _derive_pipeline_stage(
        sportai_status=status,
        ingest_started=ingest_started,
        ingest_finished=ingest_finished,
        ingest_error=ingest_error,
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
# DUAL-SUBMIT T5 OPS ENDPOINT
# ==========================
@app.post("/ops/dual-submit-t5")
def ops_dual_submit_t5():
    """
    Manually trigger a T5 dual-submit for an existing SportAI match.
    Useful for running T5 against historical videos without re-running SportAI.

    Body: {"sportai_task_id": "<task_id>"}
    Response: {"status": "submitted", "t5_task_id": "..."} or {"status": "skipped", "reason": "..."}
    """
    if not _guard():
        return Response("Forbidden", 403)

    body = request.get_json(silent=True) or {}
    sportai_task_id = (body.get("sportai_task_id") or "").strip()
    if not sportai_task_id:
        return jsonify({"ok": False, "error": "sportai_task_id required"}), 400

    try:
        result = _manual_dual_submit_t5(sportai_task_id)
        return jsonify({"ok": True, **result}), 200
    except Exception as e:
        app.logger.exception("OPS DUAL-SUBMIT-T5 failed sportai_task_id=%s", sportai_task_id)
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500


@app.post("/ops/dual-submit-t5-backfill")
def ops_dual_submit_t5_backfill():
    """Retro-trigger T5 dual-submit for SA tennis_singles tasks that have no
    paired T5 job. Phase 5c.1 of the dual-submit pipeline.

    Idempotent by design — each per-task call goes through `_manual_dual_submit_t5`
    which already skips matches whose s3_key already has a T5 job. The SQL
    filter just avoids paying the lookup cost.

    Body (all optional):
      {
        "dry_run": true,       # default: true — list eligible, submit nothing
        "limit": 50,           # default: 50 — cap how many to submit per call
        "delay_ms": 1000       # default: 1000 — throttle between submits
      }

    Response:
      {
        "ok": true,
        "dry_run": bool,
        "scanned": N,           # SA tennis_singles tasks examined
        "eligible": M,          # had no paired T5 job
        "submitted": K,         # T5 jobs newly queued (0 if dry_run)
        "skipped": [{task_id, reason}, ...],
        "errors": [{task_id, error}, ...],
        "next_cursor": "<created_at>"  # for paginating large backfills
      }

    Cost note: each submitted job is ~$0.12-0.15 on Spot G4dn. Start with
    dry_run=true to size the backfill before paying.
    """
    if not _guard():
        return Response("Forbidden", 403)

    import time as _time

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", True))
    limit = int(body.get("limit", 50))
    delay_ms = int(body.get("delay_ms", 1000))
    if limit < 1 or limit > 500:
        return jsonify({"ok": False, "error": "limit must be in [1, 500]"}), 400
    if delay_ms < 0 or delay_ms > 60000:
        return jsonify({"ok": False, "error": "delay_ms must be in [0, 60000]"}), 400

    try:
        with engine.connect() as conn:
            _ensure_submission_context_schema(conn)
            rows = conn.execute(sql_text("""
                SELECT sc.task_id, sc.s3_key, sc.email,
                       sc.player_a_name, sc.player_b_name,
                       sc.created_at
                  FROM bronze.submission_context sc
                 WHERE sc.sport_type = 'tennis_singles'
                   AND sc.deleted_at IS NULL
                   AND sc.s3_key IS NOT NULL
                   AND sc.s3_key <> ''
                   AND NOT EXISTS (
                       SELECT 1 FROM ml_analysis.video_analysis_jobs vj
                        WHERE vj.s3_key = sc.s3_key
                   )
                 ORDER BY sc.created_at DESC
                 LIMIT :lim
            """), {"lim": limit}).mappings().all()
    except Exception as e:
        app.logger.exception("OPS DUAL-SUBMIT-T5-BACKFILL eligibility query failed")
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

    eligible = [dict(r) for r in rows]
    result = {
        "ok": True,
        "dry_run": dry_run,
        "scanned": len(eligible),
        "eligible": len(eligible),
        "submitted": 0,
        "skipped": [],
        "errors": [],
        "next_cursor": (
            eligible[-1]["created_at"].isoformat()
            if eligible and eligible[-1].get("created_at") else None
        ),
    }

    if dry_run:
        result["sample"] = [
            {"task_id": str(r["task_id"]), "s3_key": r["s3_key"]}
            for r in eligible[:5]
        ]
        return jsonify(result), 200

    submitted = 0
    for i, r in enumerate(eligible):
        sa_tid = str(r["task_id"])
        try:
            sub = _manual_dual_submit_t5(sa_tid)
            if sub.get("status") == "submitted":
                submitted += 1
                app.logger.info(
                    "DUAL-SUBMIT-BACKFILL [%d/%d] sa=%s -> t5=%s",
                    i + 1, len(eligible), sa_tid, sub.get("t5_task_id"),
                )
            else:
                result["skipped"].append({
                    "task_id": sa_tid, "reason": sub.get("reason", "unknown"),
                })
        except Exception as e:
            app.logger.exception(
                "DUAL-SUBMIT-BACKFILL failed sa_task_id=%s", sa_tid,
            )
            result["errors"].append({
                "task_id": sa_tid, "error": f"{e.__class__.__name__}: {e}",
            })
        if delay_ms > 0 and i < len(eligible) - 1:
            _time.sleep(delay_ms / 1000.0)

    result["submitted"] = submitted
    return jsonify(result), 200


@app.post("/ops/backfill-pair-labels")
def ops_backfill_pair_labels():
    """Retro-export all corpus label kinds for completed (SA, T5) pairs that
    are missing at least one. Phase 5c.2 backfill — sibling to
    `/ops/dual-submit-t5-backfill` (which queues the T5 jobs).

    Eligibility: `gold.vw_dual_submit_pairs` rows where pair_complete=TRUE
    AND there is no training_corpus row for at least one of the known kinds
    ('ball_position', 'stroke_classifier'). Each pair is processed by
    `_label_pair_now`, which exports ALL kinds idempotently — already-present
    kinds skip silently, missing kinds are exported.

    Idempotent — safe to re-run. Adding a new label_kind requires updating
    the KNOWN_KINDS list in the eligibility CTE below.

    Body (all optional):
      {
        "dry_run": true,       # default: true — list eligible, label nothing
        "limit": 50,           # default: 50 — cap how many pairs per call
        "delay_ms": 100        # default: 100 — throttle between exports
      }
    """
    if not _guard():
        return Response("Forbidden", 403)

    import time as _time

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", True))
    limit = int(body.get("limit", 50))
    delay_ms = int(body.get("delay_ms", 100))
    if limit < 1 or limit > 500:
        return jsonify({"ok": False, "error": "limit must be in [1, 500]"}), 400
    if delay_ms < 0 or delay_ms > 60000:
        return jsonify({"ok": False, "error": "delay_ms must be in [0, 60000]"}), 400

    try:
        # Eligibility: pair_complete AND missing at least one known label_kind.
        # _label_pair_now is idempotent per-kind, so re-firing already-partial
        # pairs is safe — only the missing kinds get exported.
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                WITH known_kinds(label_kind) AS (
                    VALUES ('ball_position'), ('stroke_classifier'), ('serve')
                ),
                pair_kind_have AS (
                    SELECT p.sa_task_id, p.t5_task_id,
                           COUNT(tc.id) AS have_count
                      FROM gold.vw_dual_submit_pairs p
                      LEFT JOIN ml_analysis.training_corpus tc
                        ON tc.sa_task_id = p.sa_task_id
                       AND tc.t5_task_id = p.t5_task_id
                       AND tc.label_kind IN (SELECT label_kind FROM known_kinds)
                     WHERE p.pair_complete = TRUE
                     GROUP BY p.sa_task_id, p.t5_task_id
                )
                SELECT p.sa_task_id, p.t5_task_id, p.s3_key, p.paired_at
                  FROM gold.vw_dual_submit_pairs p
                  JOIN pair_kind_have h
                    ON h.sa_task_id = p.sa_task_id
                   AND h.t5_task_id = p.t5_task_id
                 WHERE p.pair_complete = TRUE
                   AND h.have_count < (SELECT COUNT(*) FROM known_kinds)
                 ORDER BY p.paired_at ASC
                 LIMIT :lim
            """), {"lim": limit}).mappings().all()
    except Exception as e:
        app.logger.exception("OPS BACKFILL-PAIR-LABELS eligibility query failed")
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

    eligible = [dict(r) for r in rows]
    result = {
        "ok": True,
        "dry_run": dry_run,
        "eligible": len(eligible),
        "labeled": 0,
        "skipped": [],
        "errors": [],
    }

    if dry_run:
        result["sample"] = [
            {"sa_task_id": str(r["sa_task_id"]), "t5_task_id": str(r["t5_task_id"])}
            for r in eligible[:5]
        ]
        return jsonify(result), 200

    labeled = 0
    for i, r in enumerate(eligible):
        t5_tid = str(r["t5_task_id"])
        sub = _label_pair_now(t5_tid)
        if sub.get("status") == "labeled":
            labeled += 1
            kinds = sub.get("kinds") or {}
            per_kind = ", ".join(
                f"{k}={v.get('label_count', '?')}"
                for k, v in kinds.items() if v.get("status") == "labeled"
            ) or f"{sub.get('label_count')}"
            app.logger.info(
                "BACKFILL-PAIR-LABELS [%d/%d] sa=%s t5=%s -> %s",
                i + 1, len(eligible), sub["sa_task_id"], t5_tid, per_kind,
            )
        elif sub.get("status") == "skipped":
            result["skipped"].append({
                "t5_task_id": t5_tid, "reason": sub.get("reason", "unknown"),
            })
        else:
            result["errors"].append({
                "t5_task_id": t5_tid, "error": sub.get("reason", "unknown"),
            })
        if delay_ms > 0 and i < len(eligible) - 1:
            _time.sleep(delay_ms / 1000.0)

    result["labeled"] = labeled
    return jsonify(result), 200


@app.post("/ops/sweep-t5-orphans")
def ops_sweep_t5_orphans():
    """Catch up tennis_singles_t5 tasks whose Batch run completed but whose
    Render-side ingest never fired OR started and then died mid-flight.

    Two gaps, same symptom (Batch done, silver never built):
      (a) ORPHAN — `_auto_dual_submit_t5` submits a Batch job + creates a
          `bronze.submission_context` row, but the ingest gate inside
          `/upload/api/task-status` only opens when a browser polls. Auto-
          spawned T5 tasks have no polling browser, so they sit in
          `last_status='queued'` despite Batch having succeeded.
      (b) STUCK — the ingest started but the process died before any terminal
          write (Render redeploy mid-ingest, OOM, worker timeout), leaving
          `ingest_started_at` set + `ingest_finished_at` NULL. The original
          query only matched `ingest_started_at IS NULL`, so a single mid-
          flight death stuck the task forever (this is what blocked dual-
          submit corpus #2). Now we also re-fire tasks stale past
          INGEST_STALE_AFTER_S.

    Fires `_start_ingest_background` for each. Idempotency is delegated to the
    inner ingest path (checks `ingest_started_at` + staleness — re-fires only
    if stale, so a live ingest is never double-run) and to the pair-completion
    hook (UNIQUE constraint on training_corpus).

    Body (all optional):
      {
        "dry_run": true,             # default true; list orphans, fire nothing
        "limit": 50,                 # default 50; max orphans per call (1..500)
        "min_age_minutes": 5         # default 5; only sweep tasks whose
                                     # video_analysis_jobs.updated_at is at
                                     # least N minutes old (avoids racing the
                                     # normal browser-poll path)
      }

    Response (real run):
      {ok, dry_run, found, triggered: [{task_id}, ...]}

    Header-only auth (OPS_KEY). Safe to re-run.
    """
    if not _guard():
        return Response("Forbidden", 403)

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", True))
    limit = int(body.get("limit", 50))
    min_age_minutes = int(body.get("min_age_minutes", 5))
    if limit < 1 or limit > 500:
        return jsonify({"ok": False, "error": "limit must be in [1, 500]"}), 400
    if min_age_minutes < 0 or min_age_minutes > 1440:
        return jsonify({"ok": False, "error": "min_age_minutes must be in [0, 1440]"}), 400

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT sc.task_id::text          AS task_id,
                       sc.s3_key,
                       sc.ingest_started_at      AS ingest_started_at,
                       vaj.updated_at            AS batch_complete_at
                  FROM bronze.submission_context sc
                  JOIN ml_analysis.video_analysis_jobs vaj
                    ON vaj.task_id = sc.task_id::text
                 WHERE sc.sport_type = 'tennis_singles_t5'
                   AND sc.deleted_at IS NULL
                   AND sc.ingest_finished_at IS NULL
                   AND vaj.status = 'complete'
                   AND (
                         -- (a) ORPHAN: Batch done a while ago, ingest never fired
                         --     (auto-spawned task, no polling browser to open the gate).
                         (sc.ingest_started_at IS NULL
                          AND vaj.updated_at < NOW() - (:age || ' minutes')::interval)
                         -- (b) STUCK: ingest started but died mid-flight (Render
                         --     redeploy / OOM / worker timeout) leaving no terminal
                         --     state. Without this branch a single mid-flight death
                         --     stuck the task forever, since started_at was set. The
                         --     inner gate (_start_ingest_background + _is_stale_ingest_row)
                         --     re-checks staleness before re-firing, so a live ingest
                         --     is never double-run.
                         OR (sc.ingest_started_at IS NOT NULL
                             AND sc.ingest_started_at < NOW() - (:stale_s || ' seconds')::interval)
                       )
                 ORDER BY vaj.updated_at ASC
                 LIMIT :lim
            """), {"age": str(min_age_minutes),
                   "stale_s": str(INGEST_STALE_AFTER_S),
                   "lim": limit}).mappings().all()
    except Exception as e:
        app.logger.exception("OPS SWEEP-T5-ORPHANS query failed")
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

    orphans = [dict(r) for r in rows]
    result = {
        "ok": True,
        "dry_run": dry_run,
        "found": len(orphans),
        "triggered": [],
    }

    if dry_run:
        result["sample"] = [
            {"task_id": o["task_id"],
             "kind": "orphan" if o["ingest_started_at"] is None else "stuck_stale",
             "ingest_started_at": str(o["ingest_started_at"]),
             "batch_complete_at": str(o["batch_complete_at"])}
            for o in orphans[:10]
        ]
        return jsonify(result), 200

    def _worker(items: list) -> None:
        for it in items:
            tid = it["task_id"]
            try:
                ok = _start_ingest_background(tid, f"t5://complete/{tid}")
                app.logger.info("SWEEP-T5-ORPHANS task_id=%s started=%s", tid, ok)
            except Exception as exc:
                app.logger.exception(
                    "SWEEP-T5-ORPHANS task_id=%s error: %s", tid, exc,
                )

    threading.Thread(target=_worker, args=(orphans,), daemon=True).start()
    result["triggered"] = [{"task_id": o["task_id"]} for o in orphans]
    return jsonify(result), 200


@app.post("/ops/sweep-sa-orphans")
def ops_sweep_sa_orphans():
    """Catch up SportAI (tennis_singles) tasks that finished on SportAI's side
    but whose Render ingest never fired — the SA-side twin of
    /ops/sweep-t5-orphans (rule #10).

    The SA completion→ingest gate lives in /upload/api/task-status, which only
    runs when a BROWSER polls it. On an unattended dual-submit re-run (upload a
    batch, close the tab), SportAI completes in minutes but the SA task sits in
    `last_status='processing'` forever — so the T5 twin never auto-spawns and no
    corpus row lands. This sweep is the server-side poller that closes that gap.

    Unlike the T5 sweep, "SportAI is done" is NOT in our DB — so the worker calls
    `_sportai_status(task_id)` per candidate and fires `_start_ingest_background`
    only for tasks SportAI has actually finished (result_url present). The SA
    ingest then fires `_auto_dual_submit_t5` exactly as the browser path does.

    Idempotent: the inner ingest gate checks `ingest_started_at` + staleness, so
    a live/finished ingest is never double-run; the dual-submit + corpus hooks
    are UNIQUE-guarded downstream.

    Body (all optional): {"dry_run": true, "limit": 50, "min_age_minutes": 5}.
    `min_age_minutes` avoids racing the normal browser-poll path on fresh uploads.
    Header-only auth (OPS_KEY). Safe to re-run (paired with the T5 sweep in
    cron_sweep_t5_orphans.py — one Render cron does both)."""
    if not _guard():
        return Response("Forbidden", 403)

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", True))
    limit = int(body.get("limit", 50))
    min_age_minutes = int(body.get("min_age_minutes", 5))
    if limit < 1 or limit > 500:
        return jsonify({"ok": False, "error": "limit must be in [1, 500]"}), 400
    if min_age_minutes < 0 or min_age_minutes > 1440:
        return jsonify({"ok": False, "error": "min_age_minutes must be in [0, 1440]"}), 400

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT sc.task_id::text AS task_id,
                       sc.last_status,
                       sc.last_status_at
                  FROM bronze.submission_context sc
                 WHERE sc.sport_type = 'tennis_singles'
                   AND sc.deleted_at IS NULL
                   AND sc.ingest_started_at IS NULL
                   AND sc.ingest_finished_at IS NULL
                   AND sc.last_status_at < NOW() - (:age || ' minutes')::interval
                   -- skip terminal FAILURES (SportAI will never produce a result);
                   -- keep 'completed' — a completed-but-not-ingested task is the
                   -- prime stuck case this sweep exists to catch.
                   AND lower(coalesce(sc.last_status, '')) NOT IN
                       ('canceled', 'cancelled', 'failed', 'error')
                 ORDER BY sc.last_status_at ASC
                 LIMIT :lim
            """), {"age": str(min_age_minutes), "lim": limit}).mappings().all()
    except Exception as e:
        app.logger.exception("OPS SWEEP-SA-ORPHANS query failed")
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

    candidates = [dict(r) for r in rows]
    result = {"ok": True, "dry_run": dry_run, "found": len(candidates), "triggered": []}

    if dry_run:
        result["sample"] = [
            {"task_id": o["task_id"], "last_status": o["last_status"],
             "last_status_at": str(o["last_status_at"])}
            for o in candidates[:10]
        ]
        return jsonify(result), 200

    def _worker(items: list) -> None:
        for it in items:
            tid = it["task_id"]
            try:
                live = _sportai_status(tid)
                result_url = (live or {}).get("result_url")
                if result_url:
                    ok = _start_ingest_background(tid, result_url)
                    app.logger.info(
                        "SWEEP-SA-ORPHANS task_id=%s sportai_done=1 started=%s", tid, ok)
                else:
                    app.logger.info(
                        "SWEEP-SA-ORPHANS task_id=%s sportai_not_ready status=%s",
                        tid, (live or {}).get("status"))
            except Exception as exc:
                app.logger.exception("SWEEP-SA-ORPHANS task_id=%s error: %s", tid, exc)

    threading.Thread(target=_worker, args=(candidates,), daemon=True).start()
    # "triggered" here = candidates queued for a SportAI check + ingest-if-done
    # (the worker skips any SportAI hasn't finished). See logs for per-task result.
    result["triggered"] = [{"task_id": o["task_id"]} for o in candidates]
    return jsonify(result), 200


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
# OPS — STORAGE COMPACTION
# ==========================
# Runs VACUUM (FULL, ANALYZE) on the bronze/silver/ml_analysis tables that grow
# with match volume. Reports per-table bytes-freed JSON. Each VACUUM takes
# ACCESS EXCLUSIVE on its table for the duration — trigger during low traffic.
#
# Operationally, autovacuum already reclaims dead-row space for reuse, so
# tables don't grow unbounded between calls. This endpoint exists for the
# occasional case where you want bytes returned to the OS (e.g. after a
# bulk delete like the one we just ran in the shell).
_COMPACT_TARGETS = (
    # Big JSONB / blob tables first.
    ("bronze", "raw_result"),
    ("bronze", "raw_result_chunk"),
    ("bronze", "player_swing"),
    ("bronze", "ball_position"),
    ("bronze", "ball_bounce"),
    ("bronze", "player_position"),
    ("bronze", "player"),
    ("bronze", "rally"),
    ("bronze", "session"),
    ("bronze", "session_confidences"),
    ("bronze", "thumbnail"),
    ("bronze", "highlight"),
    ("bronze", "team_session"),
    ("bronze", "bounce_heatmap"),
    ("bronze", "unmatched_field"),
    ("bronze", "debug_event"),
    ("bronze", "submission_context"),
    ("silver", "point_detail"),
    ("silver", "practice_detail"),
    ("ml_analysis", "ball_detections"),
    ("ml_analysis", "player_detections"),
    ("ml_analysis", "video_analysis_jobs"),
    ("ml_analysis", "serve_events"),
    ("ml_analysis", "match_analytics"),
)


def _table_size_bytes(conn, schema: str, table: str) -> int | None:
    # Avoid Postgres format('%I.%I', ...) here — % collides with psycopg2's
    # paramstyle. Schema/table come from the hardcoded _COMPACT_TARGETS, so
    # direct concatenation is safe (no user input).
    try:
        row = conn.execute(sql_text(
            f"SELECT pg_total_relation_size('{schema}.{table}'::regclass)"
        )).scalar()
        return int(row) if row is not None else None
    except Exception:
        return None


@app.post("/ops/compact-storage")
def ops_compact_storage():
    if not _guard():
        return Response("Forbidden", 403)

    body = request.get_json(silent=True) or {}
    only = body.get("only")  # optional list[str] of "schema.table" to scope the run
    only_set = set(only) if isinstance(only, list) else None

    results: list[dict] = []
    skipped: list[dict] = []
    total_before = 0
    total_after = 0

    # VACUUM cannot run inside a transaction — use AUTOCOMMIT.
    autocommit_engine = engine.execution_options(isolation_level="AUTOCOMMIT")

    for schema, table in _COMPACT_TARGETS:
        full = f"{schema}.{table}"
        if only_set is not None and full not in only_set:
            continue

        with autocommit_engine.connect() as conn:
            exists = conn.execute(sql_text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t"
            ), {"s": schema, "t": table}).scalar()
            if not exists:
                skipped.append({"table": full, "reason": "missing"})
                continue

            before = _table_size_bytes(conn, schema, table)
            t0 = time.time()
            try:
                conn.execute(sql_text(f'VACUUM (FULL, ANALYZE) {schema}.{table}'))
            except Exception as e:
                results.append({
                    "table": full,
                    "status": "failed",
                    "error": f"{e.__class__.__name__}: {e}"[:200],
                })
                continue
            elapsed_ms = int((time.time() - t0) * 1000)
            after = _table_size_bytes(conn, schema, table)

        if before is not None:
            total_before += before
        if after is not None:
            total_after += after

        results.append({
            "table": full,
            "status": "ok",
            "before_bytes": before,
            "after_bytes": after,
            "freed_bytes": (before - after) if (before is not None and after is not None) else None,
            "elapsed_ms": elapsed_ms,
        })

    return jsonify({
        "ok": True,
        "total_before_bytes": total_before,
        "total_after_bytes": total_after,
        "total_freed_bytes": total_before - total_after,
        "results": results,
        "skipped": skipped,
    })


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
# BOOT LOG
# ==========================
print("=== ROUTES ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    meth = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:30s} -> {r.endpoint:24s} [{meth}]")
print("================")

