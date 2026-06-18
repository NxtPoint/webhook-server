# cron_feedback_sync.py
# ============================================================
# STANDALONE / MANUAL backfill tool — NOT a scheduled Render cron (that would cost
# extra). The recurring sync is piggybacked on the existing 5-min orphan cron, which
# now also POSTs /ops/sync-feedback-signals (see cron_sweep_t5_orphans.py). Run this
# script by hand (e.g. once, from the Render shell: `python cron_feedback_sync.py`)
# to backfill historical rows, or keep it as a no-HTTP alternative.
#
# It consolidates customer feedback signals from the canonical core.* feedback tables
# (NPS detractors, cancellation + widget surveys) into support_bot.feedback_signal,
# the single queryable feedback table mined by the admin cockpit.
#
# NOTE: going-forward these now also fire LIVE at write-time (marketing_crm/feedback
# hooks → log_feedback_signal), so this sync is only a backfill/safety-net. Bot-side
# signals (low-confidence / thumbs-down / escalation) were already logged inline by
# support_bot/db.py the moment they happen. The source_id keys ('nps:<id>' /
# 'survey:<id>') match the live hooks, so the paths are idempotent against each other.
#
# Calls support_bot.db.sync_feedback_signals() directly (no HTTP hop — the cron
# runs in the same image/DB as the app). The function is idempotent
# (INSERT...SELECT...ON CONFLICT DO NOTHING) and NEVER raises.
#
# Required env vars:
#   DATABASE_URL — read by db_init.engine (imported transitively).
# ============================================================
import sys


def main() -> int:
    try:
        from support_bot.db import init_support_schema, sync_feedback_signals
    except Exception as exc:  # import-time failure (missing deps / DATABASE_URL)
        print(f"FEEDBACK-SYNC: import failed: {exc}", file=sys.stderr)
        return 1

    # Ensure the target schema + views exist (idempotent, safe on every run).
    init_support_schema()

    counts = sync_feedback_signals()
    total = sum(counts.values()) if counts else 0
    print(f"FEEDBACK-SYNC: inserted {total} new signal(s): {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
