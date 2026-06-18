# cron_sweep_t5_orphans.py
# ============================================================
# Render Cron Job — runs every 5 minutes (configured in Render dashboard).
#
# ONE Render cron fires the orphan sweeps + a feedback-signal sync (kept in this
# single script on purpose — a second Render cron would cost extra; extra HTTP
# calls cost nothing):
#
#   1. POST /ops/sweep-t5-orphans  — tennis_singles_t5 tasks whose Batch run
#      completed but whose Render-side ingest never fired (auto-spawned T5 tasks
#      have no polling browser to open the ingest gate in /upload/api/task-status).
#
#   2. POST /ops/sweep-sa-orphans  — tennis_singles (SportAI) tasks that finished
#      on SportAI's side but whose ingest never fired. The SA completion→ingest
#      gate ALSO lives in the browser-polled /upload/api/task-status, so an
#      unattended dual-submit re-run (upload a batch, close the tab) leaves SA
#      tasks stuck in last_status='processing' → the T5 twin never spawns → no
#      corpus row lands. This is the SA-side twin of the T5 sweep (rule #10).
#
#   3. POST /ops/sync-feedback-signals — backfill/safety-net for the feedback-loop
#      mining table (support_bot.feedback_signal). Signals fire LIVE at write-time
#      (marketing_crm/feedback hooks); this idempotent set-based sync just catches
#      historical/missed NPS-detractor + cancellation/widget-survey rows.
#
# All endpoints are idempotent (inner ingest gate checks ingest_started_at +
# staleness; training_corpus has a UNIQUE constraint downstream; feedback sync is
# ON CONFLICT DO NOTHING).
#
# Required env vars:
#   OPS_KEY — used as X-Ops-Key auth header
#
# Optional env vars:
#   SWEEP_T5_ORPHANS_URL — override the T5 target URL (default prod)
#   SWEEP_SA_ORPHANS_URL — override the SA target URL (default prod)
#   SWEEP_ORPHANS_BASE_URL — override the base for BOTH (default prod api)
#   SWEEP_T5_ORPHANS_LIMIT — override max tasks per sweep (default: server-side 50)
# ============================================================
import json
import os
import sys
import urllib.error
import urllib.request

key = (os.environ.get("OPS_KEY") or "").strip()
if not key:
    raise RuntimeError("Missing OPS_KEY")

base = (os.environ.get("SWEEP_ORPHANS_BASE_URL") or "https://api.nextpointtennis.com").rstrip("/")
t5_url = os.environ.get("SWEEP_T5_ORPHANS_URL") or f"{base}/ops/sweep-t5-orphans"
sa_url = os.environ.get("SWEEP_SA_ORPHANS_URL") or f"{base}/ops/sweep-sa-orphans"
feedback_url = os.environ.get("SYNC_FEEDBACK_URL") or f"{base}/ops/sync-feedback-signals"

_body = {"dry_run": False}
_limit = os.environ.get("SWEEP_T5_ORPHANS_LIMIT")
if _limit:
    try:
        _body["limit"] = int(_limit)
    except ValueError:
        print(f"SWEEP: ignoring invalid SWEEP_T5_ORPHANS_LIMIT={_limit!r}", file=sys.stderr)


def _post_sweep(name: str, url: str) -> bool:
    """POST one sweep; print its response. Returns False on HTTP/URL error.
    Failures are isolated so one sweep failing never blocks the other."""
    req = urllib.request.Request(
        url,
        data=json.dumps(_body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Ops-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"{name}: {resp.read().decode('utf-8')}")
            return True
    except urllib.error.HTTPError as e:
        print(f"{name}: HTTP {e.code} {e.reason}: "
              f"{e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"{name}: URL error: {e.reason}", file=sys.stderr)
    return False


ok_t5 = _post_sweep("SWEEP-T5", t5_url)
ok_sa = _post_sweep("SWEEP-SA", sa_url)
# 3rd call (zero extra cron cost): backfill/safety-net for the feedback-loop signal table.
# Feedback signals fire LIVE at write-time; this just catches historical/missed rows (idempotent).
ok_fb = _post_sweep("SYNC-FEEDBACK", feedback_url)

# Non-zero exit only if ALL failed (so a transient single-endpoint blip doesn't
# red the cron when the others ran fine).
sys.exit(0 if (ok_t5 or ok_sa or ok_fb) else 1)
