# ingest_app.py — blueprint with ingest core + ops endpoints
import os, json, re
from datetime import datetime, timezone, timedelta
from typing import Dict

import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

import gzip, hashlib

from db_init import engine  # shared engine

import logging
_log = logging.getLogger("ingest")

from db_init import (
    upsert_session_confidences,
    upsert_thumbnail,
    insert_highlights,
    insert_team_sessions,
    upsert_bounce_heatmap,
)

ingest_bp = Blueprint("ingest_bp", __name__)

OPS_KEY = os.getenv("OPS_KEY", "")
STRICT_BRONZE_RAW = os.environ.get("STRICT_BRONZE_RAW", "1").lower() in ("1","true","yes","y")
RALLY_GAP_S       = float(os.environ.get("RALLY_GAP_S", "6.0"))

DEFAULT_REPLACE_ON_INGEST = (
    os.getenv("INGEST_REPLACE_EXISTING")
    or os.getenv("DEFAULT_REPLACE_ON_INGEST")
    or os.getenv("STRICT_REINGEST")
    or "1"
).strip().lower() in ("1","true","yes","y")

# ---- SportAI integration config (aligned with upload_app.py) ----
SPORT_AI_BASE         = os.getenv("SPORT_AI_BASE", "https://api.sportai.app").strip().rstrip("/")
SPORT_AI_TOKEN        = os.getenv("SPORT_AI_TOKEN", "")
AUTO_INGEST_ON_COMPLETE = os.getenv("AUTO_INGEST_ON_COMPLETE", "1").lower() in ("1","true","yes","y")
SPORTAI_WEBHOOK_SECRET  = os.getenv("SPORTAI_WEBHOOK_SECRET", "")  # optional

# Fallback bases + status paths (tenants vary)
SPORT_AI_BASES = list(dict.fromkeys([
    SPORT_AI_BASE,
    "https://api.sportai.app",
    "https://sportai.app",
    "https://api.sportai.com",
]))
SPORT_AI_STATUS_PATHS = list(dict.fromkeys([
    os.getenv("SPORT_AI_STATUS_PATH", "/api/statistics/{task_id}"),
    "/api/statistics/tennis/{task_id}",
    "/api/statistics/{task_id}",
]))


def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

def _forbid():
    return Response("Forbidden", 403)


# ---------- small utils (trimmed for blueprint) ----------
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

def _quantize_time_to_fps(s, fps):
    if s is None or not fps: return s
    return round(round(float(s) * float(fps)) / float(fps), 5)

def _quantize_time(s, fps):
    if s is None: return None
    if fps: return _quantize_time_to_fps(s, fps)
    return round(float(s), 3)

def _base_dt_for_session(dt):
    return dt if dt else datetime(1970,1,1,tzinfo=timezone.utc)

