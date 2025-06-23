import requests

APP_KEY = "dybxtvkad43jct6"
APP_SECRET = "jev0a0q513eaidf"
AUTH_CODE = "1fWtI26YsakAAAAAAAAAirgPUFsFLtlUOc9Ob94rYVg"

response = requests.post(
    "https://api.dropboxapi.com/oauth2/token",
    data={
        "code": AUTH_CODE,
        "grant_type": "authorization_code",
        "redirect_uri": "https://localhost"
    },
    auth=(APP_KEY, APP_SECRET)
)

print("✅ Status:", response.status_code)
print("📄 Response:", response.json())
