# cron_capacity_sweep.py
# ============================================================
# Render Cron Job — runs every few minutes via Render's cron service.
#
# Responsibilities:
#   1. Stuck ingest detection: identifies rows in bronze.submission_context
#      where ingest_started_at is set but ingest_completed_at is NULL
#      beyond a timeout threshold.
#   2. Stuck video trim detection: identifies rows where trim_status is
#      'accepted' but the trim has not completed within the timeout.
#
# This script reads/updates bronze.submission_context directly. It does
# not import upload_app.py or ingest_worker_app.py.
#
# Required env vars: DATABASE_URL, OPS_KEY.
# ============================================================

import os
import sys

# ============================================================
# CONFIG
# ============================================================

OPS_KEY = (os.environ.get("OPS_KEY") or "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL")

if not OPS_KEY:
    raise RuntimeError("Missing OPS_KEY")

# Thresholds (seconds)
INGEST_STALE_S = int(os.environ.get("INGEST_STALE_S", "1800"))              # 30 min
TRIM_STALE_S = int(os.environ.get("TRIM_STALE_S", "1800"))                  # 30 min


# ============================================================
# STALE STATE DETECTION (DB direct)
# ============================================================

def sweep_stale_states():
    if not DATABASE_URL:
        print("SWEEP: DATABASE_URL not set — skipping stale state sweep")
        return

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

        if not ingest_stale and not trim_stale:
            print("SWEEP: No stale states found — all clean")

    engine.dispose()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("SWEEP: Starting stale state sweep")
    sweep_stale_states()
    print("SWEEP: Done")
