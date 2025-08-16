from flask import Flask, request, jsonify, render_template
import requests
import os
import json
from datetime import datetime, timezone, timedelta
from threading import Thread
from werkzeug.utils import secure_filename
from sqlalchemy import create_engine, text
from db_init import init_db

APP_VERSION = "sportai-ingest-2.0-full"

app = Flask(__name__)

# =======================
# Environment
# =======================
SPORT_AI_TOKEN = os.environ.get("SPORT_AI_TOKEN")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")
DATABASE_URL = os.environ.get("DATABASE_URL")
OPS_KEY = os.environ.get("OPS_KEY")

# =======================
# DB engine
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
    print("⚠️ DATABASE_URL is not set. DB features disabled.")

# =======================
# Helpers
# =======================
def _g(d, k, default=None):
    return d.get(k, default) if isinstance(d, dict) else default

def _is_list(x): return isinstance(x, list)
def _is_dict(x): return isinstance(x, dict)

def _to_float(v):
    try:
        return float(v)
    except Exception:
        return None

def _to_int(v):
    try:
        return int(v)
    except Exception:
        return None

def _ts_from_seconds(base_ts: datetime, seconds):
    if seconds is None:
        return None
    try:
        return (base_ts + timedelta(seconds=float(seconds))).astimezone(timezone.utc)
    except Exception:
        return None

def _from_epoch_like(v):
    if v is None: return None
    try:
        x = float(v)
        if x > 1e12:   # ms
            return datetime.fromtimestamp(x/1000.0, tz=timezone.utc)
        if x > 1e9:    # s
            return datetime.fromtimestamp(x, tz=timezone.utc)
    except Exception:
        return None
    return None

def _parse_iso(v):
    if not v: return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def _xy_from_array(a):
    if _is_list(a) and len(a) >= 2:
        return _to_float(a[0]), _to_float(a[1])
    return None, None

# =======================
# Dropbox OAuth
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
# Routes
# =======================
@app.get("/")
def index():
    return render_template("upload.html")

@app.get("/ops/version")
def ops_version():
    return {"ok": True, "version": APP_VERSION}

