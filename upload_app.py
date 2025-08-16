from flask import Flask, request, jsonify, render_template, send_file
import requests
import os
import json
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from threading import Thread
import time
from json_to_powerbi_csv import export_csv_from_json

# DB imports
from sqlalchemy import create_engine, text
from db_init import init_db  # Step 1 schema creator

app = Flask(__name__)

# =======================
# Environment variables
# =======================
SPORT_AI_TOKEN = os.environ.get("SPORT_AI_TOKEN")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")
DATABASE_URL = os.environ.get("DATABASE_URL")
OPS_KEY = os.environ.get("OPS_KEY")

# Create a shared SQLAlchemy engine
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_size=int(os.getenv("POOL_SIZE", "5")),
            max_overflow=0,
        )
        print("✅ SQLAlchemy engine created")
    except Exception as e:
        print("❌ Failed to create SQLAlchemy engine:", str(e))
else:
    print("⚠️ DATABASE_URL is not set. DB features will be disabled.")

# ==========================================
# Dropbox OAuth
# ==========================================
def get_dropbox_access_token():
    res = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
            "client_id": DROPBOX_APP_KEY,
            "client_secret": DROPBOX_APP_SECRET
        }
    )
    if res.status_code in [200, 201, 202]:
        return res.json()['access_token']
    print("❌ Dropbox token refresh failed:", res.text)
    return None

def check_video_accessibility(video_url):
    res = requests.post(
        "https://api.sportai.com/api/videos/check",
        json={"version": "stable", "video_urls": [video_url]},
        headers={"Authorization": f"Bearer {SPORT_AI_TOKEN}", "Content-Type": "application/json"}
    )
    if res.status_code not in [200, 201, 202]:
        return False, "Video is not accessible"
    try:
        resp_json = res.json()
        inner = resp_json["data"][video_url]
        if not inner.get("video_ok", False):
            return False, "Video quality too low"
        return True, None
    except Exception as e:
        return False, f"Video check failed: {str(e)}"

# ==========================================
# Basic routes
# ==========================================
@app.route('/')
def index():
    return render_template("upload.html")

@app.route('/upload', methods=['POST'])
def upload():
    if 'video' not in request.files or 'email' not in request.form:
        return jsonify({"error": "Video and email are required"}), 400

    email = request.form['email'].strip().replace("@", "_at_").replace(".", "_")
    video = request.files['video']
    file_name = video.filename
    file_bytes = video.read()
    dropbox_path = f"/wix-uploads/{file_name}"

    token = get_dropbox_access_token()
    if not token:
        return jsonify({"error": "Dropbox token refresh failed"}), 500

    upload_res = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({
                "path": dropbox_path,
                "mode": "add",
                "autorename": True,
                "mute": False
            }),
            "Content-Type": "application/octet-stream"
        },
        data=file_bytes
    )

    if not upload_res.ok:
        return jsonify({"error": "Dropbox upload failed", "details": upload_res.text}), 500

    link_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"path": dropbox_path, "settings": {"requested_visibility": "public"}}
    )

    if link_res.status_code not in [200, 201, 202]:
        err = link_res.json()
        if err.get('error', {}).get('.tag') == 'shared_link_already_exists':
            link_data = requests.post(
                "https://api.dropboxapi.com/2/sharing/list_shared_links",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"path": dropbox_path, "direct_only": True}
            ).json()
            raw_url = link_data['links'][0]['url']
        else:
            return jsonify({"error": "Failed to generate Dropbox link"}), 500
    else:
        raw_url = link_res.json()['url']

    raw_url = raw_url.replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

    # Register task with SportAI
    analyze_payload = {
        "video_url": raw_url,
        "only_in_rally_data": False,
        "version": "stable"
    }
    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}",
        "Content-Type": "application/json"
    }
    analyze_res = requests.post("https://api.sportai.com/api/statistics", json=analyze_payload, headers=headers)

    if analyze_res.status_code not in [200, 201, 202]:
        return jsonify({"error": "Failed to register task", "details": analyze_res.text}), 500

    task_id = analyze_res.json()["data"]["task_id"]

    # Start polling thread immediately
    thread = Thread(target=poll_and_save_result, args=(task_id, email))
    thread.start()

    return jsonify({
        "message": "Upload and analysis started",
        "dropbox_url": raw_url,
        "sportai_task_id": task_id
    }), 200

