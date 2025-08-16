from flask import Flask, request, jsonify, render_template
import requests
import os
import json
from datetime import datetime, timezone, timedelta
from threading import Thread
import time

# DB
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Local helpers
from db_init import init_db
from db_views import create_views
from json_to_powerbi_csv import export_csv_from_json  # unchanged

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
# Database setup
# =======================
engine = None
SessionLocal = None
if DATABASE_URL:
    try:
        engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_size=int(os.getenv("POOL_SIZE", "5")),
            max_overflow=0,
        )
        SessionLocal = sessionmaker(bind=engine)
        print("✅ SQLAlchemy engine created")
    except Exception as e:
        print("❌ Failed to create SQLAlchemy engine:", str(e))
else:
    print("⚠️ DATABASE_URL is not set. DB features will be disabled.")

# =======================
# Dropbox OAuth helpers
# =======================
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

# =======================
# Upload page
# =======================
@app.route('/')
def index():
    return render_template("upload.html")

# =======================
# Upload → Dropbox → SportAI
# =======================
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

    # share link
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

    # Kick SportAI task
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
    Thread(target=poll_and_save_result, args=(task_id, email)).start()

    return jsonify({
        "message": "Upload and analysis started",
        "dropbox_url": raw_url,
        "sportai_task_id": task_id
    }), 200

# =======================
# Poll SportAI → Save JSON → Export CSV → Ingest
# =======================
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
                return None

            print(f"📡 Downloading JSON from: {result_url}")
            result_res = requests.get(result_url)
            if result_res.status_code == 200:
                os.makedirs("data", exist_ok=True)
                timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                local_filename = f"data/sportai-{task_id}-{email}-{timestamp}.json"
                with open(local_filename, "w", encoding="utf-8") as f:
                    f.write(result_res.text)

                # Export CSV for Power BI (unchanged)
                try:
                    export_csv_from_json(local_filename)
                except Exception as e:
                    print(f"⚠️ CSV export failed: {str(e)}")

                # New: ingest using v2-aware parser
                try:
                    ingest_result_v2(local_filename, os.path.basename(local_filename), replace=False)
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

# =======================
# OPS: init DB
# =======================
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

# =======================
# OPS: simple DB ping
# =======================
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

# =======================
# OPS: quick table counts
# =======================
@app.get("/ops/db-counts")
def db_counts():
    if engine is None:
        return {"ok": False, "error": "No engine (DATABASE_URL not set)."}, 500
    try:
        counts = {}
        with engine.connect() as conn:
            for t in ["dim_session", "dim_player", "dim_rally", "fact_swing", "fact_bounce", "fact_ball_position", "fact_player_position"]:
                counts[t] = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        return {"ok": True, "counts": counts}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# =======================
