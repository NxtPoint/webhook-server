import requests
import os

SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"
task_id = "a3955762-b74f-484c-8eb6-ed8988ad15fe"

# === Step 1: Fetch result metadata ===
meta_url = f"https://api.sportai.com/api/statistics/{task_id}"
headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}

print("📡 Fetching result metadata...")
meta_response = requests.get(meta_url, headers=headers)
print("✅ Status:", meta_response.status_code)

if meta_response.status_code == 200:
    data = meta_response.json()
    print("📦 Metadata received:")
    print(data)

    result_url = data.get("data", {}).get("result_url")
    if not result_url:
        print("❌ No 'result_url' found in metadata. Aborting.")
        exit(1)

    print("📁 Remote result URL:", result_url)

    # === Step 2: Download the file ===
    download_name = os.path.basename(result_url.split('?')[0])  # remove AWS token
    print("⬇️  Downloading file as:", download_name)

    result_response = requests.get(result_url)
    if result_response.status_code == 200:
        os.makedirs("data", exist_ok=True)
        local_path = os.path.join("data", download_name)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(result_response.text)
        print("✅ Saved to local file:", local_path)
    else:
        print("❌ Failed to download result file. Status:", result_response.status_code)
else:
    print("❌ Failed to fetch result metadata.")