# ==========================================
# Polling + JSON download
# ==========================================
def poll_and_save_result(task_id, email):
    print(f"⏳ Polling started for task {task_id}...")

    max_attempts = 720  # up to 6 hours
    delay = 30
    url = f"https://api.sportai.com/api/statistics/{task_id}/status"
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}

    for attempt in range(max_attempts):
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            status = res.json()["data"].get("status")
            print(f"🔄 Attempt {attempt+1}: Status = {status}")
            if status == "completed":
                max_retries = 15
                retry_delay = 10
                for retry in range(max_retries):
                    filename = fetch_and_save_result(task_id, email)
                    if filename:
                        print(f"✅ Result saved to {filename}")
                        return
                    else:
                        print(f"⏳ Retry {retry+1}/{max_retries}: Waiting for result_url to activate...")
                        time.sleep(retry_delay)
                print("❌ JSON download failed after completion status.")
                return
        else:
            print("⚠️ Failed to check status:", res.text)
        time.sleep(delay)

    print("❌ Polling timed out after 6 hours.")

def fetch_and_save_result(task_id, email):
    try:
        meta_url = f"https://api.sportai.com/api/statistics/{task_id}"
        headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
        meta_res = requests.get(meta_url, headers=headers)

        if meta_res.status_code in [200, 201]:
            meta_json = meta_res.json()
            result_url = meta_json["data"].get("result_url")
            if not result_url:
                print("📭 Waiting for result_url to become active. Metadata:", meta_json)
                print("❌ No result_url found in metadata.")
                return None

            print(f"📡 Downloading JSON from: {result_url}")
            result_res = requests.get(result_url)
            if result_res.status_code == 200:
                os.makedirs("data", exist_ok=True)
                timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                local_filename = f"data/sportai-{task_id}-{email}-{timestamp}.json"
                with open(local_filename, "w", encoding="utf-8") as f:
                    f.write(result_res.text)

                # Export CSV for Power BI
                try:
                    export_csv_from_json(local_filename)
                    print(f"📊 CSVs exported for {local_filename}")
                except Exception as e:
                    print(f"⚠️ Failed to export CSV for {local_filename}: {str(e)}")

                # NEW: Ingest JSON into Postgres
                try:
                    if engine is None:
                        print("⚠️ No DB engine available, skipping DB ingestion.")
                    else:
                        ingest_sportai_json(local_filename, os.path.basename(local_filename))
                        print("✅ Ingestion into Postgres completed.")
                except Exception as e:
                    print("❌ Ingestion error:", str(e))

                return local_filename
            else:
                print(f"❌ Could not download JSON. Status: {result_res.status_code}")
        else:
            print(f"❌ Metadata fetch failed. Status: {meta_res.status_code}")
    except Exception as e:
        print("❌ Exception in fetch_and_save_result:", str(e))
    return None

# ==========================================
# Ops endpoints (init, ping, counts, manual ingest)
# ==========================================
@app.get("/ops/init-db")
def ops_init_db():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    try:
        result = init_db()
        return jsonify({"status": result})
    except Exception as e:
        return (str(e), 500)

@app.get("/ops/db-ping")
def db_ping():
    if engine is None:
        return {"ok": False, "error": "No engine (DATABASE_URL not set)."}, 500
    try:
        with engine.connect() as conn:
            now = conn.execute(text("SELECT NOW()")).scalar()
        return {"ok": True, "now": str(now)}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# --- TEMP: quick table counts
@app.get("/ops/db-counts")
def db_counts():
    if engine is None:
        return {"ok": False, "error": "No engine (DATABASE_URL not set)."}, 500
    try:
        counts = {}
        with engine.connect() as conn:
            for t in ["dim_session", "dim_player", "dim_rally", "fact_swing", "fact_bounce"]:
                counts[t] = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        return {"ok": True, "counts": counts}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# --- TEMP: ingest a local JSON file in /data (guarded)
