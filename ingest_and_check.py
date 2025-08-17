import json
import requests
from pathlib import Path

API_BASE   = "https://api.nextpointtennis.com"
OPS_KEY    = "270fb80a747d459eafded0ae67b9b8f6"

# >>> EDIT THESE TWO LINES <<<
JSON_PATH  = r"data\f1252f36-1248-43ce-b996-b39659f407a0_statistics.json"  # your local file
SESSION_UID = "f1252f36_1248_43ce_b996_b39659f407a0"                        # keep this canonical

# If STRICT_REINGEST is on server-side, you MUST use replace=True when re-ingesting
REPLACE = True   # set to False if inserting a brand-new session

def ingest_via_file(json_path, session_uid, replace=True):
    """Preferred: send the local JSON as a file (multipart/form-data)."""
    url = f"{API_BASE}/ops/ingest-file"
    params = {
        "key": OPS_KEY,
        "session_uid": session_uid,
        "replace": "1" if replace else "0",
    }
    with open(json_path, "rb") as f:
        files = {"file": ("payload.json", f, "application/json")}
        r = requests.post(url, params=params, files=files, timeout=120)
    print("\n/ops/ingest-file →", r.status_code)
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)

def ingest_via_raw_body(json_path, session_uid, replace=True):
    """Alternative: send the JSON as raw body (application/json)."""
    url = f"{API_BASE}/ops/ingest-file"
    params = {
        "key": OPS_KEY,
        "session_uid": session_uid,
        "replace": "1" if replace else "0",
    }
    with open(json_path, "rb") as f:
        payload = json.load(f)
    r = requests.post(url, params=params, json=payload, timeout=120)
    print("\n/ops/ingest-file (raw) →", r.status_code)
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)

def reconcile(session_uid):
    """Check DB vs payload counts for this session."""
    url = f"{API_BASE}/ops/reconcile"
    params = {"key": OPS_KEY, "session_uid": session_uid}
    r = requests.get(url, params=params, timeout=60)
    print("\n/ops/reconcile →", r.status_code)
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)

def session_summary(session_uid):
    url = f"{API_BASE}/api/session/{session_uid}/summary"
    params = {"key": OPS_KEY}
    r = requests.get(url, params=params, timeout=60)
    print("\n/api/session/<uid>/summary →", r.status_code)
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)

def list_sessions():
    url = f"{API_BASE}/ops/list-sessions"
    params = {"key": OPS_KEY}
    r = requests.get(url, params=params, timeout=60)
    print("\n/ops/list-sessions →", r.status_code)
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)

if __name__ == "__main__":
    p = Path(JSON_PATH)
    if not p.exists():
        raise FileNotFoundError(f"JSON file not found at: {p.resolve()}")

    # Option A (recommended): multipart file upload
    ingest_via_file(str(p), SESSION_UID, replace=REPLACE)

    # Option B (optional): raw JSON body (works too)
    # ingest_via_raw_body(str(p), SESSION_UID, replace=REPLACE)

    # Sanity checks
    list_sessions()
    session_summary(SESSION_UID)
    reconcile(SESSION_UID)
