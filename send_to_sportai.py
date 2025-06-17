import requests

url = "https://api.sportai.com/api/activity_detection"

payload = {
    "video_url": "https://www.dropbox.com/scl/fi/2qzlwoq9ynxn2gexxqr1r/dejan_forehand_short.mp4?rlkey=79jvs64xz5hoffpshv3sbjej0&st=oq7wbqfm&dl=1",
    "version": "latest"
}

headers = {
    "Authorization": "Bearer qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST",
    "Content-Type": "application/json"
}

response = requests.post(url, json=payload, headers=headers)

print("Status Code:", response.status_code)
print("Response Text:", response.text)
