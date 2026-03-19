# cron_capacity_sweep.py
import os
import urllib.request

BASE_URL = os.environ.get("RENDER_POWERBI_BASE_URL")
OPS_KEY = os.environ.get("OPS_KEY")

if not BASE_URL or not OPS_KEY:
    raise RuntimeError("Missing RENDER_POWERBI_BASE_URL or OPS_KEY")

url = f"{BASE_URL.rstrip('/')}/session/sweep"

req = urllib.request.Request(
    url,
    data=b"{}",
    headers={
        "Content-Type": "application/json",
        "X-Ops-Key": OPS_KEY,
    },
    method="POST",
)

print(urllib.request.urlopen(req).read().decode("utf-8"))