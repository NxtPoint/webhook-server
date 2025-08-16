from flask import Flask, request, jsonify, render_template
import requests
import os
import json
from datetime import datetime, timezone, timedelta
from werkzeug.utils import secure_filename
from threading import Thread
import time
from json_to_powerbi_csv import export_csv_from_json

# DB imports (single approach: SQL-only)
from sqlalchemy import create_engine, text
from db_init import init_db  # our schema creator

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

# =======================
# Single, shared engine
# =======================
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

                # Ingest JSON into Postgres
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

# 🆕 Reingest an already-saved JSON (no save step, just parse & load)
@app.get("/ops/reingest")
def ops_reingest():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    json_name = request.args.get("name")
    if not json_name:
        return ("Missing ?name=<filename.json>", 400)
    p = os.path.join(os.getcwd(), "data", json_name)
    if not os.path.isfile(p):
        return (f"File not found: {p}", 404)
    try:
        ingest_sportai_json(p, json_name)
        return {"ok": True, "reingested": json_name}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# 🆕 Upload a JSON directly to the server, then auto-ingest
@app.post("/ops/upload-json")
def upload_json_and_ingest():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)

    name = request.args.get("name")
    if not name:
        return ("Missing ?name=<filename.json>", 400)
    if not name.endswith(".json"):
        return ("Name must end with .json", 400)

    os.makedirs("data", exist_ok=True)
    dest_path = os.path.join(os.getcwd(), "data", secure_filename(name))

    try:
        if "file" in request.files:
            # multipart/form-data upload
            f = request.files["file"]
            f.save(dest_path)
        else:
            # raw JSON body
            if not request.data:
                return ("No file part and no request body provided", 400)
            with open(dest_path, "wb") as out:
                out.write(request.data)

        ingest_sportai_json(dest_path, name)
        return jsonify({"ok": True, "saved": name, "ingested": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# 🆕 Peek at JSON structure: top-level keys + sample keys for arrays
@app.get("/ops/peek-json")
def peek_json():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    json_name = request.args.get("name")
    if not json_name:
        return ("Missing ?name=<filename.json>", 400)
    p = os.path.join(os.getcwd(), "data", json_name)
    if not os.path.isfile(p):
        return (f"File not found: {p}", 404)

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)

        def sample_keys(obj):
            if isinstance(obj, dict):
                return sorted(list(obj.keys()))[:30]
            if isinstance(obj, list) and obj:
                if isinstance(obj[0], dict):
                    return sorted(list(obj[0].keys()))[:30]
                return ["<list-primitive>"]
            return ["<unknown>"]

        top = sorted(list(data.keys())) if isinstance(data, dict) else ["<not-a-dict>"]
        out = {"top_level_keys": top}

        for arr_key in ["swings", "shots", "strokes", "events", "rallies", "points", "ball_bounces", "bounces"]:
            if isinstance(data, dict) and arr_key in data:
                out[arr_key] = {"len": len(data[arr_key]), "sample_item_keys": sample_keys(data[arr_key])}

        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 🆕 Read-only SQL endpoint (GET and POST), guarded by OPS_KEY
def _is_safe_select(query: str) -> bool:
    if not query:
        return False
    q = query.strip().lower()
    # allow only select/with (CTE) queries; block ; to avoid batching
    if ";" in q:
        return False
    return q.startswith("select ") or q.startswith("with ")

@app.get("/ops/sql")
def ops_sql_get():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return jsonify({"error": "unauthorized"}), 403
    if engine is None:
        return jsonify({"error": "no database engine"}), 500

    q = request.args.get("q", "")
    if not _is_safe_select(q):
        return jsonify({"error": "only single SELECT/WITH queries without ';' are allowed"}), 400

    try:
        with engine.connect() as conn:
            rows = conn.execute(text(q)).mappings().all()
        return jsonify({"ok": True, "rows": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.post("/ops/sql")
def ops_sql_post():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return jsonify({"error": "unauthorized"}), 403
    if engine is None:
        return jsonify({"error": "no database engine"}), 500

    payload = request.get_json(silent=True) or {}
    q = (payload.get("q") or "").strip()
    if not _is_safe_select(q):
        return jsonify({"error": "only single SELECT/WITH queries without ';' are allowed"}), 400

    try:
        with engine.connect() as conn:
            rows = conn.execute(text(q)).mappings().all()
        return jsonify({"ok": True, "rows": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# ==========================================
# Ingestion helpers & glue (idempotent + flexible)
# ==========================================
def _parse_ts_iso(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def _parse_ts_flex(v, session_start=None):
    """
    - If ISO string -> parse to aware UTC
    - If int/float -> treat as seconds offset from session_start (if provided)
    """
    t = _parse_ts_iso(v)
    if t:
        return t
    if isinstance(v, (int, float)) and session_start:
        try:
            return (session_start + timedelta(seconds=float(v))).astimezone(timezone.utc)
        except Exception:
            return None
    return None

def _first_present(dct, keys, default=None):
    if not isinstance(dct, dict):
        return default
    for k in keys:
        if k in dct:
            return dct[k]
    return default

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
      - rallies -> dim_rally (flex keys)
      - swings  -> fact_swing (recursive discovery with flex keys)
      - ball_bounces -> fact_bounce (flex keys)
    Also: clears facts for the session first (idempotent re-ingest).
    """
    if engine is None:
        print("⚠️ No DB engine. Skipping ingest.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # ---- Session basics
    session_payload = (data.get("session", {}) or {})
    session_uid = session_payload.get("id") or data.get("id") or source_file_name
    session_start = _parse_ts_iso(session_payload.get("start_time")) or _parse_ts_iso(data.get("analysis_date"))
    session_date = session_start

    session_id = upsert_dim_session(
        session_uid=session_uid,
        source_file=source_file_name,
        session_date=session_date,
        court_surface=session_payload.get("surface"),
        venue=session_payload.get("venue"),
    )

    # Make re-ingest idempotent: clear facts for this session before inserting
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fact_swing  WHERE session_id = :sid"), {"sid": session_id})
        conn.execute(text("DELETE FROM fact_bounce WHERE session_id = :sid"), {"sid": session_id})
    print("♻️ Cleared existing fact rows for session_id", session_id)

    # ---- Players
    player_map = {}
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

    # ---- Rallies (flex)
    def first_present(dct, keys, default=None):
        if not isinstance(dct, dict):
            return default
        for k in keys:
            if k in dct:
                return dct[k]
        return default

    rallies = first_present(data, ["rallies", "points", "rally_list"], default=[])
    rally_id_map = {}
    if isinstance(rallies, list):
        for r in rallies:
            rally_number = r.get("rally_number") or r.get("index") or r.get("rally") or r.get("point_index")
            winner_uid = r.get("winner_player_id") or r.get("winner") or r.get("point_winner_player_id")
            winner_uid = str(winner_uid) if winner_uid is not None else None

            rid = upsert_dim_rally(
                session_id=session_id,
                rally_number=rally_number,
                start_ts=_parse_ts_flex(r.get("start_ts") or r.get("start_time"), session_start),
                end_ts=_parse_ts_flex(r.get("end_ts") or r.get("end_time"), session_start),
                point_winner_player_id=player_map.get(winner_uid),
                length_shots=r.get("length_shots") or r.get("shots") or r.get("strokes"),
            )
            rally_id_map[(session_id, rally_number)] = rid

    # ---- Swings (recursive discovery)
    SWING_LIST_KEYS = {"swings", "shots", "strokes", "events"}

    def is_swing_like_event(ev: dict) -> bool:
        if not isinstance(ev, dict):
            return False
        et = ev.get("event_type") or ev.get("type") or ev.get("swing_type") or ev.get("shot_type")
        if et is None:
            return True  # many JSONs omit explicit type for swing rows
        if isinstance(et, str):
            etl = et.lower()
            return any(tok in etl for tok in ["swing", "stroke", "shot", "hit"])
        return True

    def collect_candidate_lists(obj):
        """Recursively yield lists of dicts under any SWING_LIST_KEYS; also yield any list of dicts that looks swingy."""
        results = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, list):
                    if k in SWING_LIST_KEYS and v and isinstance(v[0], dict):
                        results.append(v)
                    elif v and isinstance(v[0], dict):
                        sample = v[:5]
                        hits = 0
                        for it in sample:
                            if any(x in it for x in ["swing_type", "shot_type", "player_id", "ball_hit_ts", "contact_ts", "start_ts", "end_ts"]):
                                hits += 1
                        if hits >= max(2, len(sample)//2):
                            results.append(v)
                elif isinstance(v, dict):
                    results += collect_candidate_lists(v)
        elif isinstance(obj, list):
            for it in obj:
                results += collect_candidate_lists(it)
        return results

    swing_rows = []
    candidates = collect_candidate_lists(data)

    if not candidates and isinstance(rallies, list):
        for r in rallies:
            for key in ["swings", "shots", "strokes", "events"]:
                if isinstance(r.get(key), list) and r.get(key) and isinstance(r[key][0], dict):
                    candidates.append(r[key])

    def add_swing_row(s, rally_no_hint=None):
        if not isinstance(s, dict):
            return
        if not is_swing_like_event(s):
            return

        uid_raw = s.get("player_id") or s.get("player") or s.get("hitter_id") or s.get("hitter")
        uid = str(uid_raw) if uid_raw is not None else None

        rally_no = s.get("rally_number") or s.get("rally") or s.get("point_index") or rally_no_hint

        swing_start = _parse_ts_flex(s.get("swing_start_ts") or s.get("start_ts") or s.get("start_time"), session_start)
        swing_end   = _parse_ts_flex(s.get("swing_end_ts")   or s.get("end_ts")   or s.get("end_time"),   session_start)
        hit_ts      = _parse_ts_flex(s.get("ball_hit_ts")    or s.get("contact_ts") or s.get("timestamp") or s.get("time"), session_start)

        swing_rows.append({
            "session_id": session_id,
            "rally_id": rally_id_map.get((session_id, rally_no)),
            "player_id": player_map.get(uid),
            "swing_start_ts": swing_start,
            "swing_end_ts": swing_end,
            "ball_hit_ts": hit_ts,
            "swing_type": s.get("swing_type") or s.get("shot_type") or (s.get("type") if isinstance(s.get("type"), str) else None),
            "is_serve": bool(s.get("is_serve") or s.get("serve")),
            "is_return": bool(s.get("is_return") or s.get("return")),
            "is_in_rally": bool(s.get("is_in_rally") or s.get("in_rally")),
            "valid": s.get("valid"),
            "serve_number": s.get("serve_number"),
            "serve_location": s.get("serve_location"),
            "return_depth_box": s.get("return_depth_box"),
            "ball_x": s.get("ball_x") or s.get("x"),
            "ball_y": s.get("ball_y") or s.get("y"),
            "ball_speed": s.get("ball_speed") or s.get("speed"),
            "ball_player_dist": s.get("ball_player_dist") or s.get("distance"),
            "annotations_json": s.get("annotations"),
        })

    for lst in candidates:
        rally_hint = None
        for s in lst[:3]:
            if isinstance(s, dict):
                rally_hint = s.get("rally_number") or s.get("rally") or s.get("point_index") or rally_hint
        for s in lst:
            add_swing_row(s, rally_no_hint=rally_hint)

    if swing_rows:
        inserted_sw = insert_fact_swing_batch(swing_rows)
        print(f"🟩 Swings discovered: {len(swing_rows)}, inserted: {inserted_sw}")
    else:
        print("🟨 No swings discovered anywhere in JSON.")

    # ---- Bounces (flex)
    bounces = first_present(data, ["ball_bounces", "bounces"], default=[])
    bounce_rows = []
    if isinstance(bounces, list):
        for b in bounces:
            rally_no = b.get("rally_number") or b.get("rally") or b.get("point_index")
            bounce_rows.append({
                "session_id": session_id,
                "rally_id": rally_id_map.get((session_id, rally_no)),
                "bounce_ts": _parse_ts_flex(b.get("timestamp") or b.get("ts") or b.get("time"), session_start),
                "bounce_x": b.get("x") or b.get("ball_x"),
                "bounce_y": b.get("y") or b.get("ball_y"),
            })
    if bounce_rows:
        inserted_b = insert_fact_bounce_batch(bounce_rows)
        print(f"🟩 Bounces detected: {len(bounce_rows)}, inserted: {inserted_b}")
    else:
        print("🟨 No bounces detected.")

    print(f"✅ Ingest complete for session_uid={session_uid}")

# ==========================================
# Entrypoint
# ==========================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