@app.get("/ops/ingest-file")
def ops_ingest_file():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)

    json_name = request.args.get("name")
    if not json_name:
        return ("Missing ?name=<filename.json>", 400)

    data_dir = os.path.join(os.getcwd(), "data")
    json_path = os.path.join(data_dir, json_name)

    if not os.path.isfile(json_path):
        return (f"File not found: {json_path}", 404)

    try:
        ingest_sportai_json(json_path, json_name)
        return {"ok": True, "ingested": json_name}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# ==========================================
# Ingestion helpers & glue
# ==========================================
def _parse_ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def upsert_dim_session(*, session_uid, source_file=None, session_date=None, court_surface=None, venue=None):
    sql = text("""
        INSERT INTO dim_session (session_uid, source_file, session_date, court_surface, venue)
        VALUES (:session_uid, :source_file, :session_date, :court_surface, :venue)
        ON CONFLICT (session_uid) DO UPDATE SET
            source_file = EXCLUDED.source_file,
            session_date = EXCLUDED.session_date,
            court_surface = EXCLUDED.court_surface,
            venue = EXCLUDED.venue
        RETURNING session_id;
    """)
    with engine.begin() as conn:
        sid = conn.execute(sql, dict(
            session_uid=session_uid, source_file=source_file, session_date=session_date,
            court_surface=court_surface, venue=venue
        )).scalar_one()
    return sid

def upsert_dim_player(*, sportai_player_uid, full_name=None, handedness=None, age=None, utr=None):
    sql = text("""
        INSERT INTO dim_player (sportai_player_uid, full_name, handedness, age, utr)
        VALUES (:uid, :name, :handedness, :age, :utr)
        ON CONFLICT (sportai_player_uid) DO UPDATE SET
            full_name  = COALESCE(EXCLUDED.full_name, dim_player.full_name),
            handedness = COALESCE(EXCLUDED.handedness, dim_player.handedness),
            age        = COALESCE(EXCLUDED.age, dim_player.age),
            utr        = COALESCE(EXCLUDED.utr, dim_player.utr)
        RETURNING player_id;
    """)
    with engine.begin() as conn:
        pid = conn.execute(sql, dict(
            uid=sportai_player_uid, name=full_name, handedness=handedness, age=age, utr=utr
        )).scalar_one()
    return pid

def upsert_dim_rally(*, session_id, rally_number, start_ts=None, end_ts=None, point_winner_player_id=None, length_shots=None):
    sql = text("""
        INSERT INTO dim_rally (session_id, rally_number, start_ts, end_ts, point_winner_player_id, length_shots)
        VALUES (:session_id, :rally_number, :start_ts, :end_ts, :winner, :len)
        ON CONFLICT (session_id, rally_number) DO UPDATE SET
            start_ts = EXCLUDED.start_ts,
            end_ts = EXCLUDED.end_ts,
            point_winner_player_id = EXCLUDED.point_winner_player_id,
            length_shots = EXCLUDED.length_shots
        RETURNING rally_id;
    """)
    with engine.begin() as conn:
        rid = conn.execute(sql, dict(
            session_id=session_id, rally_number=rally_number, start_ts=start_ts,
            end_ts=end_ts, winner=point_winner_player_id, len=length_shots
        )).scalar_one()
    return rid

