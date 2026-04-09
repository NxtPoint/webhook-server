# cron_capacity_sweep.py
# ============================================================
# Render Cron Job — runs every few minutes via Render's cron service.
#
# Responsibilities:
#   1. PBI session sweep: expire stale leases in billing.pbi_sessions,
#      then suspend Azure capacity if no sessions remain active and no
#      refresh is in progress (calls POST /session/sweep on powerbi_app).
#   2. Stuck PBI refresh detection: flags refreshes that were triggered
#      but have not completed within the expected window.
#   3. Stuck ingest detection: identifies rows in bronze.submission_context
#      where ingest_started_at is set but ingest_completed_at is NULL
#      beyond a timeout threshold.
#   4. Stuck video trim detection: identifies rows where trim_status is
#      'accepted' but the trim has not completed within the timeout.
#
# This script reads/updates bronze.submission_context directly and calls
# the PowerBI service HTTP API for session sweep. It does not import
# upload_app.py or ingest_worker_app.py.
#
# Required env vars: DATABASE_URL, OPS_KEY, PBI_SERVICE_URL (powerbi_app).
# ============================================================

import json
import os
import sys
import urllib.request

# ============================================================
# CONFIG
# ============================================================

POWERBI_BASE_URL = (os.environ.get("RENDER_POWERBI_BASE_URL") or "").strip().rstrip("/")
OPS_KEY = (os.environ.get("OPS_KEY") or "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL")

if not OPS_KEY:
    raise RuntimeError("Missing OPS_KEY")

# Thresholds (seconds)
PBI_REFRESH_STALE_S = int(os.environ.get("PBI_REFRESH_STALE_S", "600"))     # 10 min
INGEST_STALE_S = int(os.environ.get("INGEST_STALE_S", "1800"))              # 30 min
TRIM_STALE_S = int(os.environ.get("TRIM_STALE_S", "1800"))                  # 30 min


# ============================================================
# 1. POWERBI SESSION SWEEP (existing behavior)
# ============================================================

def sweep_powerbi_sessions():
    if not POWERBI_BASE_URL:
        print("SWEEP: RENDER_POWERBI_BASE_URL not set — skipping PowerBI session sweep")
        return

    url = f"{POWERBI_BASE_URL}/session/sweep"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Ops-Key": OPS_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            print(f"SWEEP: PowerBI session sweep ok: {body}")
    except Exception as e:
        print(f"SWEEP: PowerBI session sweep failed: {e}", file=sys.stderr)


# ============================================================
# 2. STALE STATE DETECTION (DB direct)
# ============================================================

def sweep_stale_states():
    if not DATABASE_URL:
        print("SWEEP: DATABASE_URL not set — skipping stale state sweep")
        return

    # Late import so cron still works if psycopg not installed
    try:
        from sqlalchemy import create_engine, text as sql_text
    except ImportError:
        print("SWEEP: sqlalchemy not available — skipping stale state sweep")
        return

    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if db_url.startswith("postgresql://") and "+psycopg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    engine = create_engine(db_url, pool_pre_ping=True)

    with engine.begin() as conn:
        # --- Stuck PBI refreshes ---
        result = conn.execute(sql_text("""
            UPDATE bronze.submission_context
               SET pbi_refresh_status = 'stale_timeout',
                   pbi_refresh_error = 'Cron sweep: refresh stuck in triggered/running for too long',
                   pbi_refresh_finished_at = now()
             WHERE pbi_refresh_started_at IS NOT NULL
               AND pbi_refresh_finished_at IS NULL
               AND pbi_refresh_status IN ('triggered', 'running', 'queued')
               AND pbi_refresh_started_at < now() - make_interval(secs => :pbi_s)
            RETURNING task_id
        """), {"pbi_s": PBI_REFRESH_STALE_S})
        pbi_stale = [r[0] for r in result.fetchall()]
        if pbi_stale:
            print(f"SWEEP: Marked {len(pbi_stale)} stuck PBI refreshes as stale_timeout: {pbi_stale}")

        # --- Stuck ingests ---
        result = conn.execute(sql_text("""
            UPDATE bronze.submission_context
               SET ingest_error = 'Cron sweep: ingest stuck (started but never finished)',
                   ingest_finished_at = now()
             WHERE ingest_started_at IS NOT NULL
               AND ingest_finished_at IS NULL
               AND ingest_error IS NULL
               AND ingest_started_at < now() - make_interval(secs => :ingest_s)
            RETURNING task_id
        """), {"ingest_s": INGEST_STALE_S})
        ingest_stale = [r[0] for r in result.fetchall()]
        if ingest_stale:
            print(f"SWEEP: Marked {len(ingest_stale)} stuck ingests as failed: {ingest_stale}")

        # --- Stuck video trims ---
        result = conn.execute(sql_text("""
            UPDATE bronze.submission_context
               SET trim_status = 'failed',
                   trim_error = 'Cron sweep: trim stuck in accepted/queued for too long',
                   trim_finished_at = now()
             WHERE trim_status IN ('accepted', 'queued')
               AND trim_requested_at IS NOT NULL
               AND trim_finished_at IS NULL
               AND trim_requested_at < now() - make_interval(secs => :trim_s)
            RETURNING task_id
        """), {"trim_s": TRIM_STALE_S})
        trim_stale = [r[0] for r in result.fetchall()]
        if trim_stale:
            print(f"SWEEP: Marked {len(trim_stale)} stuck video trims as failed: {trim_stale}")

        if not pbi_stale and not ingest_stale and not trim_stale:
            print("SWEEP: No stale states found — all clean")

    engine.dispose()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("SWEEP: Starting capacity + stale state sweep")
    sweep_powerbi_sessions()
    sweep_stale_states()
    print("SWEEP: Done")