# ---------- RAW RESULT storage helpers (NEW) ----------
def _ensure_raw_result_schema(conn):
    # base table
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS raw_result (
          id             BIGSERIAL PRIMARY KEY,
          session_id     INT NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
          payload_json   JSONB,
          payload_gzip   BYTEA,
          payload_sha256 TEXT,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    # add columns if they don't exist yet
    conn.execute(sql_text("""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='raw_result' AND column_name='payload_gzip'
          ) THEN
            ALTER TABLE raw_result ADD COLUMN payload_gzip BYTEA;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='raw_result' AND column_name='payload_sha256'
          ) THEN
            ALTER TABLE raw_result ADD COLUMN payload_sha256 TEXT;
          END IF;
        END $$;
    """))

def _save_raw_result(conn, session_id: int, payload: dict, size_threshold: int = 5_000_000):
    """
    Store the raw SportAI payload:
      - If <= size_threshold (~5MB), keep JSONB (easy to inspect).
      - If larger or JSONB insert fails, store GZIP-compressed bytes + SHA-256 and leave JSONB NULL.
    """
    _ensure_raw_result_schema(conn)

    js = json.dumps(payload, separators=(",", ":"))
    try_json = len(js) <= size_threshold

    if try_json:
        try:
            conn.execute(sql_text("""
                INSERT INTO raw_result (session_id, payload_json, created_at)
                VALUES (:sid, CAST(:p AS JSONB), now() AT TIME ZONE 'utc')
            """), {"sid": session_id, "p": js})
            return
        except Exception:
            # fall through to gzip path if JSON insert trips any limits
            pass

    gz = gzip.compress(js.encode("utf-8"))
    sha = hashlib.sha256(js.encode("utf-8")).hexdigest()
    conn.execute(sql_text("""
        INSERT INTO raw_result (session_id, payload_json, payload_gzip, payload_sha256, created_at)
        VALUES (:sid, NULL, :gz, :sha, now() AT TIME ZONE 'utc')
    """), {"sid": session_id, "gz": gz, "sha": sha})

# ---------- ingest core (as in your working build) ----------
_SWING_TYPES   = {"swing","stroke","shot","hit","serve","forehand","backhand","volley","overhead","slice","drop","lob"}
_SERVE_LABELS  = {"serve","first_serve","1st_serve","second_serve","2nd_serve"}

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

def _extract_ball_hit_from_events(events):
    if not isinstance(events, list): return (None, None, None)
    for ev in events:
        if not isinstance(ev, dict): continue
        label = (str(ev.get("type") or ev.get("label") or "")).lower()
        if label in {"ball_hit","contact","impact"}:
            ts = _time_s(ev.get("timestamp") or ev.get("ts") or ev.get("time_s") or ev.get("t"))
            loc = ev.get("location") or {}
            return ts, _float((loc or {}).get("x")), _float((loc or {}).get("y"))
    return (None, None, None)

def _normalize_swing_obj(obj):
    if not isinstance(obj, dict):
        return None

    # --- NEW: meta fields ---
    rally_key_present      = 'rally' in obj
    rally_val              = obj.get('rally', None) if rally_key_present else None
    rally_is_json_null     = (rally_key_present and rally_val is None)
    rally_text             = (None if rally_val is None else str(rally_val))

    is_valid               = _bool(obj.get('valid'))

    annotations            = obj.get('annotations') if isinstance(obj.get('annotations'), list) else None
    annotations_count      = (len(annotations) if isinstance(annotations, list) else None)
    ann0                   = (annotations[0] if annotations and len(annotations) > 0 and isinstance(annotations[0], dict) else None)
    ann0_format            = (ann0.get('annotation_format') if isinstance(ann0, dict) else None)
    ann0_tracking_id       = (ann0.get('tracking_id') if isinstance(ann0, dict) else None)
    ann0_bbox              = (ann0.get('bbox') if isinstance(ann0, dict) else None)

    ball_trajectory        = obj.get('ball_trajectory')
    ball_impact_type       = obj.get('ball_impact_type')
    ball_impact_location   = obj.get('ball_impact_location')
    intercepting_player_id = obj.get('intercepting_player_id')

    # --- everything below is your existing logic ---
    suid = obj.get("id") or obj.get("swing_uid") or obj.get("uid")
    start_s = _time_s(obj.get("start_ts")) or _time_s(obj.get("start_s")) or _time_s(obj.get("start"))
    end_s = _time_s(obj.get("end_ts")) or _time_s(obj.get("end_s")) or _time_s(obj.get("end"))
    if start_s is None and end_s is None:
        only_ts = _time_s(obj.get("timestamp") or obj.get("ts") or obj.get("time_s") or obj.get("t"))
        if only_ts is not None:
            start_s = end_s = only_ts

    bh_s = _time_s(obj.get("ball_hit_timestamp") or obj.get("ball_hit_ts") or obj.get("ball_hit_s"))
    bhx = bhy = None
    if bh_s is None and isinstance(obj.get("ball_hit"), dict):
        bh_s = _time_s(obj["ball_hit"].get("timestamp"))
        loc = obj["ball_hit"].get("location") or {}
        bhx = _float(loc.get("x"))
        bhy = _float(loc.get("y"))
    if bh_s is None:
        ev_bh_s, ev_bhx, ev_bhy = _extract_ball_hit_from_events(obj.get("events"))
        bh_s = ev_bh_s
        bhx = bhx if bhx is not None else ev_bhx
        bhy = bhy if bhy is not None else ev_bhy

    loc_any = obj.get("ball_hit_location")
    if (bhx is None or bhy is None) and isinstance(loc_any, dict):
        bhx = _float(loc_any.get("x"))
        bhy = _float(loc_any.get("y"))
    if (bhx is None or bhy is None) and isinstance(loc_any, (list, tuple)) and len(loc_any) >= 2:
        bhx = _float(loc_any[0])
        bhy = _float(loc_any[1])

    swing_type = (str(obj.get("swing_type") or obj.get("type") or obj.get("label") or obj.get("stroke_type") or "")).lower()
    serve = _bool(obj.get("serve"))
    serve_type = obj.get("serve_type")
    if not serve and swing_type in _SERVE_LABELS:
        serve = True
        if serve_type is None and swing_type != "serve":
            serve_type = swing_type

    player_uid = obj.get("player_id") or obj.get("sportai_player_uid") or obj.get("player_uid") or obj.get("player")
    if player_uid is not None:
        player_uid = str(player_uid)

    ball_speed = _float(obj.get("ball_speed"))
    ball_player_distance = _float(obj.get("ball_player_distance"))
    volley = _bool(obj.get("volley"))
    is_in_rally = _bool(obj.get("is_in_rally"))
    confidence_swing_type = _float(obj.get("confidence_swing_type"))
    confidence = _float(obj.get("confidence"))
    confidence_volley = _float(obj.get("confidence_volley"))

    if start_s is None and end_s is None and bh_s is None:
        return None

    _strip = {
        "id","uid","swing_uid","player_id","sportai_player_uid","player_uid","player",
        "type","label","stroke_type","swing_type","start","start_s","start_ts","end","end_s","end_ts",
        "timestamp","ts","time_s","t","ball_hit","ball_hit_timestamp","ball_hit_ts","ball_hit_s","ball_hit_location",
        "events","serve","serve_type","ball_speed","ball_player_distance","volley","is_in_rally",
        "confidence_swing_type","confidence","confidence_volley",
        "rally","valid","annotations","ball_trajectory","ball_impact_type","ball_impact_location","intercepting_player_id"
    }
    meta = {k: v for k, v in obj.items() if k not in _strip}

    return {
        "suid": suid, "player_uid": player_uid,
        "start_s": start_s, "end_s": end_s,
        "ball_hit_s": bh_s, "ball_hit_x": bhx, "ball_hit_y": bhy,
        "swing_type": swing_type, "volley": volley, "is_in_rally": is_in_rally,
        "serve": serve, "serve_type": serve_type,
        "confidence_swing_type": confidence_swing_type,
        "confidence": confidence, "confidence_volley": confidence_volley,
        "ball_speed": ball_speed, "ball_player_distance": ball_player_distance,
        "rally_key_present": rally_key_present,
        "rally_is_json_null": rally_is_json_null,
        "rally_text": rally_text,
        "is_valid": is_valid,
        "annotations": annotations,
        "annotations_count": annotations_count,
        "ann0_format": ann0_format,
        "ann0_tracking_id": ann0_tracking_id,
        "ann0_bbox": ann0_bbox,
        "ball_trajectory": ball_trajectory,
        "ball_impact_type": ball_impact_type,
        "ball_impact_location": ball_impact_location,
        "intercepting_player_id": intercepting_player_id,
        "meta": meta or None
    }


def _iter_candidate_swings_from_container(container):
    if not isinstance(container, dict): return
    keys = ("swings","strokes","swing_events") if STRICT_BRONZE_RAW else ("swings","strokes","swing_events","events")
    for key in keys:
        arr = container.get(key)
        if not isinstance(arr, list): continue
        if key == "events":
            for item in arr:
                lbl = str((item or {}).get("type") or (item or {}).get("label") or "").lower()
                if lbl and (lbl in _SWING_TYPES or "swing" in lbl or "stroke" in lbl):
                    norm = _normalize_swing_obj(item)
                    if norm: yield norm
        else:
            for item in arr:
                norm = _normalize_swing_obj(item)
                if norm: yield norm

def _gather_all_swings(payload):
    for norm in _iter_candidate_swings_from_container(payload or {}): yield norm
    for p in (payload.get("players") or []):
        p_uid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        for norm in _iter_candidate_swings_from_container(p):
            if not norm.get("player_uid") and p_uid: norm["player_uid"] = p_uid
            yield norm
        stats = p.get("statistics") or p.get("stats") or {}
        for norm in _iter_candidate_swings_from_container(stats):
            if not norm.get("player_uid") and p_uid: norm["player_uid"] = p_uid
            yield norm

def _insert_swing(conn, session_id, player_id, s, base_dt, fps):
    q_start = _quantize_time(s.get("start_s"), fps)
    q_end   = _quantize_time(s.get("end_s"), fps)
    q_hit   = _quantize_time(s.get("ball_hit_s"), fps)

    conn.execute(sql_text("""
        INSERT INTO fact_swing (
            session_id, player_id, sportai_swing_uid,
            start_s, end_s, ball_hit_s,
            start_ts, end_ts, ball_hit_ts,
            ball_hit_x, ball_hit_y, ball_speed, ball_player_distance,
            swing_type, volley, is_in_rally, serve, serve_type,
            confidence_swing_type, confidence, confidence_volley,
            -- NEW lifted meta fields ↓
            rally_key_present, rally_is_json_null, rally_text, is_valid,
            annotations, annotations_count, ann0_format, ann0_tracking_id, ann0_bbox,
            ball_trajectory, ball_impact_type, ball_impact_location, intercepting_player_id,
            meta
        ) VALUES (
            :sid, :pid, :suid,
            :ss, :es, :bhs,
            :sts, :ets, :bh_ts,
            :bhx, :bhy, :bs, :bpd,
            :sw_type, :vol, :inr, :srv, :srv_type,
            :cst, :conf, :cv,
            :rkp, :rjn, :rtxt, :ival,
            CAST(:ann AS JSONB), :annc, :ann0_fmt, :ann0_tid, CAST(:ann0_bbox AS JSONB),
            CAST(:btraj AS JSONB), :bit, :bil, :ipid,
            CAST(:meta AS JSONB)
        )
    """), {
        "sid": session_id, "pid": player_id, "suid": s.get("suid"),
        "ss": q_start, "es": q_end, "bhs": q_hit,
        "sts": seconds_to_ts(_base_dt_for_session(None), q_start),
        "ets": seconds_to_ts(_base_dt_for_session(None), q_end),
        "bh_ts": seconds_to_ts(_base_dt_for_session(None), q_hit),

        "bhx": s.get("ball_hit_x"), "bhy": s.get("ball_hit_y"),
        "bs": s.get("ball_speed"), "bpd": s.get("ball_player_distance"),

        "sw_type": s.get("swing_type"),
        "vol": s.get("volley"), "inr": s.get("is_in_rally"),
        "srv": s.get("serve"), "srv_type": s.get("serve_type"),

        "cst": s.get("confidence_swing_type"), "conf": s.get("confidence"),
        "cv": s.get("confidence_volley"),

        # NEW values
        "rkp": s.get("rally_key_present"),
        "rjn": s.get("rally_is_json_null"),
        "rtxt": s.get("rally_text"),
        "ival": s.get("is_valid"),

        "ann": json.dumps(s.get("annotations")) if s.get("annotations") is not None else None,
        "annc": s.get("annotations_count"),
        "ann0_fmt": s.get("ann0_format"),
        "ann0_tid": s.get("ann0_tracking_id"),
        "ann0_bbox": json.dumps(s.get("ann0_bbox")) if s.get("ann0_bbox") is not None else None,

        "btraj": json.dumps(s.get("ball_trajectory")) if s.get("ball_trajectory") is not None else None,
        "bit": s.get("ball_impact_type"),
        "bil": s.get("ball_impact_location"),
        "ipid": s.get("intercepting_player_id"),

        "meta": json.dumps(s.get("meta")) if s.get("meta") else None
    })


def _ensure_rallies_from_swings(conn, session_id, gap_s=6.0):
    conn.execute(sql_text("""
    WITH sw AS (
      SELECT session_id, COALESCE(ball_hit_s, start_s) AS t, swing_id,
             ROW_NUMBER() OVER (ORDER BY COALESCE(ball_hit_s, start_s), swing_id) AS rn
      FROM fact_swing
      WHERE session_id = :sid AND COALESCE(ball_hit_s, start_s) IS NOT NULL
    ),
    seg AS (
      SELECT sw.*, CASE
        WHEN rn = 1 THEN 1
        WHEN (t - LAG(t) OVER (ORDER BY rn)) >= :gap THEN 1
        ELSE 0
      END AS new_seg
      FROM sw
    ),
    grp AS (
      SELECT session_id, t, SUM(new_seg) OVER (ORDER BY rn) AS grp_id
      FROM seg
    ),
    bounds AS (
      SELECT session_id, grp_id, MIN(t) AS start_s, MAX(t) AS end_s
      FROM grp
      GROUP BY session_id, grp_id
    )
    INSERT INTO dim_rally (session_id, rally_number, start_s, end_s)
    SELECT b.session_id, ROW_NUMBER() OVER (ORDER BY start_s), b.start_s, b.end_s
    FROM bounds b
    ON CONFLICT (session_id, rally_number) DO UPDATE
      SET start_s = EXCLUDED.start_s, end_s = EXCLUDED.end_s;
    """), {"sid": session_id, "gap": float(gap_s)})

def _link_swings_to_rallies(conn, session_id):
    conn.execute(sql_text("""
        UPDATE fact_swing fs
           SET rally_id = dr.rally_id
          FROM dim_rally dr
         WHERE fs.session_id = :sid
           AND dr.session_id = :sid
           AND fs.rally_id IS NULL
           AND COALESCE(fs.ball_hit_s, fs.start_s) BETWEEN dr.start_s AND dr.end_s
    """), {"sid": session_id})

def _normalize_serve_flags(conn, session_id):
    conn.execute(sql_text("""
    WITH serves AS (
      SELECT fs.session_id, fs.rally_id, fs.swing_id,
             ROW_NUMBER() OVER (PARTITION BY fs.session_id, fs.rally_id
                                ORDER BY COALESCE(fs.ball_hit_s, fs.start_s), fs.swing_id) AS rn
      FROM fact_swing fs
     WHERE fs.session_id = :sid AND fs.rally_id IS NOT NULL AND COALESCE(fs.serve, FALSE)
    )
    UPDATE fact_swing fs SET serve = (serves.rn = 1)
    FROM serves WHERE fs.swing_id = serves.swing_id;
    """), {"sid": session_id})

def _rebuild_ts_from_seconds(conn, session_id):
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE((SELECT MIN(COALESCE(ball_hit_s, start_s))
                           FROM fact_swing WHERE session_id=:sid), 0) AS t0
        )
        UPDATE fact_swing fs
           SET start_ts = make_timestamp(1970,1,1,0,0,0) + make_interval(secs => GREATEST(0, COALESCE(fs.start_s,0) - z.t0)),
               end_ts   = make_timestamp(1970,1,1,0,0,0) + make_interval(secs => GREATEST(0, COALESCE(fs.end_s,0) - z.t0)),
               ball_hit_ts = make_timestamp(1970,1,1,0,0,0) + make_interval(secs => GREATEST(0, COALESCE(fs.ball_hit_s,0) - z.t0))
          FROM z WHERE fs.session_id = z.session_id;
    """), {"sid": session_id})
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE((SELECT MIN(COALESCE(ball_hit_s, start_s))
                           FROM fact_swing WHERE session_id=:sid), 0) AS t0
        )
        UPDATE fact_bounce b
           SET bounce_ts = make_timestamp(1970,1,1,0,0,0) + make_interval(secs => GREATEST(0, COALESCE(b.bounce_s,0) - z.t0))
          FROM z WHERE b.session_id = z.session_id;
    """), {"sid": session_id})
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE((SELECT MIN(COALESCE(ball_hit_s, start_s))
                           FROM fact_swing WHERE session_id=:sid), 0) AS t0
        )
        UPDATE fact_ball_position bp
           SET ts = make_timestamp(1970,1,1,0,0,0) + make_interval(secs => GREATEST(0, COALESCE(bp.ts_s,0) - z.t0))
          FROM z WHERE bp.session_id = z.session_id;
    """), {"sid": session_id})
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE((SELECT MIN(COALESCE(ball_hit_s, start_s))
                           FROM fact_swing WHERE session_id=:sid), 0) AS t0
        )
        UPDATE fact_player_position pp
           SET ts = make_timestamp(1970,1,1,0,0,0) + make_interval(secs => GREATEST(0, COALESCE(pp.ts_s,0) - z.t0))
          FROM z WHERE pp.session_id = z.session_id;
    """), {"sid": session_id})