# OPS: SQL peek
# =======================
@app.get("/ops/sql")
def ops_sql():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    q = request.args.get("q")
    if not q:
        return ("Missing ?q=...", 400)
    try:
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(text(q))]
        return {"ok": True, "rows": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# =======================
# OPS: drop+recreate views
# =======================
@app.get("/ops/init-views")
def ops_init_views():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    if engine is None:
        return {"ok": False, "error": "No engine (DATABASE_URL not set)."}, 500
    try:
        create_views(engine)
        return {"ok": True, "status": "views dropped & recreated"}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# =======================
# OPS: ingest a local JSON (legacy endpoint)
# =======================
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
    replace = request.args.get("replace") in ("1", "true", "yes")
    try:
        ingest_result_v2(json_path, json_name, replace=replace)
        return {"ok": True, "ingested": json_name, "replace": replace}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# =======================
# Ingestion (SportAI v2 spec)
# =======================
def seconds_to_ts(base_dt, s):
    if s is None:
        return None
    try:
        return base_dt + timedelta(seconds=float(s))
    except Exception:
        return None

def ensure_session(session_uid, source_file=None, session_date=None, fps=None, court_surface=None, venue=None):
    """
    Upsert a session row and return session_id.
    """
    sql = text("""
        INSERT INTO dim_session (session_uid, source_file, session_date, fps, court_surface, venue)
        VALUES (:uid, :src, :sdt, :fps, :surf, :ven)
        ON CONFLICT (session_uid) DO UPDATE SET
            source_file = EXCLUDED.source_file,
            session_date = COALESCE(EXCLUDED.session_date, dim_session.session_date),
            fps = COALESCE(EXCLUDED.fps, dim_session.fps),
            court_surface = COALESCE(EXCLUDED.court_surface, dim_session.court_surface),
            venue = COALESCE(EXCLUDED.venue, dim_session.venue)
        RETURNING session_id;
    """)
    with engine.begin() as conn:
        sid = conn.execute(sql, dict(uid=session_uid, src=source_file, sdt=session_date, fps=fps,
                                     surf=court_surface, ven=venue)).scalar_one()
    return sid

def upsert_player(session_id, sportai_player_uid, full_name=None, handedness=None, age=None, utr=None,
                  covered_distance=None, fastest_sprint=None, fastest_sprint_timestamp_s=None,
                  activity_score=None, swing_type_distribution=None, location_heatmap=None):
    sql = text("""
        INSERT INTO dim_player (session_id, sportai_player_uid, full_name, handedness, age, utr,
                                covered_distance, fastest_sprint, fastest_sprint_timestamp_s,
                                activity_score, swing_type_distribution, location_heatmap)
        VALUES (:sid, :uid, :name, :handed, :age, :utr, :cov, :fs, :fst, :ascore, :dist, :lheat)
        ON CONFLICT ON CONSTRAINT uq_dim_player_sess_uid DO UPDATE SET
            full_name = COALESCE(EXCLUDED.full_name, dim_player.full_name),
            handedness = COALESCE(EXCLUDED.handedness, dim_player.handedness),
            age = COALESCE(EXCLUDED.age, dim_player.age),
            utr = COALESCE(EXCLUDED.utr, dim_player.utr),
            covered_distance = COALESCE(EXCLUDED.covered_distance, dim_player.covered_distance),
            fastest_sprint = COALESCE(EXCLUDED.fastest_sprint, dim_player.fastest_sprint),
            fastest_sprint_timestamp_s = COALESCE(EXCLUDED.fastest_sprint_timestamp_s, dim_player.fastest_sprint_timestamp_s),
            activity_score = COALESCE(EXCLUDED.activity_score, dim_player.activity_score),
            swing_type_distribution = COALESCE(EXCLUDED.swing_type_distribution, dim_player.swing_type_distribution),
            location_heatmap = COALESCE(EXCLUDED.location_heatmap, dim_player.location_heatmap)
        RETURNING player_id;
    """)
    with engine.begin() as conn:
        pid = conn.execute(sql, dict(
            sid=session_id, uid=str(sportai_player_uid), name=full_name, handed=handedness, age=age, utr=utr,
            cov=covered_distance, fs=fastest_sprint, fst=fastest_sprint_timestamp_s, ascore=activity_score,
            dist=json.dumps(swing_type_distribution) if swing_type_distribution is not None else None,
            lheat=json.dumps(location_heatmap) if location_heatmap is not None else None
        )).scalar_one()
    return pid

def upsert_rally(session_id, rally_number, start_ts, end_ts, length_shots=None, point_winner_player_id=None,
                 start_s=None, end_s=None):
    sql = text("""
        INSERT INTO dim_rally (session_id, rally_number, start_ts, end_ts, length_shots, point_winner_player_id)
        VALUES (:sid, :num, :st, :et, :len, :winner)
        ON CONFLICT (session_id, rally_number) DO UPDATE SET
            start_ts = EXCLUDED.start_ts,
            end_ts = EXCLUDED.end_ts,
            length_shots = COALESCE(EXCLUDED.length_shots, dim_rally.length_shots),
            point_winner_player_id = COALESCE(EXCLUDED.point_winner_player_id, dim_rally.point_winner_player_id)
        RETURNING rally_id;
    """)
    with engine.begin() as conn:
        rid = conn.execute(sql, dict(
            sid=session_id, num=int(rally_number), st=start_ts, et=end_ts, len=length_shots, winner=point_winner_player_id
        )).scalar_one()
    return rid

def insert_fact_swing_batch_v2(rows):
    if not rows:
        return 0
    cols = [
        "session_id","rally_id","player_id",
        "start_ts","end_ts","ball_hit_ts",
        "start_s","end_s","ball_hit_s",
        "start_frame","end_frame","ball_hit_frame",
        "swing_type","serve","volley","is_in_rally",
        "confidence","confidence_swing_type","confidence_volley",
        "rally_start_s","rally_end_s",
        "ball_hit_x","ball_hit_y","ball_player_distance","ball_speed",
        "ball_impact_location_x","ball_impact_location_y","ball_impact_type",
        "intercepting_player_uid","ball_trajectory","annotations_json"
    ]
    placeholders = ",".join([f":{c}" for c in cols])
    sql = text(f"INSERT INTO fact_swing ({','.join(cols)}) VALUES ({placeholders})")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def insert_fact_bounce_batch_v2(rows):
    if not rows:
        return 0
    cols = ["session_id","rally_id","timestamp_s","bounce_ts","bounce_x","bounce_y","hitter_player_id","bounce_type"]
    placeholders = ",".join([f":{c}" for c in cols])
    sql = text(f"INSERT INTO fact_bounce ({','.join(cols)}) VALUES ({placeholders})")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def insert_ball_positions(session_id, items, base_dt):
    if not items:
        return 0
    rows = []
    for it in items:
        ts_s = it.get("timestamp")
        rows.append({
            "session_id": session_id,
            "timestamp_s": ts_s,
            "ts": seconds_to_ts(base_dt, ts_s),
            "x_image": it.get("X"),
            "y_image": it.get("Y"),
        })
    sql = text("INSERT INTO fact_ball_position (session_id,timestamp_s,ts,x_image,y_image) VALUES (:session_id,:timestamp_s,:ts,:x_image,:y_image)")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def insert_player_positions(session_id, player_map_uid_to_id, positions_dict, base_dt):
    if not positions_dict:
        return 0
    rows = []
    for uid, arr in positions_dict.items():
        pid = player_map_uid_to_id.get(str(uid))
        for it in arr or []:
            ts_s = it.get("timestamp")
            rows.append({
                "session_id": session_id,
                "player_id": pid,
                "timestamp_s": ts_s,
                "ts": seconds_to_ts(base_dt, ts_s),
                "img_x": it.get("X"),
                "img_y": it.get("Y"),
                "court_x": it.get("court_X"),
                "court_y": it.get("court_Y"),
            })
    sql = text("""INSERT INTO fact_player_position (session_id,player_id,timestamp_s,ts,img_x,img_y,court_x,court_y)
                  VALUES (:session_id,:player_id,:timestamp_s,:ts,:img_x,:img_y,:court_x,:court_y)""")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def upsert_bounce_heatmap(session_id, heatmap):
    sql = text("""INSERT INTO bounce_heatmap (session_id, heatmap)
                  VALUES (:sid, :hp)
                  ON CONFLICT (session_id) DO UPDATE SET heatmap = EXCLUDED.heatmap""")
    with engine.begin() as conn:
        conn.execute(sql, {"sid": session_id, "hp": json.dumps(heatmap) if heatmap is not None else None})

def upsert_session_confidences(session_id, conf):
    if not conf:
        return
    final = conf.get("final_confidences") or conf.get("final") or {}
    pose_c = conf.get("pose_confidences")
    swing_c = conf.get("swing_confidences")
    ball_c = conf.get("ball_confidences")
    sql = text("""
        INSERT INTO session_confidences (session_id, pose, swing, swing_ball, ball, final, pose_confidences, swing_confidences, ball_confidences)
        VALUES (:sid, :pose, :swing, :swing_ball, :ball, :final, :pose_c, :swing_c, :ball_c)
        ON CONFLICT (session_id) DO UPDATE SET
          pose = COALESCE(EXCLUDED.pose, session_confidences.pose),
          swing = COALESCE(EXCLUDED.swing, session_confidences.swing),
          swing_ball = COALESCE(EXCLUDED.swing_ball, session_confidences.swing_ball),
          ball = COALESCE(EXCLUDED.ball, session_confidences.ball),
          final = COALESCE(EXCLUDED.final, session_confidences.final),
          pose_confidences = COALESCE(EXCLUDED.pose_confidences, session_confidences.pose_confidences),
          swing_confidences = COALESCE(EXCLUDED.swing_confidences, session_confidences.swing_confidences),
          ball_confidences = COALESCE(EXCLUDED.ball_confidences, session_confidences.ball_confidences)
    """)
    with engine.begin() as conn:
        conn.execute(sql, dict(
            sid=session_id,
            pose=final.get("pose") if isinstance(final, dict) else (conf.get("pose") if isinstance(conf, dict) else None),
            swing=final.get("swing") if isinstance(final, dict) else None,
            swing_ball=final.get("swing_ball") if isinstance(final, dict) else None,
            ball=final.get("ball") if isinstance(final, dict) else (conf.get("ball_detection_confidence") if isinstance(conf, dict) else None),
            final=final.get("final") if isinstance(final, dict) else None,
            pose_c=json.dumps(pose_c) if pose_c is not None else None,
            swing_c=json.dumps(swing_c) if swing_c is not None else None,
            ball_c=json.dumps(ball_c) if ball_c is not None else None,
        ))

def insert_team_sessions(session_id, sessions):
    if not sessions:
        return 0
    rows = []
    for s in sessions:
        rows.append({
            "session_id": session_id,
            "start_s": s.get("start_time"),
            "end_s": s.get("end_time"),
            "front_team": s.get("front_team") or s.get("team_front"),
            "back_team": s.get("back_team") or s.get("team_back"),
        })
    sql = text("""INSERT INTO team_session (session_id,start_s,end_s,front_team,back_team)
                  VALUES (:session_id,:start_s,:end_s,:front_team,:back_team)""")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def insert_highlights(session_id, highlights):
    if not highlights:
        return 0
    rows = []
    for h in highlights:
        start_s = (h.get("start") or {}).get("timestamp")
        end_s = (h.get("end") or {}).get("timestamp")
        rows.append({
            "session_id": session_id,
            "type": h.get("type"),
            "start_s": start_s,
            "end_s": end_s,
            "duration": h.get("duration"),
            "swing_count": h.get("swing_count"),
            "ball_speed": h.get("ball_speed"),
            "ball_distance": h.get("ball_distance"),
            "players_distance": h.get("players_distance"),
            "players_speed": h.get("players_speed"),
            "dynamic_score": h.get("dynamic_score"),
            "players_json": json.dumps(h.get("players")) if h.get("players") is not None else None
        })
    sql = text("""INSERT INTO highlight
                  (session_id,type,start_s,end_s,duration,swing_count,ball_speed,ball_distance,players_distance,players_speed,dynamic_score,players_json)
                  VALUES (:session_id,:type,:start_s,:end_s,:duration,:swing_count,:ball_speed,:ball_distance,:players_distance,:players_speed,:dynamic_score,:players_json)""")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def insert_thumbnails(session_id, thumbs_by_player):
    if not thumbs_by_player:
        return 0
    rows = []
    for uid, arr in thumbs_by_player.items():
        for item in arr or []:
            rows.append({
                "session_id": session_id,
                "player_uid": str(uid),
                "frame_nr": item.get("frame_nr"),
                "timestamp_s": item.get("timestamp"),
                "score": item.get("score"),
                "bbox": json.dumps(item.get("bbox")) if item.get("bbox") is not None else None
            })
    sql = text("""INSERT INTO thumbnail (session_id,player_uid,frame_nr,timestamp_s,score,bbox)
                  VALUES (:session_id,:player_uid,:frame_nr,:timestamp_s,:score,:bbox)""")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)

