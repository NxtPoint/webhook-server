import requests

# Replace with your real values
ACCESS_CODE = "1fWtI26YsakAAAAAAAAAebxwA_-nfZ0nsOpCkguLGC4"
APP_KEY = "dybxtvkad43jct6"
APP_SECRET = "jev0a0q513eaidf"
REDIRECT_URI = "https://localhost"


print("🚀 Starting token exchange...")

try:
    response = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "code": ACCESS_CODE,
            "grant_type": "authorization_code",
            "client_id": APP_KEY,
            "client_secret": APP_SECRET,
            "redirect_uri": REDIRECT_URI
        }
    )

    print("✅ Response status code:", response.status_code)

    try:
        print("📦 JSON response:")
        print(response.json())
    except Exception as e:
        print("❌ JSON parse error:", e)
        print("📝 Raw response text:")
        print(response.text)

except Exception as e:
    print("❌ Request failed completely:", e)
