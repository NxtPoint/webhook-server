# cron_sweep_t5_orphans.py
# ============================================================
# Render Cron Job — runs every 5 minutes (configured in Render dashboard).
#
# Phase 5c.3 closure: auto-spawned T5 tasks have no polling browser to open
# the ingest gate in /upload/api/task-status, so they sit in
# last_status='queued' indefinitely despite Batch having succeeded.
#
# This cron fires a single authenticated POST to:
#   POST https://api.nextpointtennis.com/ops/sweep-t5-orphans
#   body: {"dry_run": false}
#
# The endpoint (upload_app.py / cleanup or upload_app inline) scans for
# tennis_singles_t5 tasks where:
#   - bronze.submission_context.ingest_started_at IS NULL
#   - ml_analysis.video_analysis_jobs.status = 'complete'
#   - age >= 5 minutes (min_age_minutes default)
# and fires _start_ingest_background for each.
#
# Idempotent (inner ingest gate checks ingest_started_at + staleness;
# ml_analysis.training_corpus has a UNIQUE constraint downstream).
#
# Required env vars:
#   OPS_KEY — used as X-Ops-Key auth header
#
# Optional env vars:
#   SWEEP_T5_ORPHANS_URL — override target URL (default: prod api.nextpointtennis.com)
#   SWEEP_T5_ORPHANS_LIMIT — override max tasks per sweep (default: server-side 50)
# ============================================================
import json
import os
import sys
import urllib.request

key = (os.environ.get("OPS_KEY") or "").strip()
if not key:
    raise RuntimeError("Missing OPS_KEY")

url = os.environ.get("SWEEP_T5_ORPHANS_URL") or "https://api.nextpointtennis.com/ops/sweep-t5-orphans"

body = {"dry_run": False}
limit = os.environ.get("SWEEP_T5_ORPHANS_LIMIT")
if limit:
    try:
        body["limit"] = int(limit)
    except ValueError:
        print(f"SWEEP-T5: ignoring invalid SWEEP_T5_ORPHANS_LIMIT={limit!r}", file=sys.stderr)

req = urllib.request.Request(
    url,
    data=json.dumps(body).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "X-Ops-Key": key,
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        print(raw)
except urllib.error.HTTPError as e:
    print(f"SWEEP-T5: HTTP {e.code} {e.reason}: {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
    sys.exit(1)
except urllib.error.URLError as e:
    print(f"SWEEP-T5: URL error: {e.reason}", file=sys.stderr)
    sys.exit(1)
