import requests

API_BASE_URL = "https://api.sportai.com/api/activity_detection"
TASK_ID = "d592f60d-ae09-46ec-866d-7a8a68fd78ea"  # Replace with your task ID
API_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"

def check_task_status(task_id):
    url = f"{API_BASE_URL}/{task_id}/status"
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers)
    
    try:
        return response.json()
    except Exception:
        return {"error": "Could not parse response", "status_code": response.status_code}

# Run it
status = check_task_status(TASK_ID)
print("🔍 Task status:", status)
