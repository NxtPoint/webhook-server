import requests

url = "https://api.sportai.com/api/activity_detection/38503ffd-3b71-4f2e-a6f2-676d586973b4"

def check_video_accessibility(video_url):
    url = "https://api.sportai.io/api/videos/check"

    payload = {
        "version": "stable",
        "video_urls": [video_url]
    }

    headers = {
        "Authorization": "Bearer qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST",
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=payload, headers=headers)
    result = response.json()

    print("📤 Video Check Response:", result)
    
    if response.status_code == 200 and result.get("data"):
        return True  # ✅ video is accessible and readable
    else:
        return False  # ❌ something is wrong
