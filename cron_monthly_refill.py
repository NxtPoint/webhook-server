# cron_monthly_refill.py
# ============================================================
# Render Cron Job — runs once per month (configured in render.yaml).
#
# Triggers the monthly billing entitlement refill by making a single
# authenticated POST request to:
#   POST https://api.nextpointtennis.com/api/billing/cron/monthly_refill
#
# The main API endpoint (billing_service.py) handles the actual logic:
# iterating active accounts with monthly plans and topping up their
# EntitlementGrant credits for the new billing period.
#
# Required env vars:
#   BILLING_OPS_KEY or OPS_KEY — used as X-Ops-Key auth header
# ============================================================
import os
import urllib.request

key = os.environ.get("BILLING_OPS_KEY") or os.environ.get("OPS_KEY")
if not key:
    raise RuntimeError("Missing BILLING_OPS_KEY or OPS_KEY")

req = urllib.request.Request(
    "https://api.nextpointtennis.com/api/billing/cron/monthly_refill",
    data=b"{}",
    headers={
        "Content-Type": "application/json",
        "X-Ops-Key": key,
    },
    method="POST",
)

print(urllib.request.urlopen(req).read().decode("utf-8"))