def ingest_result_v2(conn, payload: dict, replace=False, forced_uid=None, src_hint=None):
    session_uid  = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
    fps          = _resolve_fps(payload)
    session_date = _resolve_session_date(payload)
    base_dt      = _base_dt_for_session(session_date)
    meta         = payload.get("meta") or payload.get("metadata") or {}
    meta_json    = json.dumps(meta)

    conn.execute(sql_text("""
        INSERT INTO dim_session (session_uid, fps, session_date, meta)
        VALUES (:u, :fps, :sdt, CAST(:m AS JSONB))
        ON CONFLICT (session_uid) DO UPDATE SET
          fps = COALESCE(EXCLUDED.fps, dim_session.fps),
          session_date = COALESCE(EXCLUDED.session_date, dim_session.session_date),
          meta = COALESCE(EXCLUDED.meta, dim_session.meta)
    """), {"u": session_uid, "fps": fps, "m": meta_json, "sdt": session_date})

    session_id = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid=:u"),
                              {"u": session_uid}).scalar_one()

    if replace:
        for t in ("fact_ball_position","fact_player_position","fact_bounce","fact_swing","dim_rally","dim_player"):
            conn.execute(sql_text(f"DELETE FROM {t} WHERE session_id=:sid"), {"sid": session_id})

    # NEW: resilient raw storage (JSONB if small, else GZIP)
    _save_raw_result(conn, session_id, payload)

    # players
    players = payload.get("players") or []
    uid_to_player_id: Dict[str, int] = {}
    conn.execute(sql_text("ALTER TABLE IF EXISTS dim_player ADD COLUMN IF NOT EXISTS meta JSONB"))

    for p in players:
        puid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        if not puid: continue
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
            INSERT INTO dim_player (
                session_id, sportai_player_uid, full_name, handedness, age, utr,
                covered_distance, fastest_sprint, fastest_sprint_timestamp_s, activity_score,
                swing_type_distribution, location_heatmap, meta
            ) VALUES (
                :sid, :puid, :nm, :hand, :age, :utr, :cd, :fs, :fst, :ascore,
                CAST(:dist AS JSONB), CAST(:lheat AS JSONB), CAST(:pmeta AS JSONB)
            )
            ON CONFLICT (session_id, sportai_player_uid) DO UPDATE SET
              full_name = COALESCE(EXCLUDED.full_name, dim_player.full_name),
              handedness = COALESCE(EXCLUDED.handedness, dim_player.handedness),
              age = COALESCE(EXCLUDED.age, dim_player.age),
              utr = COALESCE(EXCLUDED.utr, dim_player.utr),
              covered_distance = COALESCE(EXCLUDED.covered_distance, dim_player.covered_distance),
              fastest_sprint = COALESCE(EXCLUDED.fastest_sprint, dim_player.fastest_sprint),
              fastest_sprint_timestamp_s = COALESCE(EXCLUDED.fastest_sprint_timestamp_s, dim_player.fastest_sprint_timestamp_s),
              activity_score = COALESCE(EXCLUDED.activity_score, dim_player.activity_score),
              swing_type_distribution = COALESCE(EXCLUDED.swing_type_distribution, dim_player.swing_type_distribution),
              location_heatmap = COALESCE(EXCLUDED.location_heatmap, dim_player.location_heatmap),
              meta = COALESCE(EXCLUDED.meta, dim_player.meta)
        """), {"sid": session_id, "puid": puid, "nm": full_name, "hand": handed, "age": age, "utr": utr,
               "cd": covered_distance, "fs": fastest_sprint, "fst": fastest_sprint_ts, "ascore": activity_score,
               "dist": json.dumps(swing_type_distribution) if swing_type_distribution is not None else None,
               "lheat": json.dumps(location_heatmap) if location_heatmap is not None else None,
               "pmeta": json.dumps(player_meta) if player_meta else None})

        pid = conn.execute(sql_text("""
            SELECT player_id FROM dim_player WHERE session_id=:sid AND sportai_player_uid=:puid
        """), {"sid": session_id, "puid": puid}).scalar_one()
        uid_to_player_id[puid] = pid

    # ensure players from player_positions
    for puid, arr in (payload.get("player_positions") or {}).items():
        if str(puid) not in uid_to_player_id and arr:
            conn.execute(sql_text("""
                INSERT INTO dim_player (session_id, sportai_player_uid)
                VALUES (:sid, :puid) ON CONFLICT (session_id, sportai_player_uid) DO NOTHING
            """), {"sid": session_id, "puid": str(puid)})
            pid = conn.execute(sql_text("""
                SELECT player_id FROM dim_player WHERE session_id=:sid AND sportai_player_uid=:p
            """), {"sid": session_id, "p": str(puid)}).scalar_one()
            uid_to_player_id[str(puid)] = pid

    # rallies (optional)
    payload_rallies = payload.get("rallies") or []
    had_payload_rallies = isinstance(payload_rallies, list) and len(payload_rallies) > 0
    for i, r in enumerate(payload_rallies, start=1):
        if isinstance(r, dict):
            start_s = _time_s(r.get("start_ts")) or _time_s(r.get("start"))
            end_s   = _time_s(r.get("end_ts"))   or _time_s(r.get("end"))
        else:
            try: start_s, end_s = _float(r[0]), _float(r[1])
            except Exception: start_s, end_s = None, None
        conn.execute(sql_text("""
            INSERT INTO dim_rally (session_id, rally_number, start_s, end_s, start_ts, end_ts)
            VALUES (:sid, :n, :ss, :es, :sts, :ets)
            ON CONFLICT (session_id, rally_number) DO UPDATE SET
              start_s=COALESCE(EXCLUDED.start_s,dim_rally.start_s),
              end_s  =COALESCE(EXCLUDED.end_s,  dim_rally.end_s),
              start_ts=COALESCE(EXCLUDED.start_ts,dim_rally.start_ts),
              end_ts  =COALESCE(EXCLUDED.end_ts,  dim_rally.end_ts)
        """), {"sid": session_id, "n": i, "ss": start_s, "es": end_s,
               "sts": seconds_to_ts(_base_dt_for_session(session_date), start_s),
               "ets": seconds_to_ts(_base_dt_for_session(session_date), end_s)})

    def rally_id_for_ts(ts_s):
        if ts_s is None: return None
        row = conn.execute(sql_text("""
            SELECT rally_id FROM dim_rally
            WHERE session_id=:sid AND :s BETWEEN start_s AND end_s
            ORDER BY rally_number LIMIT 1
        """), {"sid": session_id, "s": ts_s}).fetchone()
        return row[0] if row else None

    # bounces
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
            INSERT INTO fact_bounce (session_id, hitter_player_id, rally_id, bounce_s, bounce_ts, x, y, bounce_type)
            VALUES (:sid, :pid, :rid, :s, :ts, :x, :y, :bt)
        """), {"sid": session_id, "pid": hitter_pid, "rid": rally_id_for_ts(s),
               "s": s, "ts": seconds_to_ts(_base_dt_for_session(session_date), s), "x": bx, "y": by, "bt": btype})

    # ball positions
    for p in (payload.get("ball_positions") or []):
        s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
        hx = _float(p.get("x")); hy = _float(p.get("y"))
        conn.execute(sql_text("""
            INSERT INTO fact_ball_position (session_id, ts_s, ts, x, y)
            VALUES (:sid, :ss, :ts, :x, :y)
        """), {"sid": session_id, "ss": s, "ts": seconds_to_ts(_base_dt_for_session(session_date), s), "x": hx, "y": hy})

    # player positions
    for puid, arr in (payload.get("player_positions") or {}).items():
        pid = uid_to_player_id.get(str(puid))
        if not pid: continue
        for p in (arr or []):
            s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
            px = _float(p.get("court_X", p.get("court_x"))) if ("court_X" in p or "court_x" in p) else _float(p.get("X", p.get("x")))
            py = _float(p.get("court_Y", p.get("court_y"))) if ("court_Y" in p or "court_y" in p) else _float(p.get("Y", p.get("y")))
            conn.execute(sql_text("""
                INSERT INTO fact_player_position (session_id, player_id, ts_s, ts, x, y)
                VALUES (:sid, :pid, :ss, :ts, :x, :y)
            """), {"sid": session_id, "pid": pid, "ss": s,
                   "ts": seconds_to_ts(_base_dt_for_session(session_date), s), "x": px, "y": py})

    # swings
    seen = set()
    def _seen_key(pid, norm):
        if norm.get("suid"): return ("suid", str(norm["suid"]))
        return ("fb", pid, _quantize_time(norm.get("start_s"), fps), _quantize_time(norm.get("end_s"), fps))

    for norm in _gather_all_swings(payload):
        pid = uid_to_player_id.get(str(norm.get("player_uid") or "")) if norm.get("player_uid") else None
        k = _seen_key(pid, norm)
        if k in seen: continue
        seen.add(k)
        s = {
            "suid": str(norm.get("suid")) if norm.get("suid") else None,
            "start_s": norm.get("start_s"), "end_s": norm.get("end_s"),
            "ball_hit_s": norm.get("ball_hit_s"),
            "ball_hit_x": norm.get("ball_hit_x"), "ball_hit_y": norm.get("ball_hit_y"),
            "ball_speed": norm.get("ball_speed") or (norm.get("meta") or {}).get("ball_speed"),
            "ball_player_distance": norm.get("ball_player_distance"),
            "swing_type": norm.get("swing_type") or norm.get("label"),
            "volley": norm.get("volley"), "is_in_rally": norm.get("is_in_rally"),
            "serve": norm.get("serve"), "serve_type": norm.get("serve_type"),
            "confidence_swing_type": norm.get("confidence_swing_type"),
            "confidence": norm.get("confidence"),
            "confidence_volley": norm.get("confidence_volley"),
            "meta": norm.get("meta"),
        }
        try: _insert_swing(conn, session_id, pid, s, session_date, fps)
        except IntegrityError: pass

    if not had_payload_rallies: _ensure_rallies_from_swings(conn, session_id, gap_s=RALLY_GAP_S)
    _link_swings_to_rallies(conn, session_id)
    _normalize_serve_flags(conn, session_id)
    _rebuild_ts_from_seconds(conn, session_id)

    # --- OPTIONAL TOWERS (additive; only if present in payload) ---
    try:
        # Some providers use plural/singular or alternative keys.
        # We read from the root payload and let the helpers pick what they need.
        upsert_session_confidences(conn, session_id, payload)  # expects payload or payload['confidences']
    except Exception as e:
        _log.warning(f"[optional] confidences skipped: {e}")

    try:
        upsert_thumbnail(conn, session_id, payload)  # expects payload or payload['thumbnails']
    except Exception as e:
        _log.warning(f"[optional] thumbnails skipped: {e}")

    try:
        insert_highlights(conn, session_id, payload)  # expects payload or payload['highlights']
    except Exception as e:
        _log.warning(f"[optional] highlights skipped: {e}")

    try:
        insert_team_sessions(conn, session_id, payload)  # expects payload or payload['team_sessions']
    except Exception as e:
        _log.warning(f"[optional] team_sessions skipped: {e}")

    try:
        upsert_bounce_heatmap(conn, session_id, payload)  # expects payload or payload['bounce_heatmap']
    except Exception as e:
        _log.warning(f"[optional] bounce_heatmap skipped: {e}")

    return {"session_uid": session_uid, "session_id": session_id}



