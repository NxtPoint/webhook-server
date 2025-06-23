import requests

APP_KEY = "your_app_key_here"
APP_SECRET = "your_app_secret_here"
AUTH_CODE = "your_authorization_code_here"  # The one you got from Dropbox

response = requests.post(
    "https://api.dropboxapi.com/oauth2/token",
    data={
        "code": AUTH_CODE,
        "grant_type": "authorization_code",
        "redirect_uri": "https://localhost"  # Must match the URI you used when authorizing
    },
    auth=(APP_KEY, APP_SECRET)
)

print("Status:", response.status_code)
print("Response:", response.json())