def insert_fact_swing_batch(rows):
    if not rows:
        return 0
    cols = [
        "session_id","rally_id","player_id","swing_start_ts","swing_end_ts","ball_hit_ts",
        "swing_type","is_serve","is_return","is_in_rally","valid",
        "serve_number","serve_location","return_depth_box",
        "ball_x","ball_y","ball_speed","ball_player_dist","annotations_json"
    ]
    placeholders = ",".join([f":{c}" for c in cols])
    sql = text(f"""
        INSERT INTO fact_swing ({",".join(cols)})
        VALUES ({placeholders})
    """)
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def insert_fact_bounce_batch(rows):
    if not rows:
        return 0
    cols = ["session_id","rally_id","bounce_ts","bounce_x","bounce_y"]
    placeholders = ",".join([f":{c}" for c in cols])
    sql = text(f"INSERT INTO fact_bounce ({','.join(cols)}) VALUES ({placeholders})")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def ingest_sportai_json(json_path, source_file_name):
    """
    Reads a SportAI result JSON from disk and loads:
      - session -> dim_session
      - players -> dim_player
      - rallies -> dim_rally
      - swings  -> fact_swing
      - ball_bounces -> fact_bounce
    Adjust key names if your JSON differs.
    """
    if engine is None:
        print("⚠️ No DB engine. Skipping ingest.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # --- Session
    session_uid = (data.get("session", {}) or {}).get("id") or data.get("id") or source_file_name
    session_date = None
    ts = (data.get("session", {}) or {}).get("start_time") or data.get("analysis_date")
    if ts:
        try:
            session_date = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            session_date = None

    session_id = upsert_dim_session(
        session_uid=session_uid,
        source_file=source_file_name,
        session_date=session_date,
        court_surface=(data.get("session", {}) or {}).get("surface"),
        venue=(data.get("session", {}) or {}).get("venue"),
    )

    # --- Players
    player_map = {}  # sportai_player_uid -> db player_id
    for p in data.get("players", []):
        uid = str(p.get("id") or p.get("player_id"))
        pid = upsert_dim_player(
            sportai_player_uid=uid,
            full_name=p.get("name"),
            handedness=p.get("handedness"),
            age=p.get("age"),
            utr=p.get("utr"),
        )
        player_map[uid] = pid

    # --- Rallies (optional)
    rally_id_map = {}  # (session_id, rally_number) -> rally_id
    for r in data.get("rallies", []):
        rally_number = r.get("rally_number") or r.get("index")
        winner_uid = str(r.get("winner_player_id")) if r.get("winner_player_id") is not None else None
        rid = upsert_dim_rally(
            session_id=session_id,
            rally_number=rally_number,
            start_ts=_parse_ts(r.get("start_ts")),
            end_ts=_parse_ts(r.get("end_ts")),
            point_winner_player_id=player_map.get(winner_uid),
            length_shots=r.get("length_shots"),
        )
        rally_id_map[(session_id, rally_number)] = rid

    # --- Swings
    swing_rows = []
    for s in data.get("swings", []):
        uid = str(s.get("player_id")) if s.get("player_id") is not None else None
        rally_no = s.get("rally_number")
        swing_rows.append({
            "session_id": session_id,
            "rally_id": rally_id_map.get((session_id, rally_no)),
            "player_id": player_map.get(uid),
            "swing_start_ts": _parse_ts(s.get("swing_start_ts")),
            "swing_end_ts": _parse_ts(s.get("swing_end_ts")),
            "ball_hit_ts": _parse_ts(s.get("ball_hit_ts")),
            "swing_type": s.get("swing_type"),
            "is_serve": bool(s.get("is_serve")),
            "is_return": bool(s.get("is_return")),
            "is_in_rally": bool(s.get("is_in_rally")),
            "valid": s.get("valid"),
            "serve_number": s.get("serve_number"),
            "serve_location": s.get("serve_location"),
            "return_depth_box": s.get("return_depth_box"),
            "ball_x": s.get("ball_x"),
            "ball_y": s.get("ball_y"),
            "ball_speed": s.get("ball_speed"),
            "ball_player_dist": s.get("ball_player_dist"),
            "annotations_json": s.get("annotations"),
        })
    if swing_rows:
        insert_fact_swing_batch(swing_rows)

    # --- Bounces
    bounce_rows = []
    for b in data.get("ball_bounces", []):
        rally_no = b.get("rally_number")
        bounce_rows.append({
            "session_id": session_id,
            "rally_id": rally_id_map.get((session_id, rally_no)),
            "bounce_ts": _parse_ts(b.get("timestamp")),
            "bounce_x": b.get("x"),
            "bounce_y": b.get("y"),
        })
    if bounce_rows:
        insert_fact_bounce_batch(bounce_rows)

    print(f"✅ Ingest complete for session_uid={session_uid}  | swings={len(swing_rows)}  bounces={len(bounce_rows)}")

# ==========================================
# Entrypoint
# ==========================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