# ---------- SportAI helpers & new endpoints ----------
def _iter_status_urls(task_id: str):
    for base in SPORT_AI_BASES:
        for path in SPORT_AI_STATUS_PATHS:
            yield f"{base.rstrip('/')}/{path.lstrip('/').format(task_id=task_id)}"

def _sportai_fetch_task(task_id: str) -> dict:
    """
    Server-side call to SportAI to fetch task status + (maybe) payload/result_url.
    Returns dict: {status, result_url, data, raw}
    """
    if not SPORT_AI_TOKEN:
        return {"status": "error", "error": "SPORT_AI_TOKEN not set"}

    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
    last_err = None
    j = None

    for url in _iter_status_urls(task_id):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code >= 500:
                last_err = f"{url} -> {r.status_code}: {r.text}"
                continue
            r.raise_for_status()
            j = r.json()
            break
        except Exception as e:
            last_err = str(e)

    if j is None:
        raise RuntimeError(f"SportAI status failed: {last_err}")

    d = j.get("data", j)
    status = d.get("status") or d.get("task_status") or j.get("status") or "unknown"
    result_url = d.get("result_url") or j.get("result_url")
    return {"status": status, "result_url": result_url, "data": d, "raw": j}

def _attach_submission_context(conn, task_id: str, session_id: int):
    """
    If submission_context exists, link the session_id and copy a lean version
    onto dim_session.meta under {'task_id', 'submission_context'}.
    """
    # Check table exists
    exists = conn.execute(sql_text("SELECT to_regclass('public.submission_context') IS NOT NULL")).scalar_one()
    if not exists:
        return

    # Ensure session_id column exists
    conn.execute(sql_text("""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='submission_context' AND column_name='session_id'
          ) THEN
            ALTER TABLE submission_context ADD COLUMN session_id INT;
          END IF;
        END $$;
    """))

    # Link and enrich
    row = conn.execute(sql_text("SELECT * FROM submission_context WHERE task_id=:t LIMIT 1"),
                       {"t": task_id}).mappings().first()
    if not row:
        return

    conn.execute(sql_text("UPDATE submission_context SET session_id=:sid WHERE task_id=:t"),
                 {"sid": session_id, "t": task_id})

    keep = ["email","customer_name","match_date","start_time","location",
            "player_a_name","player_b_name","player_a_utr","player_b_utr",
            "video_url","share_url"]
    sc = {k: row[k] for k in keep if k in row and row[k] is not None}

    conn.execute(sql_text("""
        UPDATE dim_session
           SET meta = COALESCE(meta, '{}'::jsonb)
                    || jsonb_build_object('task_id', :tid)
                    || jsonb_build_object('submission_context', CAST(:sc AS JSONB))
         WHERE session_id = :sid
    """), {"sid": session_id, "tid": task_id, "sc": json.dumps(sc)})

