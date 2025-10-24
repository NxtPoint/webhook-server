# ingest_bronze.py — greenfield strict ingest to bronze schema
import os, json, gzip, hashlib, re
from datetime import datetime, timezone, timedelta
from typing import Dict

import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from db_init import engine  # reuse existing engine

ingest_bronze = Blueprint("ingest_bronze", __name__)

OPS_KEY = os.getenv("OPS_KEY", "")

def _guard() -> bool:
    from flask import request
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

def _forbid():
    return Response("Forbidden", 403)

# ---------- small utils ----------
def _float(v):
    try: return float(v)
    except Exception:
        try: return float(str(v))
        except Exception: return None

def _bool(v):
    if v is None: return None
    if isinstance(v, bool): return v
    s = str(v).strip().lower()
    if s in ("1","true","t","yes","y"): return True
    if s in ("0","false","f","no","n"):  return False
    return None

def _time_s(val):
    if val is None: return None
    if isinstance(val, (int, float, str)): return _float(val)
    if isinstance(val, dict):
        for k in ("timestamp","timestamp_s","ts","time_s","t","seconds","s"):
            if k in val: return _float(val[k])
    return None

def seconds_to_ts(base_dt: datetime, s):
    if s is None: return None
    try: return base_dt + timedelta(seconds=float(s))
    except Exception: return None

def _base_dt_for_session(dt):
    return dt if dt else datetime(1970,1,1,tzinfo=timezone.utc)

# ---------- path helper for unmatched ----------
_ARR = re.compile(r"\[\d+\]")
def _iter_paths(obj, base=""):
    if isinstance(obj, dict):
        for k,v in obj.items():
            p = f"{base}.{k}" if base else k
            yield from _iter_paths(v, p)
    elif isinstance(obj, list):
        for it in obj:
            p = f"{base}[*]"
            yield from _iter_paths(it, p)
    else:
        yield base, obj

# ---------- session resolvers ----------
def _resolve_session_uid(payload, forced_uid=None, src_hint=None):
    if forced_uid: return str(forced_uid)
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_uid","video_uid","video_id"):
        if payload.get(k): return str(payload[k])
        if meta.get(k):    return str(meta[k])
    fn = meta.get("file_name") or meta.get("filename")
    if not fn and src_hint:
        try:
            import os as _os
            fn = _os.path.splitext(_os.path.basename(src_hint))[0]
        except Exception: fn = None
    if fn: return str(fn)
    import hashlib as _hl, json as _json
    return f"sha1_{_hl.sha1(_json.dumps(payload, sort_keys=True, separators=(',',':')).encode()).hexdigest()[:12]}"