@app.get("/ops/init-db")
def ops_init_db():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    try:
        status = init_db()
        return {"ok": True, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.get("/ops/init-views")
def ops_init_views():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    try:
        _create_views()
        return {"ok": True, "status": "views ready"}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.get("/ops/db-counts")
def db_counts():
    if engine is None:
        return {"ok": False, "error": "No engine"}, 500
    tables = ["dim_session","dim_player","dim_rally","fact_bounce","fact_swing",
              "fact_ball_position","fact_player_position","team_session","highlight",
              "bounce_heatmap","session_confidences","thumbnail","raw_result"]
    try:
        out = {}
        with engine.connect() as conn:
            for t in tables:
                out[t] = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        return {"ok": True, "counts": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.get("/ops/sql")
def ops_sql_get():
    if request.args.get("key") != OPS_KEY:
        return jsonify({"error": "unauthorized"}), 403
    q = (request.args.get("q") or "").strip()
    if not q.lower().startswith(("select","with")) or ";" in q:
        return jsonify({"error":"only single SELECT/WITH queries without ';' are allowed"}), 400
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(q)).mappings().all()
        return jsonify({"ok": True, "rows": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.get("/ops/peek-json")
def peek_json():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    name = request.args.get("name")
    if not name:
        return ("Missing ?name=", 400)
    p = os.path.join(os.getcwd(), "data", name)
    if not os.path.isfile(p):
        return (f"File not found: {p}", 404)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        # return top keys & small shape
        top = data if isinstance(data, dict) else {"type": type(data).__name__}
        out = {"top_level_keys": list(top.keys()) if isinstance(top, dict) else []}
        for k in ["players","team_sessions","rallies","highlights","bounce_heatmap","ball_bounces",
                  "ball_positions","player_positions","confidences","thumbnail_crops"]:
            v = top.get(k)
            if isinstance(v, list):
                out[k] = {"len": len(v), "sample_item_keys": list(v[0].keys()) if v and isinstance(v[0], dict) else ["<unknown>"]}
            elif isinstance(v, dict):
                out[k] = {"type": "dict", "keys": list(v.keys())[:20]}
            elif v is not None:
                out[k] = {"type": type(v).__name__}
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/ops/ingest-file")
def ops_ingest_file():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    name = request.args.get("name")
    if not name:
        return ("Missing ?name=<file.json>", 400)
    path = os.path.join(os.getcwd(), "data", name)
    if not os.path.isfile(path):
        return (f"File not found: {path}", 404)
    try:
        ingest_sportai_json(path, name)
        return {"ok": True, "ingested": name}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.post("/ops/upload-json")
def ops_upload_json():
    key = request.args.get("key")
    if not OPS_KEY or key != OPS_KEY:
        return ("Forbidden", 403)
    name = request.args.get("name")
    if not name or not name.endswith(".json"):
        return ("Missing or invalid ?name=<file.json>", 400)
    os.makedirs("data", exist_ok=True)
    dest = os.path.join(os.getcwd(), "data", secure_filename(name))
    try:
        if "file" in request.files:
            request.files["file"].save(dest)
        else:
            if not request.data:
                return ("No body provided", 400)
            with open(dest, "wb") as out:
                out.write(request.data)
        ingest_sportai_json(dest, name)
        return {"ok": True, "saved": name, "ingested": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# =======================
# Upload -> Dropbox -> SportAI
# =======================
@app.post("/upload")
def upload():
    if 'video' not in request.files or 'email' not in request.form:
        return jsonify({"error":"Video and email are required"}), 400

    email = request.form['email'].strip().replace("@", "_at_").replace(".", "_")
    video = request.files['video']
    file_name = video.filename
    file_bytes = video.read()
    dropbox_path = f"/wix-uploads/{file_name}"

    token = get_dropbox_access_token()
    if not token:
        return jsonify({"error":"Dropbox token refresh failed"}), 500

    up = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({"path": dropbox_path,"mode":"add","autorename":True,"mute":False}),
            "Content-Type": "application/octet-stream"
        },
        data=file_bytes
    )
    if not up.ok:
        return jsonify({"error": "Dropbox upload failed", "details": up.text}), 500

    link = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={"Authorization": f"Bearer {token}","Content-Type":"application/json"},
        json={"path": dropbox_path,"settings":{"requested_visibility":"public"}}
    )
    if link.status_code not in [200,201,202]:
        err = link.json()
        if _g(err,'error',{}).get('.tag')=='shared_link_already_exists':
            link_data = requests.post(
                "https://api.dropboxapi.com/2/sharing/list_shared_links",
                headers={"Authorization": f"Bearer {token}","Content-Type":"application/json"},
                json={"path": dropbox_path,"direct_only":True}
            ).json()
            raw_url = link_data['links'][0]['url']
        else:
            return jsonify({"error":"Failed to generate Dropbox link"}), 500
    else:
        raw_url = link.json()['url']

    raw_url = raw_url.replace("dl=0", "raw=1").replace("www.dropbox.com","dl.dropboxusercontent.com")

    payload = {"video_url": raw_url, "only_in_rally_data": False, "version": "stable"}
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}","Content-Type":"application/json"}
    res = requests.post("https://api.sportai.com/api/statistics", json=payload, headers=headers)
    if res.status_code not in [200,201,202]:
        return jsonify({"error":"Failed to register task", "details": res.text}), 500

    task_id = res.json()["data"]["task_id"]
    Thread(target=_poll_and_fetch, args=(task_id,email), daemon=True).start()
    return {"ok": True, "task_id": task_id, "dropbox_url": raw_url}

def _poll_and_fetch(task_id, email):
    url = f"https://api.sportai.com/api/statistics/{task_id}/status"
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
    for _ in range(720):  # ~6h
        r = requests.get(url, headers=headers)
        if r.status_code==200 and _g(_g(r.json(),"data",{}),"status")=="completed":
            for _ in range(15):
                if _fetch_result(task_id, email):
                    return
                import time; time.sleep(10)
            return
        import time; time.sleep(30)