def _ingest_payload_and_counts(payload: dict, replace: bool, src_hint: str = None, task_id: str = None):
    with engine.begin() as conn:
        res = ingest_result_v2(conn, payload, replace=replace, forced_uid=None, src_hint=src_hint)
        sid = res.get("session_id")

        if task_id:
            _attach_submission_context(conn, task_id=task_id, session_id=sid)

        counts = conn.execute(sql_text("""
            SELECT
              (SELECT COUNT(*) FROM dim_rally            WHERE session_id=:sid),
              (SELECT COUNT(*) FROM fact_bounce          WHERE session_id=:sid),
              (SELECT COUNT(*) FROM fact_ball_position   WHERE session_id=:sid),
              (SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid),
              (SELECT COUNT(*) FROM fact_swing           WHERE session_id=:sid)
        """), {"sid": sid}).fetchone()
    return {
        "session_uid": res.get("session_uid"),
        "session_id": sid,
        "bronze_counts": {
            "rallies": counts[0], "ball_bounces": counts[1],
            "ball_positions": counts[2], "player_positions": counts[3],
            "swings": counts[4]
        }
    }

@ingest_bp.get("/ops/task-status")
def ops_task_status():
    """Secure, normalized status check (server hits SportAI with secret)."""
    if not _guard(): return _forbid()
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"ok": False, "error": "missing task_id"}), 400
    try:
        info = _sportai_fetch_task(task_id)
        has_inline_payload = isinstance(info.get("data"), dict) and any(
            k in info["data"] for k in ("players","swings","ball_positions","player_positions","ball_bounces","rallies")
        )
        return jsonify({"ok": True, "status": info["status"], "result_url": info["result_url"], "has_inline_payload": has_inline_payload})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@ingest_bp.route("/ops/ingest-task", methods=["POST"])
