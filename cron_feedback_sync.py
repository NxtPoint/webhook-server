# cron_feedback_sync.py
# ============================================================
# Render Cron Job — periodically consolidates customer feedback signals from the
# canonical core.* feedback tables (NPS detractors, cancellation + widget
# surveys) into support_bot.feedback_signal, the single queryable feedback table
# mined by the admin cockpit.
#
# Bot-side signals (low-confidence / thumbs-down / escalation) are logged inline
# by support_bot/db.py the moment they happen, so this cron only covers the
# core.* poll-based sources.
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
