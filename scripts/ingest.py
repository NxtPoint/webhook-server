#!/usr/bin/env python3
import argparse, os, sys, requests

def main():
    p = argparse.ArgumentParser(description="Upload a SportAI session JSON to NextPoint /ops/ingest-file")
    p.add_argument("--api-base", default=os.environ.get("API_BASE", "https://api.nextpointtennis.com"),
                   help="API base URL (default: https://api.nextpointtennis.com)")
    p.add_argument("--key", default=os.environ.get("OPS_KEY"),
                   help="OPS key (or set OPS_KEY env var)")
    p.add_argument("--session-uid", required=True, help="Session UID to store under")
    p.add_argument("--replace", action="store_true", help="Replace existing session rows")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help="Path to local JSON file")
    g.add_argument("--url", help="Public URL to the JSON (server will fetch it)")
    args = p.parse_args()

    if not args.key:
        sys.exit("Missing --key (or set OPS_KEY env var)")

    endpoint = f"{args.api_base.rstrip('/')}/ops/ingest-file"
    params = {"key": args.key, "session_uid": args.session_uid}
    if args.replace:
        params["replace"] = "1"

    if args.url:
        # Let the server fetch the JSON
        params["url"] = args.url
        r = requests.post(endpoint, params=params, timeout=180)
    else:
        # Upload local file as multipart
        with open(args.file, "rb") as f:
            files = {"file": ("statistics.json", f, "application/json")}
            r = requests.post(endpoint, params=params, files=files, timeout=300)

    print("Status:", r.status_code)
    try:
        print(r.json())
    except Exception:
        print(r.text)

if __name__ == "__main__":
    main()