def ops_ingest_task():
    """Given a SportAI task_id, fetch status -> download result JSON -> ingest raw+bronze."""
    if not _guard(): return _forbid()
    task_id = request.values.get("task_id")
    rep_in = request.values.get("replace")
    replace = DEFAULT_REPLACE_ON_INGEST if rep_in is None else str(rep_in).lower() in ("1","true","yes","y","on")
    if not task_id:
        return jsonify({"ok": False, "error": "missing task_id"}), 400
    try:
        info = _sportai_fetch_task(task_id)
        status = (info.get("status") or "").lower()
        payload = None; src_hint = None
        if info.get("result_url"):
            src_hint = info["result_url"]
            r = requests.get(info["result_url"], timeout=90)
            r.raise_for_status()
            payload = r.json()
        elif isinstance(info.get("data"), dict) and any(k in info["data"] for k in ("players","swings","ball_positions","player_positions","ball_bounces","rallies")):
            payload = info["data"]

        if payload:
            res = _ingest_payload_and_counts(payload, replace=replace, src_hint=src_hint, task_id=task_id)
            return jsonify({"ok": True, "status": status or "completed", "result_url": info.get("result_url"), **res})
        else:
            return jsonify({"ok": True, "status": status or "pending", "result_url": info.get("result_url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@ingest_bp.post("/webhook/sportai")
def sportai_webhook():
    """
    Optional webhook receiver. Configure SportAI to POST {task_id, status, result_url}.
    If SPORTAI_WEBHOOK_SECRET is set, require it via ?secret=... or header X-Webhook-Secret.
    """
    secret = request.args.get("secret") or request.headers.get("X-Webhook-Secret")
    if SPORTAI_WEBHOOK_SECRET and secret != SPORTAI_WEBHOOK_SECRET:
        return _forbid()
    data = request.get_json(silent=True) or {}
    task_id    = data.get("task_id") or data.get("id")
    status     = (data.get("status") or "").lower()
    result_url = data.get("result_url")

    if not task_id:
        return jsonify({"ok": False, "error": "missing task_id"}), 400

    try:
        payload = None; src_hint = None
        if result_url:
            src_hint = result_url
            r = requests.get(result_url, timeout=90)
            r.raise_for_status()
            payload = r.json()
        elif data.get("data") and isinstance(data["data"], dict):
            payload = data["data"]

        if AUTO_INGEST_ON_COMPLETE and (status in ("done","completed","finished","success","succeeded") or payload):
            if not payload:
                info = _sportai_fetch_task(task_id)
                if info.get("result_url"):
                    src_hint = info["result_url"]
                    r = requests.get(info["result_url"], timeout=90)
                    r.raise_for_status()
                    payload = r.json()
                elif isinstance(info.get("data"), dict):
                    payload = info["data"]

            if payload:
                res = _ingest_payload_and_counts(payload, replace=DEFAULT_REPLACE_ON_INGEST, src_hint=src_hint, task_id=task_id)
                return jsonify({"ok": True, "ingested": True, "status": status or "completed", "result_url": result_url, **res})
        return jsonify({"ok": True, "ingested": False, "status": status, "result_url": result_url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------- existing OPS endpoints (minor tweaks noted) ----------
@ingest_bp.get("/ops/db-ping")
def db_ping():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        now = conn.execute(sql_text("SELECT now() AT TIME ZONE 'utc'")).scalar_one()
    return jsonify({"ok": True, "now_utc": str(now)})

@ingest_bp.route("/ops/ingest-file", methods=["GET","POST"])
def ops_ingest_file():
    if not _guard(): return _forbid()
    if request.method == "GET":
        key = request.args.get("key") or request.args.get("ops_key") or ""
        checked = " checked" if DEFAULT_REPLACE_ON_INGEST else ""
        html = f"""<!doctype html><html><body>
        <h3>Upload SportAI Session JSON</h3>
        <form action="/ops/ingest-file?key={key}" method="post" enctype="multipart/form-data">
          <p><input type="file" name="file" accept="application/json"></p>
          <p>or URL: <input name="url" size="60" placeholder="https://.../session.json"></p>
          <p><label><input type="checkbox" name="replace" value="1"{checked}> Replace existing</label></p>
          <p>Session UID (optional): <input name="session_uid" size="40"></p>
          <button type="submit">Ingest</button>
        </form></body></html>"""
        return Response(html, mimetype="text/html")

    rep_in = request.form.get("replace")
    replace = DEFAULT_REPLACE_ON_INGEST if rep_in is None else str(rep_in).lower() in ("1","true","yes","y","on")
    forced_uid = request.form.get("session_uid") or None
    payload = None

    if "file" in request.files and request.files["file"].filename:
        try: payload = json.load(request.files["file"].stream)
        except Exception as e: return jsonify({"ok": False, "error": f"file not JSON: {e}"}), 400
    elif request.form.get("url"):
        url = request.form["url"]
        try:
            # normalize dropbox links if present
            if "dropbox.com" in url and "dl=" in url:
                from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
                p = urlparse(url); q = dict(parse_qsl(p.query, keep_blank_values=True)); q["dl"] = "1"
                host = "dl.dropboxusercontent.com" if "dropbox.com" in p.netloc else p.netloc
                url = urlunparse((p.scheme, host, p.path, p.params, urlencode(q), p.fragment))
            r = requests.get(url, timeout=90); r.raise_for_status(); payload = r.json()
        except Exception as e: return jsonify({"ok": False, "error": f"failed to fetch URL: {e}"}), 400
    else:
        try: payload = request.get_json(force=True, silent=False)
        except Exception: return jsonify({"ok": False, "error": "no JSON supplied"}), 400

    try:
        res = _ingest_payload_and_counts(payload, replace=replace, src_hint=request.form.get("url"))
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@ingest_bp.get("/ops/init-db")
def ops_init_db():
    if not _guard(): return _forbid()
    try:
        from db_init import run_init
        run_init(engine)
        return jsonify({"ok": True, "message": "DB initialized / migrated"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@ingest_bp.get("/ops/init-views")
def ops_init_views():
    if not _guard(): return _forbid()
    try:
        from db_views import run_views
        run_views(engine)
        return jsonify({"ok": True, "message": "Views created/refreshed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@ingest_bp.get("/ops/refresh-gold")
def ops_refresh_gold():
    if not _guard(): return _forbid()
    try:
        with engine.begin() as conn:
            conn.execute(sql_text("""
                DO $$ BEGIN
                  IF to_regclass('public.point_log_tbl') IS NULL THEN
                    CREATE TABLE point_log_tbl AS SELECT * FROM vw_point_log WHERE false;
                  END IF;
                END $$;"""))
            conn.execute(sql_text("TRUNCATE point_log_tbl;"))
            conn.execute(sql_text("INSERT INTO point_log_tbl SELECT * FROM vw_point_log;"))
        return jsonify({"ok": True, "message": "gold refreshed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@ingest_bp.get("/ops/db-counts")
def ops_db_counts():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        def c(tbl): return conn.execute(sql_text(f"SELECT COUNT(*) FROM {tbl}")).scalar_one()
        counts = {t: c(t) for t in [
            "dim_session","dim_player","dim_rally","fact_swing","fact_bounce",
            "fact_ball_position","fact_player_position","raw_result"
        ]}
    return jsonify({"ok": True, "counts": counts})

@ingest_bp.get("/ops/db-rollup")
def ops_db_rollup():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT ds.session_uid, ds.session_id,
              (SELECT COUNT(*) FROM dim_player dp WHERE dp.session_id = ds.session_id) AS n_players_dim,
              (SELECT COUNT(*) FROM dim_rally  dr WHERE dr.session_id = ds.session_id) AS n_rallies_dim,
              (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id = ds.session_id) AS n_swings
            FROM dim_session ds ORDER BY ds.session_id DESC LIMIT 100
        """)).mappings().all()
    return jsonify({"ok": True, "rows": len(rows), "data": [dict(r) for r in rows]})

@ingest_bp.route("/ops/sql", methods=["GET","POST"])
def ops_sql():
    if not _guard(): return _forbid()
    q = None
    if request.method == "POST":
        if request.is_json: q = (request.get_json(silent=True) or {}).get("q")
        if not q: q = request.form.get("q")
    if not q: q = request.args.get("q", "")
    q = (q or "").strip(); ql = q.lstrip().lower()
    if not (ql.startswith("select") or ql.startswith("with")):
        return Response("Only SELECT/CTE queries are allowed", 400)
    if ";" in q[:-1]: return Response("Only a single statement is allowed", 400)
    if not re.search(r"\blimit\b", q, flags=re.IGNORECASE): q = f"{q.rstrip(';')}\nLIMIT 200"
    try:
        with engine.begin() as conn:
            conn.execute(sql_text("SET LOCAL statement_timeout = 60000"))
            conn.execute(sql_text("SET LOCAL TRANSACTION READ ONLY"))
            rows = conn.execute(sql_text(q)).mappings().all()
        return jsonify({"ok": True, "rows": len(rows), "data": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "query": q}), 400

@ingest_bp.get("/ops/inspect-raw")
def ops_inspect_raw():
    if not _guard(): return _forbid()
    session_uid = request.args.get("session_uid")
    if not session_uid: return jsonify({"ok": False, "error": "missing session_uid"}), 400
    with engine.connect() as conn:
        sid = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid=:u"), {"u": session_uid}).scalar()
        if not sid: return jsonify({"ok": False, "error": "unknown session_uid"}), 404
        row = conn.execute(sql_text("""
            SELECT payload_json, payload_gzip FROM raw_result
            WHERE session_id=:sid ORDER BY created_at DESC LIMIT 1
        """), {"sid": sid}).first()
    if not row: return jsonify({"ok": False, "error": "no raw_result for session"}), 404

    doc = None
    if row[0] is not None:
        if isinstance(row[0], str):
            try: doc = json.loads(row[0])
            except Exception: return jsonify({"ok": False, "error": "payload_json not valid JSON"}), 500
        else:
            doc = row[0]
    elif row[1] is not None:
        try:
            js = gzip.decompress(row[1]).decode("utf-8")
            doc = json.loads(js)
        except Exception as e:
            return jsonify({"ok": False, "error": f"failed to read gzip: {e}"}), 500
    else:
        return jsonify({"ok": False, "error": "raw_result had neither JSON nor GZIP"}), 500

    bp = doc.get("ball_positions"); bb = doc.get("ball_bounces"); pp = doc.get("player_positions")
    summary = {"keys": sorted(doc.keys() if isinstance(doc, dict) else []),
               "ball_positions_len": (len(bp) if isinstance(bp, list) else None),
               "ball_bounces_len":   (len(bb) if isinstance(bb, list) else None),
               "player_positions_players": (len(pp) if isinstance(pp, dict) else None)}
    return jsonify({"ok": True, "session_uid": session_uid, "summary": summary})
