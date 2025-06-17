import requests
import time

API_BASE_URL = "https://api.sportai.com/api/activity_detection"
TASK_ID = "d592f60d-ae09-46ec-866d-7a8a68fd78ea"  # Replace with your actual task ID
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
        return {"error": "Failed to parse JSON", "status_code": response.status_code}

# Auto-check loop
while True:
    print("🔁 Checking task status...")
    status = check_task_status(TASK_ID)
    
    if "data" in status:
        data = status["data"]
        print(f"🟡 Status: {data['task_status']}, Progress: {data.get('task_progress', 0)}")
        
        if data["task_status"] == "completed":
            print("✅ Task completed!")
            break
    elif "error" in status:
        print("❌ Error:", status["error"])
        break
    else:
        print("⚠️ Unexpected response:", status)
        break

    time.sleep(60)  # Wait 60 seconds before checking again
