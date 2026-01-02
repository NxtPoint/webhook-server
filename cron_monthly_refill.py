# cron_monthly_refill.py
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
