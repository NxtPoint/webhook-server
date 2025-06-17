import requests

API_URL = "https://api.sportai.com/api/activity_detection"
VIDEO_URL = "https://www.dropbox.com/scl/fi/v87iqmpv0dwtiyez6e39e/IMG_4860.MOV?rlkey=zi4i1bo71mlok2rrnp4pfj3ec&st=865g0tk6&raw=1"
API_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"

def send_video_to_sportai(video_url):
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "video_url": video_url,
        "version": "latest"
    }
    response = requests.post(API_URL, headers=headers, json=payload)
    return response.json()

# Run the upload
result = send_video_to_sportai(VIDEO_URL)
print("📦 Upload result:", result)