def _fetch_result(task_id, email):
    meta = requests.get(f"https://api.sportai.com/api/statistics/{task_id}",
                        headers={"Authorization": f"Bearer {SPORT_AI_TOKEN}"})
    if meta.status_code not in [200,201]: return False
    result_url = _g(_g(meta.json(),"data",{}),"result_url")
    if not result_url: return False

    res = requests.get(result_url)
    if res.status_code != 200: return False

    os.makedirs("data", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    local = f"data/sportai-{task_id}-{email}-{ts}.json"
    with open(local, "w", encoding="utf-8") as f:
        f.write(res.text)

    try:
        ingest_sportai_json(local, os.path.basename(local))
        print("✅ JSON ingested:", local)
    except Exception as e:
        print("❌ Ingestion error:", str(e))

    return True

# =======================
# DB Upserts / Inserts
# =======================
def upsert_dim_session(*, session_uid, source_file=None, session_date=None, fps=None, court_surface=None, venue=None):
    sql = text("""
        INSERT INTO dim_session (session_uid, source_file, session_date, fps, court_surface, venue)
        VALUES (:uid, :src, :dt, :fps, :surf, :venue)
        ON CONFLICT (session_uid) DO UPDATE SET
          source_file   = EXCLUDED.source_file,
          session_date  = EXCLUDED.session_date,
          fps           = EXCLUDED.fps,
          court_surface = EXCLUDED.court_surface,
          venue         = EXCLUDED.venue
        RETURNING session_id;
    """)
    with engine.begin() as conn:
        sid = conn.execute(sql, dict(uid=session_uid, src=source_file, dt=session_date, fps=fps,
                                     surf=court_surface, venue=venue)).scalar_one()
    return sid

def upsert_dim_player(*, session_id, sportai_player_uid, full_name=None, handedness=None, age=None, utr=None,
                      covered_distance=None, fastest_sprint=None, fastest_sprint_timestamp_s=None,
                      activity_score=None, swing_type_distribution=None, location_heatmap=None):
    sql = text("""
        INSERT INTO dim_player
          (session_id, sportai_player_uid, full_name, handedness, age, utr,
           covered_distance, fastest_sprint, fastest_sprint_timestamp_s, activity_score,
           swing_type_distribution, location_heatmap)
        VALUES
          (:sid, :uid, :name, :hand, :age, :utr,
           :cov, :fst, :fst_ts, :act,
           :dist, :heat)
        ON CONFLICT (session_id, sportai_player_uid) DO UPDATE SET
          full_name  = COALESCE(EXCLUDED.full_name, dim_player.full_name),
          handedness = COALESCE(EXCLUDED.hand, dim_player.handedness),
          age        = COALESCE(EXCLUDED.age, dim_player.age),
          utr        = COALESCE(EXCLUDED.utr, dim_player.utr),
          covered_distance = COALESCE(EXCLUDED.cov, dim_player.covered_distance),
          fastest_sprint = COALESCE(EXCLUDED.fst, dim_player.fastest_sprint),
          fastest_sprint_timestamp_s = COALESCE(EXCLUDED.fst_ts, dim_player.fastest_sprint_timestamp_s),
          activity_score = COALESCE(EXCLUDED.act, dim_player.activity_score),
          swing_type_distribution = COALESCE(EXCLUDED.dist, dim_player.swing_type_distribution),
          location_heatmap = COALESCE(EXCLUDED.heat, dim_player.location_heatmap)
        RETURNING player_id;
    """)
    with engine.begin() as conn:
        pid = conn.execute(sql, dict(
            sid=session_id, uid=str(sportai_player_uid), name=full_name, hand=handedness, age=age, utr=utr,
            cov=covered_distance, fst=fastest_sprint, fst_ts=fastest_sprint_timestamp_s,
            act=activity_score, dist=json.dumps(swing_type_distribution) if swing_type_distribution is not None else None,
            heat=json.dumps(location_heatmap) if location_heatmap is not None else None
        )).scalar_one()
    return pid

def upsert_dim_rally(*, session_id, rally_number, start_ts=None, end_ts=None, length_shots=None, point_winner_player_id=None):
    sql = text("""
        INSERT INTO dim_rally (session_id, rally_number, start_ts, end_ts, length_shots, point_winner_player_id)
        VALUES (:sid, :num, :st, :en, :len, :win)
        ON CONFLICT (session_id, rally_number) DO UPDATE SET
          start_ts = EXCLUDED.start_ts,
          end_ts   = EXCLUDED.end_ts,
          length_shots = COALESCE(EXCLUDED.length_shots, dim_rally.length_shots),
          point_winner_player_id = COALESCE(EXCLUDED.point_winner_player_id, dim_rally.point_winner_player_id)
        RETURNING rally_id;
    """)
    with engine.begin() as conn:
        rid = conn.execute(sql, dict(sid=session_id, num=rally_number, st=start_ts, en=end_ts,
                                     len=length_shots, win=point_winner_player_id)).scalar_one()
    return rid

def insert_many(sql, rows):
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(text(sql), rows)
    return len(rows)

# =======================
# Ingestion pipeline (Bronze + Silver)
# =======================
def ingest_sportai_json(json_path, source_file_name):
    if engine is None:
        print("⚠️ No DB engine. Skipping ingest.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # -------- session base & fps
    # SportAI timestamps are seconds from start of video. We'll anchor absolute time to epoch(UTC)
    # unless we learn an absolute time later.
    session_base = datetime(1970,1,1,tzinfo=timezone.utc)
    fps = None
    if isinstance(data, dict):
        fps = data.get("fps") or _g(_g(data, "video", {}), "fps")

    session_uid = str(_g(data, "id") or source_file_name)
    session_id = upsert_dim_session(session_uid=session_uid, source_file=source_file_name,
                                    session_date=session_base, fps=_to_float(fps),
                                    court_surface=None, venue=None)

    # Save raw JSON (bronze)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO raw_result (session_id, payload) VALUES (:sid, :p)
            ON CONFLICT (session_id) DO UPDATE SET payload = EXCLUDED.payload
        """), {"sid": session_id, "p": json.dumps(data)})

    # Clear existing silver records for this session (idempotent re-ingest)
    with engine.begin() as conn:
        for t in ["fact_swing","fact_bounce","fact_ball_position","fact_player_position",
                  "team_session","highlight","bounce_heatmap","session_confidences",
                  "thumbnail"]:
            conn.execute(text(f"DELETE FROM {t} WHERE session_id = :sid"), {"sid": session_id})
        conn.execute(text("DELETE FROM dim_rally WHERE session_id = :sid"), {"sid": session_id})
        conn.execute(text("DELETE FROM dim_player WHERE session_id = :sid"), {"sid": session_id})

    # -------- players (and swings inside players)
    player_id_map = {}  # sportai uid -> PK
    players = data.get("players") or []
    if _is_list(players):
        for p in players:
            if not _is_dict(p): continue
            uid = p.get("player_id")
            pid = upsert_dim_player(
                session_id=session_id,
                sportai_player_uid=uid,
                full_name=p.get("full_name") or p.get("name"),
                handedness=None, age=None, utr=None,
                covered_distance=_to_float(p.get("covered_distance")),
                fastest_sprint=_to_float(p.get("fastest_sprint")),
                fastest_sprint_timestamp_s=_to_float(p.get("fastest_sprint_timestamp")),
                activity_score=_to_float(p.get("activity_score")),
                swing_type_distribution=p.get("swing_type_distribution"),
                location_heatmap=p.get("location_heatmap"),
            )
            player_id_map[str(uid)] = pid
    else:
        players = []

    # -------- rallies: float[][] seconds
    rally_id_map = {}  # rally_index(1-based) -> rally_id
    rallies = data.get("rallies") or []
    if _is_list(rallies):
        for idx, rr in enumerate(rallies, start=1):
            if not _is_list(rr) or len(rr) < 2: continue
            st_s = _to_float(rr[0]); en_s = _to_float(rr[1])
            st_ts = _ts_from_seconds(session_base, st_s)
            en_ts = _ts_from_seconds(session_base, en_s)
            rid = upsert_dim_rally(session_id=session_id, rally_number=idx,
                                   start_ts=st_ts, end_ts=en_ts, length_shots=None, point_winner_player_id=None)
            rally_id_map[idx] = rid

    # -------- swings: nested per player per docs
    swing_rows = []
    for p in players:
        if not _is_dict(p): continue
        uid = str(p.get("player_id"))
        pid = player_id_map.get(uid)
        swings = p.get("swings") or []
        if not _is_list(swings): continue
        for s in swings:
            if not _is_dict(s): continue
            # times/frames
            start_s = _to_float(_g(s,"start",{}).get("timestamp"))
            end_s   = _to_float(_g(s,"end",{}).get("timestamp"))
            hit_s   = _to_float(_g(s,"ball_hit",{}).get("timestamp"))
            start_frame = _to_int(_g(s,"start",{}).get("frame_nr"))
            end_frame   = _to_int(_g(s,"end",{}).get("frame_nr"))
            hit_frame   = _to_int(_g(s,"ball_hit",{}).get("frame_nr"))

            # absolute timestamps
            start_ts = _ts_from_seconds(session_base, start_s)
            end_ts   = _ts_from_seconds(session_base, end_s)
            hit_ts   = _ts_from_seconds(session_base, hit_s)

            # rally window on swing (seconds array)
            r = s.get("rally") or [None, None]
            r0 = _to_float(r[0]) if len(r)>=1 else None
            r1 = _to_float(r[1]) if len(r)>=2 else None

            # attempt to assign rally_id by nearest window mid
            rally_id = None
            if r0 is not None and r1 is not None and rally_id_map:
                mid = (r0 + r1) / 2.0
                best = None; best_idx=None
                for idx, rid in rally_id_map.items():
                    # use stored start/end from dim_rally
                    best = best or float("inf")
                    # we need rally seconds to compare; approximate from ordering
                    # choose nearest index by start time proximity:
                    diff = abs(idx - 1 - mid)  # crude; we’ll fallback to time based if available
                # Better approach: fetch windows and compare time (we have absolute in DB)
            # Use DB windows to match by absolute time (prefer hit_ts, else start_ts)
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT rally_id, start_ts, end_ts
                    FROM dim_rally WHERE session_id = :sid
                """), {"sid": session_id}).mappings().all()
            tref = hit_ts or start_ts or end_ts
            if tref and rows:
                best = None; best_rid=None
                for row in rows:
                    st = row["start_ts"]; en=row["end_ts"]
                    if st and en and st <= tref <= en:
                        best_rid = row["rally_id"]; best = 0; break
                    mid = st + (en-st)/2 if st and en else None
                    if mid:
                        d = abs((tref - mid).total_seconds())
                        if best is None or d < best:
                            best = d; best_rid = row["rally_id"]
                rally_id = best_rid

            # hit location
            bhx, bhy = _xy_from_array(s.get("ball_hit_location"))
            i_loc = s.get("ball_impact_location")  # not used yet: could be [x,y] or null
            i_x, i_y = _xy_from_array(i_loc) if i_loc else (None, None)

            swing_rows.append({
                "session_id": session_id,
                "rally_id": rally_id,
                "player_id": pid,
                "start_ts": start_ts, "end_ts": end_ts, "ball_hit_ts": hit_ts,
                "start_s": start_s, "end_s": end_s, "ball_hit_s": hit_s,
                "start_frame": start_frame, "end_frame": end_frame, "ball_hit_frame": hit_frame,
                "swing_type": s.get("swing_type"),
                "serve": bool(s.get("serve")),
                "volley": bool(s.get("volley")),
                "is_in_rally": bool(s.get("is_in_rally")),
                "confidence": _to_float(s.get("confidence")),
                "confidence_swing_type": _to_float(s.get("confidence_swing_type")),
                "confidence_volley": _to_float(s.get("confidence_volley")),
                "rally_start_s": r0, "rally_end_s": r1,
                "ball_hit_x": bhx, "ball_hit_y": bhy,
                "ball_player_distance": _to_float(s.get("ball_player_distance")),
                "ball_speed": _to_float(s.get("ball_speed")),
                "ball_impact_location_x": i_x, "ball_impact_location_y": i_y,
                "ball_impact_type": s.get("ball_impact_type"),
                "intercepting_player_uid": str(s.get("intercepting_player_id")) if s.get("intercepting_player_id") is not None else None,
                "ball_trajectory": json.dumps(s.get("ball_trajectory")) if s.get("ball_trajectory") is not None else None,
                "annotations_json": json.dumps(s.get("annotations")) if s.get("annotations") is not None else None
            })

    if swing_rows:
        insert_many("""
            INSERT INTO fact_swing (
              session_id, rally_id, player_id, start_ts, end_ts, ball_hit_ts,
              start_s, end_s, ball_hit_s, start_frame, end_frame, ball_hit_frame,
              swing_type, serve, volley, is_in_rally,
              confidence, confidence_swing_type, confidence_volley,
              rally_start_s, rally_end_s,
              ball_hit_x, ball_hit_y, ball_player_distance, ball_speed,
              ball_impact_location_x, ball_impact_location_y, ball_impact_type,
              intercepting_player_uid, ball_trajectory, annotations_json
            ) VALUES (
              :session_id,:rally_id,:player_id,:start_ts,:end_ts,:ball_hit_ts,
              :start_s,:end_s,:ball_hit_s,:start_frame,:end_frame,:ball_hit_frame,
              :swing_type,:serve,:volley,:is_in_rally,
              :confidence,:confidence_swing_type,:confidence_volley,
              :rally_start_s,:rally_end_s,
              :ball_hit_x,:ball_hit_y,:ball_player_distance,:ball_speed,
              :ball_impact_location_x,:ball_impact_location_y,:ball_impact_type,
              :intercepting_player_uid, CAST(:ball_trajectory AS JSONB), CAST(:annotations_json AS JSONB)
            )
        """, swing_rows)

    # -------- ball_bounces (array of objects)
    bounces = data.get("ball_bounces") or []
    bounce_rows = []
    if _is_list(bounces):
        # We’ll map a bounce into the rally whose window contains the derived absolute ts
        rally_windows = []
        with engine.connect() as conn:
            for r in conn.execute(text("SELECT rally_id, start_ts, end_ts FROM dim_rally WHERE session_id=:sid"),
                                  {"sid": session_id}).mappings():
                rally_windows.append((r["rally_id"], r["start_ts"], r["end_ts"]))
        for b in bounces:
            if not _is_dict(b): continue
            t_s = _to_float(b.get("timestamp"))
            b_ts = _ts_from_seconds(session_base, t_s)
            bx, by = _xy_from_array(b.get("court_pos"))
            hitter_uid = b.get("player_id")
            hitter_pid = player_id_map.get(str(hitter_uid))
            rmatch = None
            if b_ts and rally_windows:
                best = None; best_rid=None
                for rid, st, en in rally_windows:
                    if st and en and st <= b_ts <= en: best_rid = rid; best=0; break
                    if st and en:
                        mid = st + (en-st)/2
                        d = abs((b_ts - mid).total_seconds())
                        if best is None or d < best:
                            best = d; best_rid = rid
                rmatch = best_rid
            bounce_rows.append({
                "session_id": session_id, "rally_id": rmatch,
                "timestamp_s": t_s, "bounce_ts": b_ts,
                "bounce_x": bx, "bounce_y": by,
                "hitter_player_id": hitter_pid,
                "bounce_type": b.get("type")
            })
    if bounce_rows:
        insert_many("""
            INSERT INTO fact_bounce (session_id, rally_id, timestamp_s, bounce_ts, bounce_x, bounce_y, hitter_player_id, bounce_type)
            VALUES (:session_id,:rally_id,:timestamp_s,:bounce_ts,:bounce_x,:bounce_y,:hitter_player_id,:bounce_type)
        """, bounce_rows)

    # -------- ball_positions (object[])
    ball_positions = data.get("ball_positions") or []
    bp_rows = []
    if _is_list(ball_positions):
        for bp in ball_positions:
            if not _is_dict(bp): continue
            t_s = _to_float(bp.get("timestamp"))
            ts = _ts_from_seconds(session_base, t_s)
            bp_rows.append({
                "session_id": session_id, "timestamp_s": t_s, "ts": ts,
                "x_image": _to_float(bp.get("X")), "y_image": _to_float(bp.get("Y"))
            })
    if bp_rows:
        insert_many("""
            INSERT INTO fact_ball_position (session_id, timestamp_s, ts, x_image, y_image)
            VALUES (:session_id,:timestamp_s,:ts,:x_image,:y_image)
        """, bp_rows)

    # -------- player_positions (dict of arrays)
    player_positions = data.get("player_positions") or {}
    pp_rows = []
    if _is_dict(player_positions):
        for uid, arr in player_positions.items():
            pid = player_id_map.get(str(uid))
            if not pid:  # ensure player exists
                pid = upsert_dim_player(session_id=session_id, sportai_player_uid=uid)
                player_id_map[str(uid)] = pid
            if _is_list(arr):
                for item in arr:
                    if not _is_dict(item): continue
                    t_s = _to_float(item.get("timestamp"))
                    ts = _ts_from_seconds(session_base, t_s)
                    pp_rows.append({
                        "session_id": session_id, "player_id": pid,
                        "timestamp_s": t_s, "ts": ts,
                        "img_x": _to_float(item.get("X")), "img_y": _to_float(item.get("Y")),
                        "court_x": _to_float(item.get("court_X")), "court_y": _to_float(item.get("court_Y"))
                    })
    if pp_rows:
        insert_many("""
            INSERT INTO fact_player_position (session_id, player_id, timestamp_s, ts, img_x, img_y, court_x, court_y)
            VALUES (:session_id,:player_id,:timestamp_s,:ts,:img_x,:img_y,:court_x,:court_y)
        """, pp_rows)

    # -------- team_sessions (array)
    team_sessions = data.get("team_sessions") or []
    ts_rows = []
    if _is_list(team_sessions):
        for t in team_sessions:
            if not _is_dict(t): continue
            ts_rows.append({
                "session_id": session_id,
                "start_s": _to_float(t.get("start_time")),
                "end_s": _to_float(t.get("end_time")),
                "front_team": t.get("team_front") or t.get("front_team") or [],
                "back_team": t.get("team_back") or t.get("back_team") or []
            })
    if ts_rows:
        insert_many("""
            INSERT INTO team_session (session_id, start_s, end_s, front_team, back_team)
            VALUES (:session_id,:start_s,:end_s,:front_team,:back_team)
        """, ts_rows)

    # -------- highlights (array)
    highlights = data.get("highlights") or []
    hl_rows = []
    if _is_list(highlights):
        for h in highlights:
            if not _is_dict(h): continue
            start_s = _to_float(_g(h,"start",{}).get("timestamp"))
            end_s   = _to_float(_g(h,"end",{}).get("timestamp"))
            hl_rows.append({
                "session_id": session_id,
                "type": h.get("type"),
                "start_s": start_s,
                "end_s": end_s,
                "duration": _to_float(h.get("duration")),
                "swing_count": _to_int(h.get("swing_count")),
                "ball_speed": _to_float(h.get("ball_speed")),
                "ball_distance": _to_float(h.get("ball_distance")),
                "players_distance": _to_float(h.get("players_distance")),
                "players_speed": _to_float(h.get("players_speed")),
                "dynamic_score": _to_float(h.get("dynamic_score")),
                "players_json": json.dumps(h.get("players")) if h.get("players") is not None else None
            })
    if hl_rows:
        insert_many("""
            INSERT INTO highlight (session_id, type, start_s, end_s, duration, swing_count,
                                   ball_speed, ball_distance, players_distance, players_speed,
                                   dynamic_score, players_json)
            VALUES (:session_id,:type,:start_s,:end_s,:duration,:swing_count,
                    :ball_speed,:ball_distance,:players_distance,:players_speed,
                    :dynamic_score, CAST(:players_json AS JSONB))
        """, hl_rows)

    # -------- bounce_heatmap (matrix)
    heatmap = data.get("bounce_heatmap")
    if heatmap is not None:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO bounce_heatmap (session_id, heatmap) VALUES (:sid, :h)
                ON CONFLICT (session_id) DO UPDATE SET heatmap = EXCLUDED.heatmap
            """), {"sid": session_id, "h": json.dumps(heatmap)})

    # -------- confidences (object)
    conf = data.get("confidences") or {}
    if _is_dict(conf):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO session_confidences
                  (session_id, pose, swing, swing_ball, ball, final,
                   pose_confidences, swing_confidences, ball_confidences)
                VALUES
                  (:sid, :pose, :swing, :swing_ball, :ball, :final,
                   :pose_conf, :swing_conf, :ball_conf)
                ON CONFLICT (session_id) DO UPDATE SET
                  pose = EXCLUDED.pose,
                  swing = EXCLUDED.swing,
                  swing_ball = EXCLUDED.swing_ball,
                  ball = EXCLUDED.ball,
                  final = EXCLUDED.final,
                  pose_confidences = EXCLUDED.pose_confidences,
                  swing_confidences = EXCLUDED.swing_confidences,
                  ball_confidences = EXCLUDED.ball_confidences
            """), {
                "sid": session_id,
                "pose": _to_float(_g(conf,"final_confidences",{}).get("pose")),
                "swing": _to_float(_g(conf,"final_confidences",{}).get("swing")),
                "swing_ball": _to_float(_g(conf,"final_confidences",{}).get("swing_ball")),
                "ball": _to_float(_g(conf,"final_confidences",{}).get("ball")),
                "final": _to_float(_g(conf,"final_confidences",{}).get("final")),
                "pose_conf": json.dumps(conf.get("pose_confidences")) if conf.get("pose_confidences") is not None else None,
                "swing_conf": json.dumps(conf.get("swing_confidences")) if conf.get("swing_confidences") is not None else None,
                "ball_conf": json.dumps(conf.get("ball_confidences")) if conf.get("ball_confidences") is not None else None
            })

    # -------- thumbnails (object mapping player uid -> array)
    thumbs = data.get("thumbnail_crops") or {}
    t_rows = []
    if _is_dict(thumbs):
        for uid, arr in thumbs.items():
            if _is_list(arr):
                for it in arr:
                    if not _is_dict(it): continue
                    t_rows.append({
                        "session_id": session_id,
                        "player_uid": str(uid),
                        "frame_nr": _to_int(it.get("frame_nr")),
                        "timestamp_s": _to_float(it.get("timestamp")),
                        "score": _to_float(it.get("score")),
                        "bbox": json.dumps(it.get("bbox")) if it.get("bbox") is not None else None
                    })
    if t_rows:
        insert_many("""
            INSERT INTO thumbnail (session_id, player_uid, frame_nr, timestamp_s, score, bbox)
            VALUES (:session_id,:player_uid,:frame_nr,:timestamp_s,:score, CAST(:bbox AS JSONB))
        """, t_rows)

    print(f"✅ Ingest complete (session_id={session_id}, file={source_file_name})")

