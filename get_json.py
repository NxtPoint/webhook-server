import requests

API_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"
TASK_ID = "5d213632-f472-4663-a20b-b25eb2296c77"

url = f"https://api.sportai.com/api/statistics/{TASK_ID}"

headers = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}

response = requests.get(url, headers=headers)

try:
    data = response.json()
    print("✅ Status:", response.status_code)
    print("📄 Full Results:")
    print(data)
    
    # Optionally save to a file
    with open(f"result_{TASK_ID}.json", "w") as f:
        import json
        json.dump(data, f, indent=2)
        print(f"💾 Result saved to result_{TASK_ID}.json")

except Exception as e:
    print("❌ Error reading result:", str(e))
    print(response.text)
