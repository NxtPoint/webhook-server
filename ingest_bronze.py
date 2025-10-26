# ingest_bronze.py — clean, stable ingest to bronze schema (pure JSON -> bronze)

import os
import json
import gzip
import hashlib
from datetime import datetime, timezone, timedelta, date, time
from typing import Dict, Any, Iterable, Tuple, Optional

import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text

from db_init import engine  # reuse existing engine

# -------------------------------------------------------
# Flask Blueprint
# -------------------------------------------------------
ingest_bronze = Blueprint("ingest_bronze", __name__)
OPS_KEY = os.getenv("OPS_KEY", "")

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------
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

def _float(v):
    try:
        return float(v)
    except Exception:
        try:
            return float(str(v))
        except Exception:
            return None

def _boolish(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y"):
        return True
    if s in ("0", "false", "f", "no", "n"):
        return False
    return None

def _time_s(val):
    """Extract seconds from common timestamp shapes."""
    if val is None:
        return None
    if isinstance(val, (int, float, str)):
        return _float(val)
    if isinstance(val, dict):
        for k in ("timestamp", "timestamp_s", "ts", "time_s", "t", "seconds", "s"):
            if k in val:
                return _float(val[k])
    return None

def seconds_to_ts(base_dt: datetime, s):
    if s is None:
        return None
    try:
        return base_dt + timedelta(seconds=float(s))
    except Exception:
        return None

def _base_dt_for_session(dt):
    return dt if dt else datetime(1970, 1, 1, tzinfo=timezone.utc)

def _resolve_session_uid(payload, forced_uid=None, src_hint=None):
    if forced_uid:
        return str(forced_uid)
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_uid", "video_uid", "video_id"):
        if payload.get(k):
            return str(payload[k])
        if meta.get(k):
            return str(meta[k])
    fn = meta.get("file_name") or meta.get("filename")
    if not fn and src_hint:
        try:
            import os as _os
            fn = _os.path.splitext(_os.path.basename(src_hint))[0]
        except Exception:
            fn = None
    if fn:
        return str(fn)
    import hashlib as _hl, json as _json
    return f"sha1_{_hl.sha1(_json.dumps(payload, sort_keys=True, separators=(',',':')).encode()).hexdigest()[:12]}"

def _resolve_fps(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("fps", "frame_rate", "frames_per_second"):
        if payload.get(k) is not None:
            return _float(payload[k])
        if meta.get(k) is not None:
            return _float(meta[k])
    return None

def _resolve_session_date(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_date", "date", "recorded_at"):
        raw = payload.get(k) if k in payload else meta.get(k)
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                return None
    return None

def _coerce_date(v):
    if not v:
        return None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        pass
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _coerce_time(v):
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except Exception:
            pass
    # sometimes seconds float like "239.44"
    try:
        sec = float(s)
        hh = int(sec // 3600)
        mm = int((sec % 3600) // 60)
        ss = int(sec % 60)
        return time(hh, mm, ss)
    except Exception:
        return None

# -------------------------------------------------------
# Schema init (idempotent)
# -------------------------------------------------------
def _run_bronze_init(conn):
    conn.execute(sql_text("""
    DO $$
    BEGIN
      -- schema
      IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='bronze') THEN
        EXECUTE 'CREATE SCHEMA bronze';
      END IF;

      -- session
      IF to_regclass('bronze.session') IS NULL THEN
        CREATE TABLE bronze.session (
          session_id   BIGSERIAL PRIMARY KEY,
          session_uid  TEXT UNIQUE NOT NULL,
          fps          DOUBLE PRECISION,
          session_date TIMESTAMPTZ,
          meta         JSONB
        );
      END IF;

      -- player (SportAI-native fields only)
      IF to_regclass('bronze.player') IS NULL THEN
        CREATE TABLE bronze.player (
          player_id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          sportai_player_uid TEXT NOT NULL,
          covered_distance DOUBLE PRECISION,
          fastest_sprint DOUBLE PRECISION,
          fastest_sprint_ts_s DOUBLE PRECISION,
          activity_score DOUBLE PRECISION,
          swing_count INT,
          swing_type_distribution JSONB,
          location_heatmap JSONB,
          UNIQUE (session_id, sportai_player_uid)
        );
      END IF;

      -- swing (legacy/materialised metrics)
      IF to_regclass('bronze.swing') IS NULL THEN
        CREATE TABLE bronze.swing (
          swing_id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          player_id INT REFERENCES bronze.player(player_id) ON DELETE SET NULL,
          sportai_swing_uid TEXT,
          start_s DOUBLE PRECISION, end_s DOUBLE PRECISION, ball_hit_s DOUBLE PRECISION,
          start_ts TIMESTAMPTZ, end_ts TIMESTAMPTZ, ball_hit_ts TIMESTAMPTZ,
          ball_hit_x DOUBLE PRECISION, ball_hit_y DOUBLE PRECISION,
          ball_speed DOUBLE PRECISION, ball_player_distance DOUBLE PRECISION,
          swing_type TEXT, volley BOOLEAN, is_in_rally BOOLEAN, serve BOOLEAN, serve_type TEXT,
          meta JSONB,
          raw JSONB
        );
      END IF;

      -- player_swing (verbatim Swings object, 1:1 with JSON)
      IF to_regclass('bronze.player_swing') IS NULL THEN
        CREATE TABLE bronze.player_swing (
          swing_id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          player_id INT REFERENCES bronze.player(player_id) ON DELETE SET NULL,
          start JSONB,
          "end" JSONB,
          valid BOOLEAN,
          serve BOOLEAN,
          swing_type TEXT,
          volley BOOLEAN,
          is_in_rally BOOLEAN,
          rally DOUBLE PRECISION[],
          ball_hit JSONB,
          confidence_swing_type DOUBLE PRECISION,
          confidence DOUBLE PRECISION,
          confidence_volley DOUBLE PRECISION,
          ball_hit_location DOUBLE PRECISION[],
          ball_player_distance DOUBLE PRECISION,
          ball_speed DOUBLE PRECISION,
          ball_impact_location DOUBLE PRECISION[],
          ball_impact_type TEXT,
          intercepting_player_id INT,
          ball_trajectory JSONB,
          annotations JSONB
        );
        CREATE INDEX ON bronze.player_swing(session_id);
        CREATE INDEX ON bronze.player_swing(player_id);
      END IF;

      -- ball_position
      IF to_regclass('bronze.ball_position') IS NULL THEN
        CREATE TABLE bronze.ball_position (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          ts_s DOUBLE PRECISION, ts TIMESTAMPTZ, x DOUBLE PRECISION, y DOUBLE PRECISION
        );
      END IF;

      -- player_position
      IF to_regclass('bronze.player_position') IS NULL THEN
        CREATE TABLE bronze.player_position (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          player_id INT NOT NULL REFERENCES bronze.player(player_id) ON DELETE CASCADE,
          ts_s DOUBLE PRECISION, ts TIMESTAMPTZ, x DOUBLE PRECISION, y DOUBLE PRECISION
        );
      END IF;

      -- ball_bounce
      IF to_regclass('bronze.ball_bounce') IS NULL THEN
        CREATE TABLE bronze.ball_bounce (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          hitter_player_id INT REFERENCES bronze.player(player_id) ON DELETE SET NULL,
          bounce_s DOUBLE PRECISION, bounce_ts TIMESTAMPTZ,
          x DOUBLE PRECISION, y DOUBLE PRECISION,
          bounce_type TEXT
        );
      END IF;

      -- rally
      IF to_regclass('bronze.rally') IS NULL THEN
        CREATE TABLE bronze.rally (
          rally_id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          rally_number INT NOT NULL,
          start_s DOUBLE PRECISION, end_s DOUBLE PRECISION,
          start_ts TIMESTAMPTZ, end_ts TIMESTAMPTZ,
          UNIQUE (session_id, rally_number)
        );
      END IF;

      -- JSONB towers (verbatim)
      IF to_regclass('bronze.submission_context') IS NULL THEN
        CREATE TABLE bronze.submission_context (
          session_id INT PRIMARY KEY REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          data JSONB
        );
      END IF;

      -- flattened submission context for convenience
      IF to_regclass('bronze.submission_context_flat') IS NULL THEN
        CREATE TABLE bronze.submission_context_flat (
          session_id  INT PRIMARY KEY REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          email       TEXT,
          task_id     TEXT,
          location    TEXT,
          match_date  DATE,
          start_time  TIME,
          raw         JSONB
        );
      END IF;

      IF to_regclass('bronze.thumbnail') IS NULL THEN
        CREATE TABLE bronze.thumbnail (
          session_id INT PRIMARY KEY REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          data JSONB
        );
      END IF;

      IF to_regclass('bronze.highlight') IS NULL THEN
        CREATE TABLE bronze.highlight (
          session_id INT PRIMARY KEY REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          data JSONB
        );
      END IF;

      IF to_regclass('bronze.team_session') IS NULL THEN
        CREATE TABLE bronze.team_session (
          session_id INT PRIMARY KEY REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          data JSONB
        );
      END IF;

      IF to_regclass('bronze.bounce_heatmap') IS NULL THEN
        CREATE TABLE bronze.bounce_heatmap (
          session_id INT PRIMARY KEY REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          data JSONB
        );
      END IF;

      -- raw_result snapshot
      IF to_regclass('bronze.raw_result') IS NULL THEN
        CREATE TABLE bronze.raw_result (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          payload_json JSONB,
          payload_gzip BYTEA,
          payload_sha256 TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
      END IF;

      -- debug_event (verbatim debug_data)
      IF to_regclass('bronze.debug_event') IS NULL THEN
        CREATE TABLE bronze.debug_event (
          session_id INT PRIMARY KEY REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          data JSONB
        );
      END IF;

      -- unmatched (debugging)
      IF to_regclass('bronze.unmatched_field') IS NULL THEN
        CREATE TABLE bronze.unmatched_field (
          id BIGSERIAL PRIMARY KEY,
          session_id INT NOT NULL REFERENCES bronze.session(session_id) ON DELETE CASCADE,
          json_path TEXT NOT NULL,
          example_value JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
      END IF;
    END $$;"""))

# -------------------------------------------------------
# RAW snapshot saver + JSONB upsert helper
# -------------------------------------------------------
def _save_raw_result(conn, session_id: int, payload: dict, size_threshold: int = 5_000_000):
    js = json.dumps(payload, separators=(",", ":"))
    try_json = len(js) <= size_threshold
    if try_json:
        try:
            conn.execute(sql_text("""
                INSERT INTO bronze.raw_result (session_id, payload_json, created_at)
                VALUES (:sid, CAST(:p AS JSONB), now())
            """), {"sid": session_id, "p": js})
            return
        except Exception:
            pass
    gz = gzip.compress(js.encode("utf-8"))
    sha = hashlib.sha256(js.encode("utf-8")).hexdigest()
    conn.execute(sql_text("""
        INSERT INTO bronze.raw_result (session_id, payload_json, payload_gzip, payload_sha256, created_at)
        VALUES (:sid, NULL, :gz, :sha, now())
    """), {"sid": session_id, "gz": gz, "sha": sha})

def _upsert_jsonb(conn, table: str, session_id: int, data):
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (session_id, data)
        VALUES (:sid, CAST(:j AS JSONB))
        ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
    """), {"sid": session_id, "j": json.dumps(data)})

# -------------------------------------------------------
# Ingest (pure extract)
# -------------------------------------------------------
def ingest_bronze_strict(conn, payload: dict, replace=False, forced_uid=None, src_hint=None):
    # session identity + fps
    session_uid  = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
    fps          = _resolve_fps(payload)

    # pull submission_ctx early so we can use it for session_date
    submission_ctx = (
        payload.get("submission_context")
        or payload.get("submission")
        or (payload.get("meta") or {}).get("submission_context")
        or (payload.get("debug_data") or {}).get("submission_context")
    )

    # session_date priority: explicit -> submission_ctx -> team_sessions
    session_date = _resolve_session_date(payload)
    if not session_date and submission_ctx:
        sc_mdate = _coerce_date(submission_ctx.get("match_date"))
        sc_stime = _coerce_time(submission_ctx.get("start_time"))
        if sc_mdate or sc_stime:
            session_date = datetime.combine(sc_mdate or date(1970, 1, 1), sc_stime or time(0, 0, 0), tzinfo=timezone.utc)

    ts_block = payload.get("team_sessions") or (payload.get("debug_data") or {}).get("team_sessions") or []
    if not session_date and ts_block and isinstance(ts_block, list) and ts_block:
        ts0 = ts_block[0] or {}
        ts_date = _coerce_date(ts0.get("match_date"))
        ts_time = _coerce_time(ts0.get("start_time"))
        if ts_date or ts_time:
            session_date = datetime.combine(ts_date or date(1970, 1, 1), ts_time or time(0, 0, 0), tzinfo=timezone.utc)

    meta = payload.get("meta") or payload.get("metadata") or {}

    # insert session row
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
      tables = [
          "ball_position", "player_position", "ball_bounce", "swing", "rally", "player",
          "thumbnail", "highlight", "team_session", "bounce_heatmap",
          "player_swing", "debug_event", "submission_context", "submission_context_flat"
      ]
      for t in tables:
          exists = conn.execute(
              sql_text("SELECT to_regclass(:t)"),
              {"t": f"bronze.{t}"}
          ).scalar()
          if exists:
              conn.execute(sql_text(f"DELETE FROM bronze.{t} WHERE session_id=:sid"), {"sid": session_id})


    # raw save
    _save_raw_result(conn, session_id, payload)

    # ---------- PLAYERS (SportAI-native) ----------
    uid_to_player_id: Dict[str, int] = {}
    for p in (payload.get("players") or []):
        puid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        if not puid:
            conn.execute(sql_text("""
                INSERT INTO bronze.unmatched_field(session_id, json_path, example_value)
                VALUES (:sid, :p, CAST(:v AS JSONB))
            """), {"sid": session_id, "p": "players[*].<missing_uid>", "v": json.dumps(p)})
            continue

        conn.execute(sql_text("""
            INSERT INTO bronze.player (
              session_id, sportai_player_uid,
              covered_distance, fastest_sprint, fastest_sprint_ts_s, activity_score,
              swing_count, swing_type_distribution, location_heatmap
            ) VALUES (
              :sid, :puid,
              :cd, :fs, :fst, :ascore,
              :scount, CAST(:dist AS JSONB), CAST(:lheat AS JSONB)
            )
            ON CONFLICT (session_id, sportai_player_uid) DO UPDATE SET
              covered_distance = COALESCE(EXCLUDED.covered_distance, bronze.player.covered_distance),
              fastest_sprint = COALESCE(EXCLUDED.fastest_sprint, bronze.player.fastest_sprint),
              fastest_sprint_ts_s = COALESCE(EXCLUDED.fastest_sprint_ts_s, bronze.player.fastest_sprint_ts_s),
              activity_score = COALESCE(EXCLUDED.activity_score, bronze.player.activity_score),
              swing_count = COALESCE(EXCLUDED.swing_count, bronze.player.swing_count),
              swing_type_distribution = COALESCE(EXCLUDED.swing_type_distribution, bronze.player.swing_type_distribution),
              location_heatmap = COALESCE(EXCLUDED.location_heatmap, bronze.player.location_heatmap)
        """), {
            "sid": session_id, "puid": puid,
            "cd": _float(p.get("covered_distance") or (p.get("metrics") or {}).get("covered_distance")),
            "fs": _float(p.get("fastest_sprint") or (p.get("metrics") or {}).get("fastest_sprint")),
            "fst": _float(p.get("fastest_sprint_timestamp") or (p.get("metrics") or {}).get("fastest_sprint_timestamp_s")),
            "ascore": _float(p.get("activity_score") or (p.get("metrics") or {}).get("activity_score")),
            "scount": int(_float(p.get("swing_count"))) if p.get("swing_count") is not None else None,
            "dist": json.dumps(p.get("swing_type_distribution")) if p.get("swing_type_distribution") is not None else None,
            "lheat": json.dumps(p.get("location_heatmap") or p.get("heatmap")) if (p.get("location_heatmap") or p.get("heatmap")) is not None else None,
        })

        pid = conn.execute(sql_text("""
            SELECT player_id FROM bronze.player WHERE session_id=:sid AND sportai_player_uid=:puid
        """), {"sid": session_id, "puid": puid}).scalar_one()
        uid_to_player_id[puid] = pid

    # ---------- BALL BOUNCES ----------
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
            INSERT INTO bronze.ball_bounce (
              session_id, hitter_player_id, bounce_s, bounce_ts, x, y, bounce_type
            ) VALUES (
              :sid, :pid, :s, :ts, :x, :y, :bt
            )
        """), {"sid": session_id, "pid": hitter_pid, "s": s,
               "ts": seconds_to_ts(_base_dt_for_session(session_date), s),
               "x": bx, "y": by, "bt": btype})

    # ---------- BALL POSITIONS ----------
    for p in (payload.get("ball_positions") or []):
        s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
        bx = _float(p.get("x")); by = _float(p.get("y"))
        if bx is None or by is None:
            cp = p.get("court_pos") or p.get("court_position")
            if isinstance(cp, (list, tuple)) and len(cp) >= 2:
                bx, by = _float(cp[0]), _float(cp[1])
        if bx is None: bx = _float(p.get("court_x", p.get("court_X", p.get("X"))))
        if by is None: by = _float(p.get("court_y", p.get("court_Y", p.get("Y"))))
        conn.execute(sql_text("""
            INSERT INTO bronze.ball_position (session_id, ts_s, ts, x, y)
            VALUES (:sid, :ss, :ts, :x, :y)
        """), {"sid": session_id, "ss": s,
               "ts": seconds_to_ts(_base_dt_for_session(session_date), s),
               "x": bx, "y": by})

    # ---------- PLAYER POSITIONS ----------
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

    # ---------- SWING (legacy metrics) ----------
    def _emit_swing(obj, pid):
        raw_obj = json.dumps(obj)
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
              swing_type, volley, is_in_rally, serve, serve_type, meta, raw
            ) VALUES (
              :sid, :pid, :suid,
              :ss, :es, :bhs,
              :sts, :ets, :bhts,
              :bhx, :bhy, :bs, :bpd,
              :st, :vol, :inr, :srv, :srv_type, CAST(:meta AS JSONB), CAST(:raw AS JSONB)
            )
        """), {
            "sid": session_id, "pid": pid, "suid": obj.get("id") or obj.get("swing_uid") or obj.get("uid"),
            "ss": start_s, "es": end_s, "bhs": bh_s,
            "sts": seconds_to_ts(_base_dt_for_session(session_date), start_s),
            "ets": seconds_to_ts(_base_dt_for_session(session_date), end_s),
            "bhts": seconds_to_ts(_base_dt_for_session(session_date), bh_s),
            "bhx": _float(obj.get("ball_hit_location", {}).get("x")) if isinstance(obj.get("ball_hit_location"), dict) else bhx,
            "bhy": _float(obj.get("ball_hit_location", {}).get("y")) if isinstance(obj.get("ball_hit_location"), dict) else bhy,
            "bs": _float(obj.get("ball_speed")),
            "bpd": _float(obj.get("ball_player_distance")),
            "st": (str(obj.get("swing_type") or obj.get("type") or obj.get("label") or obj.get("stroke_type") or "")).lower(),
            "vol": _boolish(obj.get("volley")),
            "inr": _boolish(obj.get("is_in_rally")),
            "srv": _boolish(obj.get("serve")),
            "srv_type": obj.get("serve_type"),
            "meta": json.dumps({k:v for k,v in obj.items() if k not in {
                "id","uid","swing_uid","player_id","sportai_player_uid","player_uid",
                "start","start_s","start_ts","end","end_s","end_ts","timestamp","ts","time_s","t",
                "ball_hit","ball_hit_timestamp","ball_hit_ts","ball_hit_s","ball_hit_location",
                "type","label","stroke_type","swing_type","volley","is_in_rally","serve","serve_type",
                "ball_speed","ball_player_distance"
            }}),
            "raw": raw_obj
        })

    # ---------- PLAYER_SWING (verbatim + dedupe) ----------
    seen = set()

    def _swing_key(obj, pid):
        suid = obj.get("id") or obj.get("swing_uid") or obj.get("uid")
        if suid:
            return ("uid", str(suid))
        return ("pt", pid, _time_s(obj.get("start") or obj.get("start_s") or obj.get("start_ts")),
                      _time_s(obj.get("end")   or obj.get("end_s")   or obj.get("end_ts")))

    def _emit_player_swing(obj, pid):
        start_obj = obj.get("start")
        end_obj   = obj.get("end")
        ball_hit  = obj.get("ball_hit")

        rally_arr             = obj.get("rally") or None
        ball_hit_location_arr = obj.get("ball_hit_location") or None
        ball_impact_location  = obj.get("ball_impact_location") or None
        ball_trajectory_arr   = obj.get("ball_trajectory") or None
        annotations_arr       = obj.get("annotations") or None

        conn.execute(sql_text("""
          INSERT INTO bronze.player_swing (
              session_id, player_id,
              start, "end", valid, serve, swing_type, volley, is_in_rally, rally,
              ball_hit, confidence_swing_type, confidence, confidence_volley,
              ball_hit_location, ball_player_distance, ball_speed,
              ball_impact_location, ball_impact_type, intercepting_player_id,
              ball_trajectory, annotations
          ) VALUES (
              :sid, :pid,
              CAST(:start AS JSONB), CAST(:end AS JSONB), :valid, :serve, :stype, :volley, :inr, :rally,
              CAST(:ball_hit AS JSONB), :conf_st, :conf, :conf_v,
              :ball_hit_loc, :bpd, :bs,
              :ball_impact_loc, :impact_type, :inter_pid,
              CAST(:ball_traj AS JSONB), CAST(:ann AS JSONB)
          )
        """), {
            "sid": session_id, "pid": pid,
            "start": json.dumps(start_obj) if start_obj is not None else None,
            "end":   json.dumps(end_obj)   if end_obj   is not None else None,
            "valid": _boolish(obj.get("valid")),
            "serve": _boolish(obj.get("serve")),
            "stype": (obj.get("swing_type") or None),
            "volley": _boolish(obj.get("volley")),
            "inr": _boolish(obj.get("is_in_rally")),
            "rally": list(rally_arr) if isinstance(rally_arr, (list, tuple)) else None,
            "ball_hit": json.dumps(ball_hit) if ball_hit is not None else None,
            "conf_st": _float(obj.get("confidence_swing_type")),
            "conf":    _float(obj.get("confidence")),
            "conf_v":  _float(obj.get("confidence_volley")),
            "ball_hit_loc": list(ball_hit_location_arr) if isinstance(ball_hit_location_arr, (list, tuple)) else None,
            "bpd": _float(obj.get("ball_player_distance")),
            "bs":  _float(obj.get("ball_speed")),
            "ball_impact_loc": list(ball_impact_location) if isinstance(ball_impact_location, (list, tuple)) else None,
            "impact_type": obj.get("ball_impact_type"),
            "inter_pid": int(obj.get("intercepting_player_id")) if str(obj.get("intercepting_player_id") or "").isdigit() else None,
            "ball_traj": json.dumps(ball_trajectory_arr) if isinstance(ball_trajectory_arr, (list, tuple, dict)) else None,
            "ann":       json.dumps(annotations_arr)     if isinstance(annotations_arr,     (list, tuple, dict)) else None,
        })

    # nested players[*].swings (authoritative)
    for p in (payload.get("players") or []):
        pid = uid_to_player_id.get(str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or ""))
        for s in (p.get("swings") or []):
            k = _swing_key(s, pid)
            if k in seen:
                continue
            seen.add(k)
            _emit_player_swing(s, pid)
            _emit_swing(s, pid)

    # root swings[] (dedupe)
    for s in (payload.get("swings") or []):
        suid = s.get("player_id") or s.get("sportai_player_uid") or s.get("player_uid")
        pid = uid_to_player_id.get(str(suid)) if suid is not None else None
        k = _swing_key(s, pid)
        if k in seen:
            continue
        seen.add(k)
        _emit_player_swing(s, pid)
        _emit_swing(s, pid)

    # ---------- RALLIES ----------
    payload_rallies = payload.get("rallies") or []
    for i, r in enumerate(payload_rallies, start=1):
        if isinstance(r, dict):
            start_s = _time_s(r.get("start_ts")) or _time_s(r.get("start"))
            end_s   = _time_s(r.get("end_ts"))   or _time_s(r.get("end"))
        else:
            try:
                start_s, end_s = _float(r[0]), _float(r[1])
            except Exception:
                start_s, end_s = None, None
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

    # ---------- JSONB towers ----------
    if payload.get("debug_data") is not None:
        _upsert_jsonb(conn, "debug_event", session_id, payload["debug_data"])

    if payload.get("confidences") is not None:
        _upsert_jsonb(conn, "session_confidences", session_id, payload["confidences"])

    thumbs = payload.get("thumbnails") or payload.get("thumbnail_crops")
    if thumbs is not None:
        _upsert_jsonb(conn, "thumbnail", session_id, thumbs)

    if payload.get("highlights") is not None:
        _upsert_jsonb(conn, "highlight", session_id, payload["highlights"])

    if payload.get("team_sessions") is not None:
        _upsert_jsonb(conn, "team_session", session_id, payload["team_sessions"])

    bounce_heatmap = payload.get("bounce_heatmap") or payload.get("bounce_heatmaps")
    if bounce_heatmap is not None:
        _upsert_jsonb(conn, "bounce_heatmap", session_id, bounce_heatmap)

    # Submission context (verbatim + flattened)
    if submission_ctx is not None:
        _upsert_jsonb(conn, "submission_context", session_id, submission_ctx)

        sc_email = submission_ctx.get("email")
        sc_task  = submission_ctx.get("task_id")
        sc_loc   = submission_ctx.get("location")
        sc_mdate = _coerce_date(submission_ctx.get("match_date"))
        sc_stime = _coerce_time(submission_ctx.get("start_time"))

        conn.execute(sql_text("""
            INSERT INTO bronze.submission_context_flat
              (session_id, email, task_id, location, match_date, start_time, raw)
            VALUES
              (:sid, :email, :task, :loc, :mdate, :stime, CAST(:raw AS JSONB))
            ON CONFLICT (session_id) DO UPDATE SET
              email      = EXCLUDED.email,
              task_id    = EXCLUDED.task_id,
              location   = EXCLUDED.location,
              match_date = EXCLUDED.match_date,
              start_time = EXCLUDED.start_time,
              raw        = EXCLUDED.raw
        """), {
            "sid": session_id,
            "email": sc_email, "task": sc_task, "loc": sc_loc,
            "mdate": sc_mdate, "stime": sc_stime,
            "raw": json.dumps(submission_ctx),
        })

    # unmatched top-level keys
    known = {
        "players","swings","rallies","ball_bounces","ball_positions","player_positions",
        "confidences","thumbnails","thumbnail_crops","highlights","team_sessions","bounce_heatmap",
        "meta","metadata","session_uid","submission_context","submission","video_uid","video_id","fps","frame_rate","frames_per_second",
        "session_date","date","recorded_at","debug_data"
    }
    for k, v in (payload.items() if isinstance(payload, dict) else []):
        if k not in known:
            conn.execute(sql_text("""
              INSERT INTO bronze.unmatched_field(session_id, json_path, example_value)
              VALUES (:sid, :p, CAST(:v AS JSONB))
            """), {"sid": session_id, "p": k, "v": json.dumps(v)})
    # inside ingest_bronze_strict(...)
    sync_submission_context(session_id)
    return {"ok": True, "session_uid": session_uid, "session_id": session_id}

# -------------------------------------------------------
# Endpoints
# -------------------------------------------------------
@ingest_bronze.get("/bronze/health")
def bronze_health():
    if not _guard(): return _forbid()
    return jsonify({"ok": True, "service": "bronze", "status": "ready"})

@ingest_bronze.post("/bronze/init")
def bronze_init():
    if not _guard(): return _forbid()
    with engine.begin() as conn:
        _run_bronze_init(conn)
    return jsonify({"ok": True, "message": "bronze schema ready"})

@ingest_bronze.post("/bronze/ingest-file")
def bronze_ingest_file():
    if not _guard(): return _forbid()
    replace = str(request.values.get("replace","1")).lower() in ("1","true","yes","y","on")
    forced_uid = request.values.get("session_uid") or None

    # payload
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
    try:
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

            if not row:
                return jsonify({"ok": False, "error": "unknown session_id"}), 404

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
            "rallies": counts[0], "ball_bounces": counts[1],
            "ball_positions": counts[2], "player_positions": counts[3], "swings": counts[4]
        }})
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print("[/bronze/reingest-from-raw] ERROR:", err)
        return jsonify({"ok": False, "error": str(e), "trace": err}), 500

# Debug helpers (safe, require key)
@ingest_bronze.get("/bronze/raw-dump")
def bronze_raw_dump():
    if not _guard(): return _forbid()
    sid = int(request.args.get("session_id"))
    with engine.begin() as conn:
        row = conn.execute(sql_text("""
            SELECT payload_json, payload_gzip
            FROM bronze.raw_result
            WHERE session_id=:sid
            ORDER BY created_at DESC
            LIMIT 1
        """), {"sid": sid}).first()
        if not row:
            return jsonify({"ok": False, "error": "no raw_result"}), 404
        pj, gz = row[0], row[1]
        if pj is not None:
            payload = pj if isinstance(pj, dict) else json.loads(pj)
        elif gz is not None:
            payload = json.loads(gzip.decompress(gz).decode("utf-8"))
        else:
            return jsonify({"ok": False, "error": "empty raw_result"}), 404
        keys = list(payload.keys())[:100]
        return jsonify({"ok": True, "top_level_keys": keys, "count": len(keys)})

# --- add to ingest_bronze.py ---
from sqlalchemy import text
from db_init import engine

def sync_submission_context(session_id: int) -> None:
    """
    Mirrors public.submission_context -> bronze.submission_context for the given session.
    - Resolves session_id via public.sc.session_id OR bronze.submission.task_id -> session_id
    - Upserts a single row per session_id with a stable JSON payload (volatile fields stripped)
    - Idempotent and safe to call multiple times during the ingest
    """
    ensure_sql = text("""
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_namespace WHERE nspname = 'bronze'
      ) THEN
        EXECUTE 'CREATE SCHEMA bronze';
      END IF;

      IF NOT EXISTS (
        SELECT 1
        FROM   information_schema.tables
        WHERE  table_schema = 'bronze'
        AND    table_name   = 'submission_context'
      ) THEN
        EXECUTE '
          CREATE TABLE bronze.submission_context (
            session_id INT PRIMARY KEY,
            data JSONB NOT NULL
          )';
      END IF;
    END$$;
    """)

    upsert_sql = text("""
    INSERT INTO bronze.submission_context (session_id, data)
    SELECT
      COALESCE(sc.session_id, s.session_id) AS session_id,
      to_jsonb(sc)
        - 'ingest_error'
        - 'ingest_started_at'
        - 'ingest_finished_at'
        - 'last_status'
        - 'last_status_at'
        - 'last_result_url'
    FROM public.submission_context sc
    LEFT JOIN bronze.submission s
      ON s.task_id = sc.task_id
    WHERE COALESCE(sc.session_id, s.session_id) = :sid
    ON CONFLICT (session_id) DO UPDATE
    SET data = EXCLUDED.data;
    """)

    with engine.begin() as cx:
        # lightweight safety: ensure target exists (no-op if already present)
        cx.execute(ensure_sql)
        # strictly session-scoped upsert
        cx.execute(upsert_sql, {"sid": session_id})
# --- end add ---
