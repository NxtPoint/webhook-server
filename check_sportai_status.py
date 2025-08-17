import os
import requests

SPORT_AI_TOKEN = os.getenv("qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST")

def check_status(task_id: str):
    url = f"https://api.sportai.com/api/statistics/{task_id}/status"
    r = requests.get(url, headers={"Authorization": f"Bearer {SPORT_AI_TOKEN}"}, timeout=30)
    r.raise_for_status()
    data = r.json().get("data", {})
    status  = data.get("status")       # e.g. "queued", "processing", "done"/"completed", "failed"
    progress = data.get("progress", 0) # 0..1
    return status, progress

def fetch_result_json(task_id: str) -> dict:
    """Call this after status is 'done' or 'completed'."""
    url = f"https://api.sportai.com/api/statistics/{task_id}"
    r = requests.get(url, headers={"Authorization": f"Bearer {SPORT_AI_TOKEN}"}, timeout=60)
    r.raise_for_status()
    j = r.json().get("data", {})
    return j  # contains result_url; you can GET that URL to download the JSON text

if __name__ == "__main__":
    task_id = "3ec10080-b5af-4ff0-a2b6-de33cadd767d"
    status, progress = check_status(task_id)
    print(f"Status: {status}, progress: {progress:.0%}")

    if status in ("done", "completed"):
        meta = fetch_result_json(task_id)
        print("Result meta keys:", list(meta.keys()))
        print("Result URL:", meta.get("result_url"))