def _resolve_fps(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("fps","frame_rate","frames_per_second"):
        if payload.get(k) is not None: return _float(payload[k])
        if meta.get(k) is not None:    return _float(meta[k])
    return None

def _resolve_session_date(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_date","date","recorded_at"):
        raw = payload.get(k) if k in payload else meta.get(k)
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z","+00:00")).astimezone(timezone.utc)
            except Exception: return None
    return None

# ---------- raw snapshot ----------
def _save_raw_result(conn, session_id: int, payload: dict, size_threshold: int = 5_000_000):
    js = json.dumps(payload, separators=(",", ":"))
    try_json = len(js) <= size_threshold
    if try_json:
        try:
            conn.execute(sql_text("""
                INSERT INTO bronze.raw_result (session_id, payload_json, created_at)
                VALUES (:sid, CAST(:p AS JSONB), now() AT TIME ZONE 'utc')
            """), {"sid": session_id, "p": js})
            return
        except Exception:
            pass
    gz = gzip.compress(js.encode("utf-8"))
    sha = hashlib.sha256(js.encode("utf-8")).hexdigest()
    conn.execute(sql_text("""
        INSERT INTO bronze.raw_result (session_id, payload_json, payload_gzip, payload_sha256, created_at)
        VALUES (:sid, NULL, :gz, :sha, now() AT TIME ZONE 'utc')
    """), {"sid": session_id, "gz": gz, "sha": sha})

# ---------- strict ingest ----------
def ingest_bronze_strict(conn, payload: dict, replace=False, forced_uid=None, src_hint=None):
    # session row
    session_uid  = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
    fps          = _resolve_fps(payload)
    session_date = _resolve_session_date(payload)
    meta         = payload.get("meta") or payload.get("metadata") or {}

    conn.execute(sql_text("""
        INSERT INTO bronze.session (session_uid, fps, session_date, meta)
        VALUES (:u, :fps, :sdt, CAST(:m AS JSONB))
        ON CONFLICT (session_uid) DO UPDATE SET
          fps = COALESCE(EXCLUDED.fps, bronze.session.fps),
          session_date = COALESCE(EXCLUDED.session_date, bronze.session.session_date),
          meta = COALESCE(EXCLUDED.meta, bronze.session.meta)
    """), {"u": session_uid, "fps": fps, "sdt": session_date, "m": json.dumps(meta)})

    session_id = conn.execute(sql_text("SELECT session_id FROM bronze.session WHERE session_uid=:u"),
                              {"u": session_uid}).scalar_one()

    if replace:
        for t in ("ball_position","player_position","ball_bounce","swing","rally","player",
                  "session_confidences","thumbnail","highlight","team_session","bounce_heatmap"):
            conn.execute(sql_text(f"DELETE FROM bronze.{t} WHERE session_id=:sid"), {"sid": session_id})

    # raw save
    _save_raw_result(conn, session_id, payload)

    # players
    uid_to_player_id: Dict[str, int] = {}
    for p in (payload.get("players") or []):
        puid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        if not puid:
            conn.execute(sql_text("""
                INSERT INTO bronze.unmatched_field(session_id, json_path, example_value)
                VALUES (:sid, :p, CAST(:v AS JSONB))
            """), {"sid": session_id, "p": "players[*].<missing_uid>", "v": json.dumps(p)})
            continue

        full_name = p.get("full_name") or p.get("name")
        handed    = p.get("handedness")
        age       = p.get("age")
        utr       = _float(p.get("utr"))
        metrics   = p.get("metrics") or {}
        covered_distance  = _float(p.get("covered_distance") or metrics.get("covered_distance"))
        fastest_sprint    = _float(p.get("fastest_sprint")   or metrics.get("fastest_sprint"))
        fastest_sprint_ts = _float(p.get("fastest_sprint_timestamp") or metrics.get("fastest_sprint_timestamp_s"))
        activity_score    = _float(p.get("activity_score")   or metrics.get("activity_score"))
        swing_type_distribution = p.get("swing_type_distribution")
        location_heatmap        = p.get("location_heatmap") or p.get("heatmap")

        player_meta = {k: v for k, v in p.items() if k not in {
            "id","sportai_player_uid","uid","player_id","full_name","name","handedness","age","utr",
            "metrics","statistics","stats","swing_type_distribution","location_heatmap","heatmap"}}

        conn.execute(sql_text("""
            INSERT INTO bronze.player (
              session_id, sportai_player_uid, full_name, handedness, age, utr,
              covered_distance, fastest_sprint, fastest_sprint_ts_s, activity_score,
              swing_type_distribution, location_heatmap, meta
            ) VALUES (
              :sid, :puid, :nm, :hand, :age, :utr, :cd, :fs, :fst, :ascore,
              CAST(:dist AS JSONB), CAST(:lheat AS JSONB), CAST(:pmeta AS JSONB)
            )
            ON CONFLICT (session_id, sportai_player_uid) DO UPDATE SET
              full_name = COALESCE(EXCLUDED.full_name, bronze.player.full_name),
              handedness = COALESCE(EXCLUDED.handedness, bronze.player.handedness),
              age = COALESCE(EXCLUDED.age, bronze.player.age),
              utr = COALESCE(EXCLUDED.utr, bronze.player.utr),
              covered_distance = COALESCE(EXCLUDED.covered_distance, bronze.player.covered_distance),
              fastest_sprint = COALESCE(EXCLUDED.fastest_sprint, bronze.player.fastest_sprint),
              fastest_sprint_ts_s = COALESCE(EXCLUDED.fastest_sprint_ts_s, bronze.player.fastest_sprint_ts_s),
              activity_score = COALESCE(EXCLUDED.activity_score, bronze.player.activity_score),
              swing_type_distribution = COALESCE(EXCLUDED.swing_type_distribution, bronze.player.swing_type_distribution),
              location_heatmap = COALESCE(EXCLUDED.location_heatmap, bronze.player.location_heatmap),
              meta = COALESCE(EXCLUDED.meta, bronze.player.meta)
        """), {"sid": session_id, "puid": puid, "nm": full_name, "hand": handed, "age": age, "utr": utr,
               "cd": covered_distance, "fs": fastest_sprint, "fst": fastest_sprint_ts, "ascore": activity_score,
               "dist": json.dumps(swing_type_distribution) if swing_type_distribution is not None else None,
               "lheat": json.dumps(location_heatmap) if location_heatmap is not None else None,
               "pmeta": json.dumps(player_meta) if player_meta else None})

        pid = conn.execute(sql_text("""
            SELECT player_id FROM bronze.player WHERE session_id=:sid AND sportai_player_uid=:puid
        """), {"sid": session_id, "puid": puid}).scalar_one()
        uid_to_player_id[puid] = pid

    # ball_bounces
    for b in (payload.get("ball_bounces") or []):
        s  = _time_s(b.get("timestamp")) or _time_s(b.get("timestamp_s")) or _time_s(b.get("ts")) or _time_s(b.get("t"))
        bx = _float(b.get("x")); by = _float(b.get("y"))
        if bx is None or by is None:
            cp = b.get("court_pos") or b.get("court_position")
            if isinstance(cp, (list, tuple)) and len(cp) >= 2:
                bx, by = _float(cp[0]), _float(cp[1])
        btype = b.get("type") or b.get("bounce_type")
        hitter_uid = b.get("player_id") or b.get("sportai_player_uid")
        hitter_pid = uid_to_player_id.get(str(hitter_uid)) if hitter_uid is not None else None

        conn.execute(sql_text("""
            INSERT INTO bronze.ball_bounce (session_id, hitter_player_id, bounce_s, bounce_ts, x, y, bounce_type)
            VALUES (:sid, :pid, :s, :ts, :x, :y, :bt)
        """), {"sid": session_id, "pid": hitter_pid, "s": s,
               "ts": seconds_to_ts(_base_dt_for_session(session_date), s),
               "x": bx, "y": by, "bt": btype})

    # ball_positions
    for p in (payload.get("ball_positions") or []):
        s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
        hx = _float(p.get("x")); hy = _float(p.get("y"))
        conn.execute(sql_text("""
            INSERT INTO bronze.ball_position (session_id, ts_s, ts, x, y)
            VALUES (:sid, :ss, :ts, :x, :y)
        """), {"sid": session_id, "ss": s,
               "ts": seconds_to_ts(_base_dt_for_session(session_date), s),
               "x": hx, "y": hy})

    # player_positions
    for puid, arr in (payload.get("player_positions") or {}).items():
        pid = uid_to_player_id.get(str(puid))
        if not pid:
            conn.execute(sql_text("""
                INSERT INTO bronze.player (session_id, sportai_player_uid)
                VALUES (:sid, :puid) ON CONFLICT (session_id, sportai_player_uid) DO NOTHING
            """), {"sid": session_id, "puid": str(puid)})
            pid = conn.execute(sql_text("""
                SELECT player_id FROM bronze.player WHERE session_id=:sid AND sportai_player_uid=:puid
            """), {"sid": session_id, "puid": str(puid)}).scalar_one()
            uid_to_player_id[str(puid)] = pid

        for p in (arr or []):
            s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
            px = _float(p.get("court_X", p.get("court_x"))) if ("court_X" in p or "court_x" in p) else _float(p.get("X", p.get("x")))
            py = _float(p.get("court_Y", p.get("court_y"))) if ("court_Y" in p or "court_y" in p) else _float(p.get("Y", p.get("y")))
            conn.execute(sql_text("""
                INSERT INTO bronze.player_position (session_id, player_id, ts_s, ts, x, y)
                VALUES (:sid, :pid, :ss, :ts, :x, :y)
            """), {"sid": session_id, "pid": pid, "ss": s,
                   "ts": seconds_to_ts(_base_dt_for_session(session_date), s),
                   "x": px, "y": py})

    # swings (root + players[*].swings)
    def _emit_swing(obj, pid):
        start_s = _time_s(obj.get("start_ts")) or _time_s(obj.get("start_s")) or _time_s(obj.get("start"))
        end_s   = _time_s(obj.get("end_ts"))   or _time_s(obj.get("end_s"))   or _time_s(obj.get("end"))
        if start_s is None and end_s is None:
            only_ts = _time_s(obj.get("timestamp") or obj.get("ts") or obj.get("time_s") or obj.get("t"))
            if only_ts is not None:
                start_s = end_s = only_ts
        bh_s = _time_s(obj.get("ball_hit_timestamp") or obj.get("ball_hit_ts") or obj.get("ball_hit_s"))
        bhx = bhy = None
        if bh_s is None and isinstance(obj.get("ball_hit"), dict):
            bh_s = _time_s(obj["ball_hit"].get("timestamp"))
            loc = obj["ball_hit"].get("location") or {}
            bhx = _float(loc.get("x")); bhy = _float(loc.get("y"))

        conn.execute(sql_text("""
            INSERT INTO bronze.swing (
              session_id, player_id, sportai_swing_uid,
              start_s, end_s, ball_hit_s,
              start_ts, end_ts, ball_hit_ts,
              ball_hit_x, ball_hit_y, ball_speed, ball_player_distance,
              swing_type, volley, is_in_rally, serve, serve_type, meta
            ) VALUES (
              :sid, :pid, :suid,
              :ss, :es, :bhs,
              :sts, :ets, :bhts,
              :bhx, :bhy, :bs, :bpd,
              :st, :vol, :inr, :srv, :srv_type, CAST(:meta AS JSONB)
            )
        """), {
            "sid": session_id, "pid": pid, "suid": obj.get("id") or obj.get("swing_uid") or obj.get("uid"),
            "ss": start_s, "es": end_s, "bhs": bh_s,
            "sts": seconds_to_ts(_base_dt_for_session(None), start_s),
            "ets": seconds_to_ts(_base_dt_for_session(None), end_s),
            "bhts": seconds_to_ts(_base_dt_for_session(None), bh_s),
            "bhx": _float(obj.get("ball_hit_location", {}).get("x")) if isinstance(obj.get("ball_hit_location"), dict) else bhx,
            "bhy": _float(obj.get("ball_hit_location", {}).get("y")) if isinstance(obj.get("ball_hit_location"), dict) else bhy,
            "bs": _float(obj.get("ball_speed")),
            "bpd": _float(obj.get("ball_player_distance")),
            "st": (str(obj.get("swing_type") or obj.get("type") or obj.get("label") or obj.get("stroke_type") or "")).lower(),
            "vol": _bool(obj.get("volley")), "inr": _bool(obj.get("is_in_rally")),
            "srv": _bool(obj.get("serve")), "srv_type": obj.get("serve_type"),
            "meta": json.dumps({k:v for k,v in obj.items() if k not in {
                "id","uid","swing_uid","player_id","sportai_player_uid","player_uid",
                "start","start_s","start_ts","end","end_s","end_ts","timestamp","ts","time_s","t",
                "ball_hit","ball_hit_timestamp","ball_hit_ts","ball_hit_s","ball_hit_location",
                "type","label","stroke_type","swing_type","volley","is_in_rally","serve","serve_type",
                "ball_speed","ball_player_distance"
            }})
        })

    for s in (payload.get("swings") or []):
        suid = s.get("player_id") or s.get("sportai_player_uid") or s.get("player_uid")
        pid = uid_to_player_id.get(str(suid)) if suid is not None else None
        _emit_swing(s, pid)

    for p in (payload.get("players") or []):
        pid = uid_to_player_id.get(str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or ""))
        for s in (p.get("swings") or []):
            _emit_swing(s, pid)

    # rallies (if present)
    payload_rallies = payload.get("rallies") or []
    for i, r in enumerate(payload_rallies, start=1):
        if isinstance(r, dict):
            start_s = _time_s(r.get("start_ts")) or _time_s(r.get("start"))
            end_s   = _time_s(r.get("end_ts"))   or _time_s(r.get("end"))
        else:
            try: start_s, end_s = _float(r[0]), _float(r[1])
            except Exception: start_s, end_s = None, None
        conn.execute(sql_text("""
            INSERT INTO bronze.rally (session_id, rally_number, start_s, end_s, start_ts, end_ts)
            VALUES (:sid, :n, :ss, :es, :sts, :ets)
            ON CONFLICT (session_id, rally_number) DO UPDATE SET
              start_s=COALESCE(EXCLUDED.start_s,bronze.rally.start_s),
              end_s  =COALESCE(EXCLUDED.end_s,  bronze.rally.end_s),
              start_ts=COALESCE(EXCLUDED.start_ts,bronze.rally.start_ts),
              end_ts  =COALESCE(EXCLUDED.end_ts,  bronze.rally.end_ts)
        """), {"sid": session_id, "n": i, "ss": start_s, "es": end_s,
               "sts": seconds_to_ts(_base_dt_for_session(session_date), start_s),
               "ets": seconds_to_ts(_base_dt_for_session(session_date), end_s)})

    # optional JSONB towers
    def _upsert_jsonb(table, data):
        conn.execute(sql_text(f"""
            INSERT INTO bronze.{table} (session_id, data)
            VALUES (:sid, CAST(:j AS JSONB))
            ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
        """), {"sid": session_id, "j": json.dumps(data)})

    if payload.get("confidences") is not None:
        _upsert_jsonb("session_confidences", payload["confidences"])
    thumbs = payload.get("thumbnails") or payload.get("thumbnail_crops")
    if thumbs is not None:
        _upsert_jsonb("thumbnail", thumbs)
    if payload.get("highlights") is not None:
        _upsert_jsonb("highlight", payload["highlights"])
    if payload.get("team_sessions") is not None:
        _upsert_jsonb("team_session", payload["team_sessions"])
    if payload.get("bounce_heatmap") is not None:
        _upsert_jsonb("bounce_heatmap", payload["bounce_heatmap"])

    # unmatched top-level keys
    known = {"players","swings","rallies","ball_bounces","ball_positions","player_positions",
             "confidences","thumbnails","thumbnail_crops","highlights","team_sessions","bounce_heatmap",
             "meta","metadata","session_uid","video_uid","video_id","fps","frame_rate","frames_per_second",
             "session_date","date","recorded_at"}
    for k,v in (payload.items() if isinstance(payload, dict) else []):
        if k not in known:
            conn.execute(sql_text("""
              INSERT INTO bronze.unmatched_field(session_id, json_path, example_value)
              VALUES (:sid, :p, CAST(:v AS JSONB))
            """), {"sid": session_id, "p": k, "v": json.dumps(v)})

    return {"session_uid": session_uid, "session_id": session_id}

# ---------- endpoints ----------
@ingest_bronze.post("/bronze/ingest-file")
def bronze_ingest_file():
    if not _guard(): return _forbid()
    replace = str(request.values.get("replace","1")).lower() in ("1","true","yes","y","on")
    forced_uid = request.values.get("session_uid") or None

    # payload
    payload = None
    if "file" in request.files and request.files["file"].filename:
        payload = json.load(request.files["file"].stream)
    elif request.values.get("url"):
        r = requests.get(request.values["url"], timeout=90); r.raise_for_status(); payload = r.json()
    else:
        payload = request.get_json(force=True, silent=False)

    with engine.begin() as conn:
        res = ingest_bronze_strict(conn, payload, replace=replace, forced_uid=forced_uid, src_hint=request.values.get("url"))
        sid = res["session_id"]
        counts = conn.execute(sql_text("""
            SELECT
              (SELECT COUNT(*) FROM bronze.rally            WHERE session_id=:sid),
              (SELECT COUNT(*) FROM bronze.ball_bounce      WHERE session_id=:sid),
              (SELECT COUNT(*) FROM bronze.ball_position    WHERE session_id=:sid),
              (SELECT COUNT(*) FROM bronze.player_position  WHERE session_id=:sid),
              (SELECT COUNT(*) FROM bronze.swing            WHERE session_id=:sid)
        """), {"sid": sid}).fetchone()
    return jsonify({"ok": True, **res, "bronze_counts": {
        "rallies": counts[0], "ball_bounces": counts[1], "ball_positions": counts[2], "player_positions": counts[3], "swings": counts[4]
    }})

@ingest_bronze.post("/bronze/reingest-from-raw")
def bronze_reingest_from_raw():
    if not _guard(): return _forbid()
    data = request.get_json(silent=True) or request.form
    sid = int(data.get("session_id"))
    replace = str(data.get("replace","1")).lower() in ("1","true","yes","y","on")

    with engine.begin() as conn:
        row = conn.execute(sql_text("""
            SELECT s.session_uid,
                   (SELECT payload_json FROM bronze.raw_result WHERE session_id=s.session_id ORDER BY created_at DESC LIMIT 1),
                   (SELECT payload_gzip  FROM bronze.raw_result WHERE session_id=s.session_id ORDER BY created_at DESC LIMIT 1)
            FROM bronze.session s WHERE s.session_id=:sid
        """), {"sid": sid}).first()
        if not row: return jsonify({"ok": False, "error": "unknown session_id"}), 404
        forced_uid, pj, gz = row[0], row[1], row[2]
        if pj is not None:
            payload = pj if isinstance(pj, dict) else json.loads(pj)
        elif gz is not None:
            payload = json.loads(gzip.decompress(gz).decode("utf-8"))
        else:
            return jsonify({"ok": False, "error": "no raw_result for session"}), 404

        res = ingest_bronze_strict(conn, payload, replace=replace, forced_uid=forced_uid)
        counts = conn.execute(sql_text("""
            SELECT
              (SELECT COUNT(*) FROM bronze.rally            WHERE session_id=:sid),
              (SELECT COUNT(*) FROM bronze.ball_bounce      WHERE session_id=:sid),
              (SELECT COUNT(*) FROM bronze.ball_position    WHERE session_id=:sid),
              (SELECT COUNT(*) FROM bronze.player_position  WHERE session_id=:sid),
              (SELECT COUNT(*) FROM bronze.swing            WHERE session_id=:sid)
        """), {"sid": sid}).fetchone()

    return jsonify({"ok": True, **res, "bronze_counts": {
        "rallies": counts[0], "ball_bounces": counts[1], "ball_positions": counts[2], "player_positions": counts[3], "swings": counts[4]
    }})