def upsert_raw_payload(session_id, payload):
    sql = text("""INSERT INTO raw_result (session_id, payload)
                  VALUES (:sid, :pl)
                  ON CONFLICT (session_id) DO UPDATE SET payload = EXCLUDED.payload""")
    with engine.begin() as conn:
        conn.execute(sql, {"sid": session_id, "pl": json.dumps(payload)})

def find_rally_id_for_time(rallies_index, t_s):
    """
    rallies_index: list of dicts with keys start_s, end_s, rally_id
    returns rally_id or None
    """
    if t_s is None:
        return None
    for r in rallies_index:
        if r["start_s"] is not None and r["end_s"] is not None and (r["start_s"] - 0.01) <= t_s <= (r["end_s"] + 0.01):
            return r["rally_id"]
    return None

def ingest_result_v2(json_path, source_file_name, replace=False):
    """
    Ingests SportAI result JSON following the official v2 spec you provided.
    If replace=True, the previous session (by session_uid == source_file_name) is deleted and rebuilt.
    """
    if engine is None:
        raise RuntimeError("No DB engine")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Choose a stable session_uid; use file name
    session_uid = source_file_name

    # Optional session meta if ever provided
    session_date = None  # if you have a real datetime, set it; else timestamps will be epoch-based
    fps = None
    court_surface = None
    venue = None

    # Replace mode: drop old session row to avoid dupes (cascades to facts)
    if replace:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM dim_session WHERE session_uid = :u"), {"u": session_uid})

    # Create / ensure session
    session_id = ensure_session(session_uid=session_uid, source_file=source_file_name,
                                session_date=session_date, fps=fps, court_surface=court_surface, venue=venue)

    # Base datetime to convert seconds → timestamp
    base_dt = session_date.astimezone(timezone.utc) if (session_date and session_date.tzinfo) else datetime(1970,1,1,tzinfo=timezone.utc)

    # --- Players (and map uid->player_id)
    players = data.get("players") or []
    player_map_uid_to_id = {}
    for p in players:
        uid = p.get("player_id")
        pid = upsert_player(
            session_id=session_id,
            sportai_player_uid=uid,
            full_name=None,
            handedness=None,
            age=None,
            utr=None,
            covered_distance=p.get("covered_distance"),
            fastest_sprint=p.get("fastest_sprint"),
            fastest_sprint_timestamp_s=p.get("fastest_sprint_timestamp"),
            activity_score=p.get("activity_score"),
            swing_type_distribution=p.get("swing_type_distribution"),
            location_heatmap=p.get("location_heatmap"),
        )
        player_map_uid_to_id[str(uid)] = pid

    # --- Rallies (top-level list of [start,end] seconds)
    rallies = data.get("rallies") or []
    rallies_index = []
    rally_number = 0
    for pair in rallies:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        rally_number += 1
        start_s, end_s = pair[0], pair[1]
        start_ts = seconds_to_ts(base_dt, start_s)
        end_ts   = seconds_to_ts(base_dt, end_s)
        rid = upsert_rally(session_id, rally_number, start_ts, end_ts, length_shots=None, point_winner_player_id=None)
        rallies_index.append({"rally_id": rid, "rally_number": rally_number, "start_s": start_s, "end_s": end_s})

    # --- Swings (nested per player)
    swing_rows = []
    for p in players:
        uid = str(p.get("player_id"))
        pid = player_map_uid_to_id.get(uid)
        for s in p.get("swings") or []:
            start_s = (s.get("start") or {}).get("timestamp")
            end_s   = (s.get("end") or {}).get("timestamp")
            hit_s   = (s.get("ball_hit") or {}).get("timestamp")

            # choose a target time to locate the rally (ball_hit_s preferred, else mid)
            probe_s = hit_s if hit_s is not None else ((start_s + end_s)/2.0 if (start_s is not None and end_s is not None) else start_s)
            rally_id = find_rally_id_for_time(rallies_index, probe_s)

            # ball_hit_location -> x,y
            bh_loc = s.get("ball_hit_location") or [None, None]
            bh_x = bh_loc[0] if isinstance(bh_loc, (list, tuple)) and len(bh_loc) >= 2 else None
            bh_y = bh_loc[1] if isinstance(bh_loc, (list, tuple)) and len(bh_loc) >= 2 else None

            # ball_impact_location -> not used yet, but table has x,y + type
            bi_loc = s.get("ball_impact_location") or [None, None]
            bi_x = bi_loc[0] if isinstance(bi_loc, (list, tuple)) and len(bi_loc) >= 2 else None
            bi_y = bi_loc[1] if isinstance(bi_loc, (list, tuple)) and len(bi_loc) >= 2 else None

            swing_rows.append({
                "session_id": session_id,
                "rally_id": rally_id,
                "player_id": pid,
                "start_ts": seconds_to_ts(base_dt, start_s),
                "end_ts":   seconds_to_ts(base_dt, end_s),
                "ball_hit_ts": seconds_to_ts(base_dt, hit_s),
                "start_s": start_s,
                "end_s": end_s,
                "ball_hit_s": hit_s,
                "start_frame": (s.get("start") or {}).get("frame_nr"),
                "end_frame": (s.get("end") or {}).get("frame_nr"),
                "ball_hit_frame": (s.get("ball_hit") or {}).get("frame_nr"),
                "swing_type": s.get("swing_type"),
                "serve": bool(s.get("serve")),
                "volley": s.get("volley"),
                "is_in_rally": s.get("is_in_rally"),
                "confidence": s.get("confidence"),
                "confidence_swing_type": s.get("confidence_swing_type"),
                "confidence_volley": s.get("confidence_volley"),
                "rally_start_s": (s.get("rally") or [None, None])[0],
                "rally_end_s":   (s.get("rally") or [None, None])[1],
                "ball_hit_x": bh_x,
                "ball_hit_y": bh_y,
                "ball_player_distance": s.get("ball_player_distance"),
                "ball_speed": s.get("ball_speed"),
                "ball_impact_location_x": bi_x,
                "ball_impact_location_y": bi_y,
                "ball_impact_type": s.get("ball_impact_type"),
                "intercepting_player_uid": str(s.get("intercepting_player_id")) if s.get("intercepting_player_id") is not None else None,
                "ball_trajectory": json.dumps(s.get("ball_trajectory")) if s.get("ball_trajectory") is not None else None,
                "annotations_json": json.dumps(s.get("annotations")) if s.get("annotations") is not None else None,
            })
    if swing_rows:
        insert_fact_swing_batch_v2(swing_rows)

    # --- Ball bounces
    bounce_rows = []
    for b in data.get("ball_bounces") or []:
        ts_s = b.get("timestamp")
        cp = b.get("court_pos") or [None, None]
        bx = cp[0] if isinstance(cp, (list, tuple)) and len(cp) >= 2 else None
        by = cp[1] if isinstance(cp, (list, tuple)) and len(cp) >= 2 else None
        rally_id = find_rally_id_for_time(rallies_index, ts_s)
        bounce_rows.append({
            "session_id": session_id,
            "rally_id": rally_id,
            "timestamp_s": ts_s,
            "bounce_ts": seconds_to_ts(base_dt, ts_s),
            "bounce_x": bx,
            "bounce_y": by,
            "hitter_player_id": player_map_uid_to_id.get(str(b.get("player_id"))),
            "bounce_type": b.get("type")
        })
    if bounce_rows:
        insert_fact_bounce_batch_v2(bounce_rows)

    # --- Ball positions
    insert_ball_positions(session_id, data.get("ball_positions") or [], base_dt)

    # --- Player positions
    insert_player_positions(session_id, player_map_uid_to_id, data.get("player_positions") or {}, base_dt)

    # --- Team sessions
    insert_team_sessions(session_id, data.get("team_sessions") or [])

    # --- Highlights
    insert_highlights(session_id, data.get("highlights") or [])

    # --- Heatmap & confidences & thumbnails
    upsert_bounce_heatmap(session_id, data.get("bounce_heatmap"))
    upsert_session_confidences(session_id, data.get("confidences"))
    insert_thumbnails(session_id, data.get("thumbnail_crops") or {})

    # --- Save raw payload for debug
    upsert_raw_payload(session_id, data)

    print(f"✅ v2 ingest complete | session_id={session_id} swings={len(swing_rows)} bounces={len(bounce_rows)}")

# =======================
# Entrypoint
# =======================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
