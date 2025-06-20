import requests
import os

# ⬇️ Sport AI Constants
API_URL = "https://api.sportai.com/api/activity_detection"
API_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"

# ⬇️ Dropbox Constants from environment
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_FILE_PATH = "/wix-uploads/IMG_4860.MOV"


def get_dropbox_access_token():
    """Refreshes Dropbox access token."""
    res = requests.post("https://api.dropboxapi.com/oauth2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": DROPBOX_REFRESH_TOKEN,
        "client_id": DROPBOX_APP_KEY,
        "client_secret": DROPBOX_APP_SECRET
    })
    if res.status_code == 200:
        return res.json()['access_token']
    else:
        raise Exception(f"Failed to refresh Dropbox token: {res.text}")


def create_shared_link(dropbox_token, path):
    link_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={
            "Authorization": f"Bearer {dropbox_token}",
            "Content-Type": "application/json"
        },
        json={"path": path, "settings": {"requested_visibility": "public"}}
    )
    if link_res.status_code == 200:
        link_data = link_res.json()
        raw_url = link_data.get("url", "").replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")
        return raw_url
    else:
        raise Exception(f"Dropbox shared link error: {link_res.text}")


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
try:
    print("🔄 Refreshing Dropbox token...")
    dbx_token = get_dropbox_access_token()

    print("🔗 Creating Dropbox link...")
    dropbox_video_url = create_shared_link(dbx_token, DROPBOX_FILE_PATH)

    print("🚀 Sending to Sport AI...")
    result = send_video_to_sportai(dropbox_video_url)
    print("📦 Upload result:", result)
except Exception as e:
    print("❌ Error:", str(e))
