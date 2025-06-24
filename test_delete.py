import requests

# Replace with your actual Bearer token
SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"

# Replace with the video URL you want to test
video_url = "https://www.dropbox.com/scl/fi/1bpsrpw2krepl0kdfq37e/dejan_forehand_mp4.mp4?rlkey=w6bcm3wsudjinaf6eday3ik2f&st=nflcgks9&raw=1"
video_url = video_url.replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

url = "https://api.sportai.com/api/videos/check"

payload = {
    "version": "stable",
    "video_urls": [video_url]
}

headers = {
    "Authorization": f"Bearer {SPORT_AI_TOKEN}",
    "Content-Type": "application/json"
}

response = requests.post(url, json=payload, headers=headers)

print("✅ Status Code:", response.status_code)
print("📦 Response JSON:")
print(response.json())
