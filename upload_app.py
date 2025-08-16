from flask import Flask, request, jsonify, render_template
import requests
import os
import json
from datetime import datetime, timezone, timedelta
from werkzeug.utils import secure_filename
from threading import Thread
import time
from json_to_powerbi_csv import export_csv_from_json

# DB imports (SQL-only)
from sqlalchemy import create_engine, text
from db_init import init_db  # schema creator

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
            f = request.files["file"]
            f.save(dest_path)
        else:
            if not request.data:
                return ("No file part and no request body provided", 400)
            with open(dest_path, "wb") as out:
                out.write(request.data)

        ingest_sportai_json(dest_path, name)
        return jsonify({"ok": True, "saved": name, "ingested": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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
        try:
            fps = (data.get("session") or {}).get("fps") or data.get("fps")
            if fps:
                out["fps"] = fps
        except Exception:
            pass

        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==========================================
# SQL helper endpoints (read-only)
# ==========================================
def _is_safe_select(query: str) -> bool:
    if not query:
        return False
    q = query.strip().lower()
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
# Timestamp helpers
# ==========================================
def _parse_ts_iso(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def _from_epoch_like(val):
    if val is None:
        return None
    try:
        x = float(val)
    except Exception:
        return None
    try:
        if x > 1e12:  # ms
            return datetime.fromtimestamp(x / 1000.0, tz=timezone.utc)
        if x > 1e9:   # sec
            return datetime.fromtimestamp(x, tz=timezone.utc)
    except Exception:
        return None
    return None

def _parse_ts_flex(v, session_start=None, fps=None):
    # 1) ISO
    t = _parse_ts_iso(v)
    if t:
        return t
    # 2) epoch-like absolute
    if isinstance(v, (int, float, str)):
        abs_t = _from_epoch_like(v)
        if abs_t:
            return abs_t
    # 3) numeric seconds relative
    if isinstance(v, (int, float)) and session_start:
        try:
            return (session_start + timedelta(seconds=float(v))).astimezone(timezone.utc)
        except Exception:
            pass
    # 4) dict keys
    sec = None; ms = None; frame_idx = None
    if isinstance(v, dict):
        sec = v.get("seconds") or v.get("time_s") or v.get("sec")
        ms  = v.get("time_ms") or v.get("timestamp_ms") or v.get("ms") or v.get("millis") or v.get("epoch_ms")
        if ms is None:
            maybe_epoch = v.get("epoch") or v.get("unix") or v.get("unix_s")
            e = _from_epoch_like(maybe_epoch)
            if e:
                return e
        frame_idx = v.get("frame") or v.get("frame_idx") or v.get("frameIndex") or v.get("contact_frame")
    if sec is not None and session_start is not None:
        try:
            return (session_start + timedelta(seconds=float(sec))).astimezone(timezone.utc)
        except Exception:
            pass
    if ms is not None:
        e = _from_epoch_like(ms)
        if e:
            return e
        if session_start is not None:
            try:
                return (session_start + timedelta(milliseconds=float(ms))).astimezone(timezone.utc)
            except Exception:
                pass
    if frame_idx is not None and session_start is not None and fps:
        try:
            return (session_start + timedelta(seconds=float(frame_idx) / float(fps))).astimezone(timezone.utc)
        except Exception:
            pass
    return None

# ==========================================
# DB upserts/inserts
# ==========================================
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

# ==========================================
# Ingest (bounce-first; derive session_start; derive rallies; strict swings)
# ==========================================
def ingest_sportai_json(json_path, source_file_name):
    """
    Loads:
      - bounces FIRST (to set fallback session_start)
      - session -> dim_session
      - players -> dim_player
      - rallies -> dim_rally (from JSON or derived from bounces/time gaps)
      - swings  -> fact_swing (only if real swing-like data exists)
      - bounces -> fact_bounce
    Clears facts for this session on each ingest (idempotent).
    """
    if engine is None:
        print("⚠️ No DB engine. Skipping ingest.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    fps = _get_fps_safe(data=data)

    # -------- Collect bounces first (to maybe derive session_start)
    raw_bounces = (data.get("ball_bounces") or data.get("bounces") or [])
    pre_bounce_abs = []   # absolute datetimes parsed (epoch/ISO)
    pre_bounce_rel = []   # numeric seconds (relative)
    pre_bounces = []

    for b in raw_bounces if isinstance(raw_bounces, list) else []:
        if not isinstance(b, dict):
            continue
        raw_t = b.get("timestamp") or b.get("ts") or b.get("time") or b.get("time_ms") or b.get("timestamp_ms") or b.get("ms") or b.get("frame")
        # absolute attempt
        abs_t = _parse_ts_flex(raw_t, session_start=None, fps=fps)
        if abs_t:
            pre_bounce_abs.append(abs_t)
        # relative seconds detection (small numbers < epoch sec)
        try:
            if abs_t is None and isinstance(raw_t, (int, float)) and float(raw_t) < 1e9:
                pre_bounce_rel.append(float(raw_t))
        except Exception:
            pass
        pre_bounces.append(b)

    # -------- Work out session_start
    session_payload = (data.get("session") or {})
    session_start = _parse_ts_iso(session_payload.get("start_time")) or _parse_ts_iso(data.get("analysis_date"))

    if session_start is None:
        if pre_bounce_abs:
            # If any bounce had an absolute time, anchor session_start just before the earliest
            session_start = min(pre_bounce_abs) - timedelta(seconds=2)
            print("⏰ session_start from absolute bounces:", session_start.isoformat())
        elif pre_bounce_rel:
            # All we have are relative seconds; anchor to epoch 1970-01-01 so DB timestamps aren’t null
            session_start = datetime(1970, 1, 1, tzinfo=timezone.utc)
            print("⏰ session_start set to epoch (relative-seconds mode)")

    # -------- Upsert session
    session_uid = session_payload.get("id") or data.get("id") or source_file_name
    session_id = upsert_dim_session(
        session_uid=session_uid,
        source_file=source_file_name,
        session_date=session_start,
        court_surface=session_payload.get("surface"),
        venue=session_payload.get("venue"),
    )

    # -------- Idempotent: clear facts for this session
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fact_swing  WHERE session_id = :sid"), {"sid": session_id})
        conn.execute(text("DELETE FROM fact_bounce WHERE session_id = :sid"), {"sid": session_id})
    print("♻️ Cleared existing fact rows for session_id", session_id)

    # -------- Players (best-effort)
    player_map = {}
    players = data.get("players", [])
    if isinstance(players, dict):
        players = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in players.items()]
    for p in players or []:
        uid = str(p.get("id") or p.get("player_id") or p.get("playerId") or p.get("athlete_id") or p.get("player_index"))
        name = p.get("name") or p.get("full_name") or (" ".join([x for x in [p.get("first_name"), p.get("last_name")] if x])) or None
        pid = upsert_dim_player(
            sportai_player_uid=uid,
            full_name=name,
            handedness=p.get("handedness") or p.get("hand") or p.get("dominant_hand"),
            age=p.get("age"),
            utr=p.get("utr"),
        )
        player_map[uid] = pid

    # -------- Build bounces with final session_start
    bounce_rows = []
    bounces_by_rally = {}
    for b in pre_bounces:
        rn = b.get("rally_number") or b.get("rally") or b.get("point_index") or b.get("rallyIndex") or b.get("point")
        ts = _parse_ts_flex(
            b.get("timestamp") or b.get("ts") or b.get("time") or b.get("time_ms") or b.get("timestamp_ms") or b.get("ms") or b.get("frame"),
            session_start, fps
        )
        bx = (b.get("court_pos") or {}).get("x") if isinstance(b.get("court_pos"), dict) else b.get("x") or b.get("ball_x")
        by = (b.get("court_pos") or {}).get("y") if isinstance(b.get("court_pos"), dict) else b.get("y") or b.get("ball_y")
        bounce_rows.append({"rally_no": rn, "bounce_ts": ts, "bounce_x": bx, "bounce_y": by})
        if rn is not None and ts is not None:
            bounces_by_rally.setdefault(rn, []).append(ts)

    # -------- Rallies from JSON (if present)
    raw_rallies = data.get("rallies") or data.get("points") or data.get("rally_list") or []
    rally_id_map = {}
    if isinstance(raw_rallies, list) and raw_rallies:
        for r in raw_rallies:
            rally_number = r.get("rally_number") or r.get("index") or r.get("rally") or r.get("point_index") or r.get("rallyIndex") or r.get("point")
            rid = upsert_dim_rally(
                session_id=session_id,
                rally_number=rally_number,
                start_ts=_parse_ts_flex(r.get("start_ts") or r.get("start_time") or r.get("start"), session_start, fps),
                end_ts=_parse_ts_flex(r.get("end_ts") or r.get("end_time") or r.get("end"), session_start, fps),
                point_winner_player_id=None,
                length_shots=r.get("length_shots") or r.get("shots") or r.get("strokes"),
            )
            rally_id_map[(session_id, rally_number)] = rid

    # -------- Derive rallies from bounces
    if not rally_id_map and bounces_by_rally:
        for rn, ts_list in bounces_by_rally.items():
            start_ts = min(ts_list) if ts_list else None
            end_ts = max(ts_list) if ts_list else None
            rid = upsert_dim_rally(
                session_id=session_id,
                rally_number=rn,
                start_ts=start_ts,
                end_ts=end_ts,
                point_winner_player_id=None,
                length_shots=None,
            )
            rally_id_map[(session_id, rn)] = rid
        print(f"🧩 Derived {len(bounces_by_rally)} rallies from bounces (numbered).")

    if not rally_id_map and bounce_rows:
        # cluster by time gaps when no rally numbers at all
        ts_sorted = sorted([b["bounce_ts"] for b in bounce_rows if b["bounce_ts"]])
        clusters = []
        if ts_sorted:
            current = [ts_sorted[0]]
            for t in ts_sorted[1:]:
                if (t - current[-1]).total_seconds() > 6.0:
                    clusters.append(current)
                    current = [t]
                else:
                    current.append(t)
            clusters.append(current)
        cluster_map = {}
        for idx, cl in enumerate(clusters, start=1):
            st = min(cl); en = max(cl)
            rid = upsert_dim_rally(
                session_id=session_id,
                rally_number=idx,
                start_ts=st,
                end_ts=en,
                point_winner_player_id=None,
                length_shots=None,
            )
            rally_id_map[(session_id, idx)] = rid
            cluster_map[idx] = (st, en)
        # assign rally_no to bounces
        for b in bounce_rows:
            t = b["bounce_ts"]
            if t is None:
                continue
            chosen = None
            for idx, (st, en) in cluster_map.items():
                if st <= t <= en:
                    chosen = idx; break
            if chosen is None:
                # nearest window center
                best = None; best_idx = None
                for idx, (st, en) in cluster_map.items():
                    mid = st + (en - st)/2
                    delta = abs((t - mid).total_seconds())
                    if best is None or delta < best:
                        best = delta; best_idx = idx
                chosen = best_idx
            b["rally_no"] = chosen
        print(f"🧩 Derived {len(cluster_map)} rallies by time-gap clustering.")

    # -------- Build rally windows for mapping
    with engine.connect() as conn:
        windows = conn.execute(text("""
            SELECT rally_id, rally_number, start_ts, end_ts
            FROM dim_rally WHERE session_id = :sid
        """), {"sid": session_id}).mappings().all()
    win_list = [(w["rally_number"], w["rally_id"], w["start_ts"], w["end_ts"]) for w in windows]

    def _find_rid_for_time(ts):
        if ts is None:
            return None
        for rn, rid, st, en in win_list:
            if st and en and st <= ts <= en:
                return rid
        # nearest window center
        best = None; best_rid = None
        for rn, rid, st, en in win_list:
            if st and en:
                mid = st + (en - st)/2
                delta = abs((ts - mid).total_seconds())
                if best is None or delta < best:
                    best = delta; best_rid = rid
        return best_rid

    # -------- STRICT swing collection (your file likely has none)
    # Only consider arrays explicitly named swings/shots/strokes/events, AND
    # require at least ONE of these keys in item: swing_type, shot_type, ball_hit_ts, contact_ts
    swings_collected = []
    candidate_keys = ["swings", "shots", "strokes", "events"]
    for key in candidate_keys:
        lst = data.get(key)
        if isinstance(lst, list) and lst and isinstance(lst[0], dict):
            for s in lst:
                if not isinstance(s, dict):
                    continue
                if not any(k in s for k in ["swing_type", "shot_type", "ball_hit_ts", "contact_ts"]):
                    continue  # not swing-like enough
                uid_raw = s.get("player_id") or s.get("player") or s.get("hitter_id") or s.get("hitter") or s.get("playerId") or s.get("athlete_id") or s.get("player_index")
                uid = str(uid_raw) if uid_raw is not None else None
                rally_no = s.get("rally_number") or s.get("rally") or s.get("point_index") or s.get("rallyIndex") or s.get("point")
                swing_start = _parse_ts_flex(
                    s.get("swing_start_ts") or s.get("start_ts") or s.get("start_time") or s.get("start") \
                    or s.get("time_s") or s.get("seconds") or s.get("time_ms") or s.get("timestamp_ms") or s.get("ms") \
                    or s.get("frame") or s.get("frame_idx") or s.get("frameIndex"),
                    session_start, fps
                )
                swing_end   = _parse_ts_flex(
                    s.get("swing_end_ts") or s.get("end_ts") or s.get("end_time") or s.get("end"),
                    session_start, fps
                )
                hit_src = s.get("ball_hit_ts") or s.get("contact_ts") or s.get("hit_ts")
                ball_hit_ts = _parse_ts_flex(hit_src, session_start, fps)
                swings_collected.append({
                    "rally_no": rally_no,
                    "player_uid": uid,
                    "swing_start_ts": swing_start,
                    "swing_end_ts": swing_end,
                    "ball_hit_ts": ball_hit_ts,
                    "swing_type": s.get("swing_type") or s.get("shot_type"),
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

    # Map swings to rallies + players, then insert
    swing_rows = []
    if swings_collected:
        for sw in swings_collected:
            rid = None
            if sw["rally_no"] is not None:
                rid = next((rid for rn, rid, st, en in win_list if rn == sw["rally_no"]), None)
            if rid is None:
                ts_for_match = sw["ball_hit_ts"] or sw["swing_start_ts"] or sw["swing_end_ts"]
                rid = _find_rid_for_time(ts_for_match)
            pid = player_map.get(str(sw["player_uid"])) if sw["player_uid"] is not None else None
            swing_rows.append({
                "session_id": session_id,
                "rally_id": rid,
                "player_id": pid,
                "swing_start_ts": sw["swing_start_ts"],
                "swing_end_ts": sw["swing_end_ts"],
                "ball_hit_ts": sw["ball_hit_ts"],
                "swing_type": sw["swing_type"],
                "is_serve": sw["is_serve"],
                "is_return": sw["is_return"],
                "is_in_rally": sw["is_in_rally"],
                "valid": sw["valid"],
                "serve_number": sw["serve_number"],
                "serve_location": sw["serve_location"],
                "return_depth_box": sw["return_depth_box"],
                "ball_x": sw["ball_x"],
                "ball_y": sw["ball_y"],
                "ball_speed": sw["ball_speed"],
                "ball_player_dist": sw["ball_player_dist"],
                "annotations_json": sw["annotations_json"],
            })
        if swing_rows:
            inserted_sw = insert_fact_swing_batch(swing_rows)
            print(f"🟩 Swings discovered: {len(swing_rows)}, inserted: {inserted_sw}")
    else:
        print("🟨 No swings arrays present in this JSON — skipping swings.")

    # Insert bounces with rally_ids
    final_bounce_rows = []
    for b in bounce_rows:
        rn = b.get("rally_no")
        rid = next((rid for rno, rid, st, en in win_list if rno == rn), None)
        if rid is None:
            rid = _find_rid_for_time(b.get("bounce_ts"))
        final_bounce_rows.append({
            "session_id": session_id,
            "rally_id": rid,
            "bounce_ts": b["bounce_ts"],
            "bounce_x": b["bounce_x"],
            "bounce_y": b["bounce_y"],
        })
    if final_bounce_rows:
        inserted_b = insert_fact_bounce_batch(final_bounce_rows)
        print(f"🟩 Bounces detected: {len(final_bounce_rows)}, inserted: {inserted_b}")
    else:
        print("🟨 No bounces detected.")

    print(f"✅ Ingest complete for session_uid={session_uid}")

# small helper to get fps safely
def _get_fps_safe(data):
    try:
        s = (data.get("session") or {})
        return s.get("fps") or s.get("frame_rate") or s.get("frames_per_second") \
               or (data.get("video") or {}).get("fps") \
               or data.get("fps")
    except Exception:
        return None

# ==========================================
# Entrypoint
# ==========================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
