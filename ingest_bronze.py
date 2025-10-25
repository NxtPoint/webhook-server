# ingest_bronze.py — clean stable ingest to bronze schema
import os, json, gzip, hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict

import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text
from db_init import engine

# -------------------------------------------------------
# Flask Blueprint
# -------------------------------------------------------
ingest_bronze = Blueprint("ingest_bronze", __name__)
OPS_KEY = os.getenv("OPS_KEY", "")

# -------------------------------------------------------
# Utilities
# -------------------------------------------------------
def _float(v):
    try:
        return float(v)
    except Exception:
        try:
            return float(str(v))
        except Exception:
            return None

def _time_s(v):
    if v is None: return None
    if isinstance(v, (int, float, str)): return _float(v)
    if isinstance(v, dict):
        for k in ("timestamp","timestamp_s","ts","time_s","t","seconds","s"):
            if k in v: return _float(v[k])
    return None

def seconds_to_ts(base_dt: datetime, s):
    if s is None: return None
    try:
        return base_dt + timedelta(seconds=float(s))
    except Exception:
        return None

def _base_dt_for_session(dt):
    return dt if dt else datetime(1970,1,1,tzinfo=timezone.utc)

def _forbid(): return Response("Forbidden", 403)
def _guard():
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("Authorization","").replace("Bearer ","").strip()
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

# -------------------------------------------------------
# Bronze Schema
# -------------------------------------------------------
def _run_bronze_init(conn):
    conn.execute(sql_text("""
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='bronze') THEN
        EXECUTE 'CREATE SCHEMA bronze';
      END IF;

      IF to_regclass('bronze.session') IS NULL THEN
        CREATE TABLE bronze.session (
          session_id BIGSERIAL PRIMARY KEY,
          session_uid TEXT UNIQUE NOT NULL,
          fps NUMERIC,
          session_date TIMESTAMPTZ,
          meta JSONB
        );
      END IF;

      IF to_regclass('bronze.player') IS NULL THEN
        CREATE TABLE bronze.player (
          player_id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          sportai_player_uid TEXT NOT NULL,
          full_name TEXT,
          handedness TEXT,
          age NUMERIC,
          utr NUMERIC,
          meta JSONB,
          UNIQUE (session_id, sportai_player_uid)
        );
      END IF;

      IF to_regclass('bronze.ball_bounce') IS NULL THEN
        CREATE TABLE bronze.ball_bounce (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          hitter_player_id INT REFERENCES bronze.player(player_id) ON DELETE SET NULL,
          bounce_s NUMERIC,
          bounce_ts TIMESTAMPTZ,
          x NUMERIC,
          y NUMERIC,
          bounce_type TEXT
        );
      END IF;

      IF to_regclass('bronze.ball_position') IS NULL THEN
        CREATE TABLE bronze.ball_position (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          ts_s NUMERIC,
          ts TIMESTAMPTZ,
          x NUMERIC,
          y NUMERIC
        );
      END IF;

      IF to_regclass('bronze.player_position') IS NULL THEN
        CREATE TABLE bronze.player_position (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          player_id INT REFERENCES bronze.player(player_id),
          ts_s NUMERIC,
          ts TIMESTAMPTZ,
          x NUMERIC,
          y NUMERIC
        );
      END IF;

      IF to_regclass('bronze.raw_result') IS NULL THEN
        CREATE TABLE bronze.raw_result (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          payload_json JSONB,
          payload_gzip BYTEA,
          created_at TIMESTAMPTZ DEFAULT now()
        );
      END IF;
    END$$;
    """))

