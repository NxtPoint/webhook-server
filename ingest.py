# ingest.py
import requests
from pathlib import Path

BASE = "https://api.nextpointtennis.com"
OPS_KEY = "270fb80a747d459eafded0ae67b9b8f6"

def ingest_from_url(file_url: str, replace: bool = True):
    params = {"key": OPS_KEY, "url": file_url}
    if replace: params["replace"] = "1"
    r = requests.get(f"{BASE}/ops/ingest-file", params=params, timeout=120)
    r.raise_for_status()
    print(r.json())

def ingest_local_file(filepath: str, replace: bool = True):
    params = {"key": OPS_KEY}
    if replace: params["replace"] = "1"
    fp = Path(filepath)
    with fp.open("rb") as f:
        r = requests.post(f"{BASE}/ops/ingest-file", params=params, files={"file": f}, timeout=180)
    r.raise_for_status()
    print(r.json())

if __name__ == "__main__":
    # Example 1: GET from a Dropbox direct link
    # ingest_from_url("https://dl.dropboxusercontent.com/s/FILE.json?dl=1", replace=True)

    # Example 2: POST a local file
    # ingest_local_file(r"C:\path\to\SportAI_result_v2.json", replace=True)

    print("Edit this file and uncomment one of the examples.")