# =======================
# Views (Gold)
# =======================
def _create_views():
    stmts = [
        # bounces with rally number
        """
        CREATE OR REPLACE VIEW vw_bounce AS
        SELECT b.session_id, b.rally_id, r.rally_number, b.bounce_ts, b.timestamp_s,
               b.bounce_x, b.bounce_y, b.bounce_type, b.hitter_player_id
        FROM fact_bounce b
        LEFT JOIN dim_rally r USING (rally_id);
        """,
        # rally summary
        """
        CREATE OR REPLACE VIEW vw_rally_summary AS
        SELECT r.session_id, r.rally_id, r.rally_number,
               r.start_ts, r.end_ts,
               COUNT(b.*) AS bounce_count
        FROM dim_rally r
        LEFT JOIN fact_bounce b ON b.rally_id = r.rally_id
        GROUP BY 1,2,3,4,5;
        """,
        # swing enriched
        """
        CREATE OR REPLACE VIEW vw_swing AS
        SELECT s.session_id, s.rally_id, r.rally_number, s.player_id,
               s.start_ts, s.ball_hit_ts, s.end_ts,
               s.swing_type, s.serve, s.volley, s.is_in_rally,
               s.confidence, s.confidence_swing_type, s.confidence_volley,
               s.ball_hit_x, s.ball_hit_y, s.ball_speed, s.ball_player_distance
        FROM fact_swing s
        LEFT JOIN dim_rally r USING (rally_id);
        """,
        # player metrics
        """
        CREATE OR REPLACE VIEW vw_player_metrics AS
        SELECT p.session_id, p.player_id, p.sportai_player_uid, p.full_name,
               p.covered_distance, p.fastest_sprint, p.fastest_sprint_timestamp_s,
               p.activity_score
        FROM dim_player p;
        """,
        # ball positions
        """
        CREATE OR REPLACE VIEW vw_ball_positions AS
        SELECT session_id, ts, timestamp_s, x_image, y_image
        FROM fact_ball_position;
        """,
        # player positions
        """
        CREATE OR REPLACE VIEW vw_player_positions AS
        SELECT session_id, player_id, ts, timestamp_s, img_x, img_y, court_x, court_y
        FROM fact_player_position;
        """,
        # highlights
        """
        CREATE OR REPLACE VIEW vw_highlights AS
        SELECT session_id, type, start_s, end_s, duration, swing_count,
               ball_speed, ball_distance, players_distance, players_speed, dynamic_score
        FROM highlight;
        """,
        # team sessions
        """
        CREATE OR REPLACE VIEW vw_team_sessions AS
        SELECT session_id, start_s, end_s, front_team, back_team
        FROM team_session;
        """
    ]
    with engine.begin() as conn:
        for s in stmts:
            conn.execute(text(s))

# =======================
# Entrypoint
# =======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