# -------------------------------------------------------
# Ingest Logic
# -------------------------------------------------------
def ingest_bronze_strict(conn, payload: dict, replace=False, forced_uid=None, src_hint=None):
    """Pure extract from SportAI JSON to bronze tables"""
    # --- session
    session_uid = forced_uid or str(payload.get("session_uid") or payload.get("video_uid") or "unknown")
    fps = _float(payload.get("fps"))
    session_date = datetime.now(timezone.utc)
    conn.execute(sql_text("""
        INSERT INTO bronze.session (session_uid,fps,session_date,meta)
        VALUES (:u,:f,:d,CAST(:m AS JSONB))
        ON CONFLICT (session_uid) DO NOTHING
    """), {"u":session_uid,"f":fps,"d":session_date,"m":json.dumps(payload.get("meta") or {})})
    session_id = conn.execute(sql_text("SELECT session_id FROM bronze.session WHERE session_uid=:u"),{"u":session_uid}).scalar_one()

    if replace:
        for t in ("player","ball_bounce","ball_position","player_position"):
            conn.execute(sql_text(f"DELETE FROM bronze.{t} WHERE session_id=:sid"),{"sid":session_id})

    # --- save raw snapshot
    js = json.dumps(payload,separators=(",",":"))
    if len(js)<5_000_000:
        conn.execute(sql_text("INSERT INTO bronze.raw_result(session_id,payload_json) VALUES(:sid,CAST(:p AS JSONB))"),
                     {"sid":session_id,"p":js})
    else:
        conn.execute(sql_text("INSERT INTO bronze.raw_result(session_id,payload_gzip) VALUES(:sid,:gz)"),
                     {"sid":session_id,"gz":gzip.compress(js.encode())})

    # --- players
    uid_to_player_id = {}
    for p in payload.get("players") or []:
        puid = str(p.get("id") or p.get("sportai_player_uid") or "")
        if not puid: continue
        conn.execute(sql_text("""
            INSERT INTO bronze.player(session_id,sportai_player_uid,full_name,handedness,age,utr,meta)
            VALUES(:sid,:uid,:nm,:h,:a,:u,CAST(:m AS JSONB))
            ON CONFLICT (session_id,sportai_player_uid) DO NOTHING
        """),{"sid":session_id,"uid":puid,"nm":p.get("full_name"),"h":p.get("handedness"),
               "a":_float(p.get("age")),"u":_float(p.get("utr")),"m":json.dumps(p)})
        pid = conn.execute(sql_text("SELECT player_id FROM bronze.player WHERE session_id=:sid AND sportai_player_uid=:uid"),
                           {"sid":session_id,"uid":puid}).scalar_one()
        uid_to_player_id[puid]=pid

    # --- ball_bounces (pure)
    for b in payload.get("ball_bounces") or []:
        s=_time_s(b.get("timestamp")) or _time_s(b.get("ts"))
        bx=_float(b.get("x")); by=_float(b.get("y"))
        btype=b.get("type") or b.get("bounce_type")
        hitter_uid=b.get("player_id") or b.get("sportai_player_uid")
        pid=uid_to_player_id.get(str(hitter_uid))
        conn.execute(sql_text("""
            INSERT INTO bronze.ball_bounce(session_id,hitter_player_id,bounce_s,bounce_ts,x,y,bounce_type)
            VALUES(:sid,:pid,:s,:ts,:x,:y,:bt)
        """),{"sid":session_id,"pid":pid,"s":s,"ts":seconds_to_ts(_base_dt_for_session(session_date),s),
              "x":bx,"y":by,"bt":btype})

    # --- ball_positions
    for p in payload.get("ball_positions") or []:
        s=_time_s(p.get("timestamp")) or _time_s(p.get("ts"))
        x=_float(p.get("x")); y=_float(p.get("y"))
        conn.execute(sql_text("""
            INSERT INTO bronze.ball_position(session_id,ts_s,ts,x,y)
            VALUES(:sid,:ss,:ts,:x,:y)
        """),{"sid":session_id,"ss":s,"ts":seconds_to_ts(_base_dt_for_session(session_date),s),"x":x,"y":y})

    # --- player_positions
    for puid,arr in (payload.get("player_positions") or {}).items():
        pid=uid_to_player_id.get(str(puid))
        for p in arr or []:
            s=_time_s(p.get("timestamp")) or _time_s(p.get("ts"))
            x=_float(p.get("x")); y=_float(p.get("y"))
            conn.execute(sql_text("""
                INSERT INTO bronze.player_position(session_id,player_id,ts_s,ts,x,y)
                VALUES(:sid,:pid,:ss,:ts,:x,:y)
            """),{"sid":session_id,"pid":pid,"ss":s,"ts":seconds_to_ts(_base_dt_for_session(session_date),s),"x":x,"y":y})

    return {"ok":True,"session_uid":session_uid,"session_id":session_id}

# -------------------------------------------------------
# Endpoints
# -------------------------------------------------------
@ingest_bronze.post("/bronze/init")
def bronze_init():
    if not _guard(): return _forbid()
    with engine.begin() as conn: _run_bronze_init(conn)
    return jsonify({"ok":True,"message":"bronze schema ready"})

@ingest_bronze.post("/bronze/reingest-from-raw")
def bronze_reingest_from_raw():
    if not _guard(): return _forbid()
    data=request.get_json(silent=True) or request.form
    sid=int(data.get("session_id"))
    replace=str(data.get("replace","1")).lower() in ("1","true","yes","y","on")
    with engine.begin() as conn:
        row=conn.execute(sql_text("""
            SELECT session_uid,
                   (SELECT payload_json FROM bronze.raw_result WHERE session_id=s.session_id ORDER BY created_at DESC LIMIT 1),
                   (SELECT payload_gzip  FROM bronze.raw_result WHERE session_id=s.session_id ORDER BY created_at DESC LIMIT 1)
            FROM bronze.session s WHERE s.session_id=:sid
        """),{"sid":sid}).first()
        forced_uid,pj,gz=row[0],row[1],row[2]
        if pj is not None: payload=pj if isinstance(pj,dict) else json.loads(pj)
        elif gz is not None: payload=json.loads(gzip.decompress(gz).decode("utf-8"))
        else: return jsonify({"ok":False,"error":"no raw_result"}),404
        res=ingest_bronze_strict(conn,payload,replace=replace,forced_uid=forced_uid)
        return jsonify(res)
