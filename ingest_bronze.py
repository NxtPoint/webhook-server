# ingest_bronze.py — Task-ID canonical (bronze keyed by task_id), JSONB bronze, gzip raw,
# submission_context-ready, player_swing exploded w/o mutation

import os, json, gzip, hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Iterable

import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text

from db_init import engine  # reuse your existing SQLAlchemy Engine

ingest_bronze = Blueprint("ingest_bronze", __name__)
OPS_KEY = os.getenv("OPS_KEY", "").strip()

# -------------------------- Auth --------------------------
def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    sup = qk or hk
    return bool(OPS_KEY) and sup == OPS_KEY

def _forbid():
    return Response("Forbidden", 403)

# ------------------------ Utilities ------------------------
def _require_json() -> Dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("JSON body required")
    return body

def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _gzip_bytes_str(s: str) -> bytes:
    return gzip.compress(s.encode("utf-8"))

def _as_list(v) -> Iterable:
    if v is None: return []
    if isinstance(v, list): return v
    return []

def _as_dict(v) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}

    from typing import Optional, Dict, Any

def _extract_session_id(payload: Dict[str, Any]) -> Optional[int]:
    """Try to read SportAI's session id from common locations. Return int or None."""
    cand = [
        payload.get("session_id"),
        (payload.get("session") or {}).get("id"),
        (payload.get("session") or {}).get("session_id"),
        (payload.get("metadata") or {}).get("session_id"),
        payload.get("sessionId"),
        (payload.get("data") or {}).get("session_id"),
    ]
    for c in cand:
        if c is None:
            continue
        try:
            sid = int(str(c))
            if sid > 0:
                return sid
        except Exception:
            pass
    return None

def _compute_session_uid(task_id: Optional[str], payload: Dict[str, Any]) -> str:
    # prefer anything SportAI-like if present
    for k in ("session_uid","sessionUid","sessionUID"):
        v = (payload.get(k) or (payload.get("session") or {}).get(k) or (payload.get("metadata") or {}).get(k))
        if isinstance(v, str) and v.strip():
            return v.strip()

    # fallback: derive from task_id + payload hash (stable, readable)
    tid = (task_id or "")[:8] or "nosrc"
    ph = _sha256_str(json.dumps(payload, separators=(",",":"), ensure_ascii=False))[:10]
    return f"{tid}-{ph}"


# --------------------- Schema bootstrap ---------------------

# Tables that hold many JSON objects per task
BRONZE_JSON_ARRAY_TABLES = [
    "player", "player_swing", "rally", "ball_position", "ball_bounce",
    "unmatched_field", "debug_event"
]

# Tables that hold exactly ONE JSON object per task (upsert by task_id)
BRONZE_JSON_SINGLETON_TABLES = [
    "player_position", "session_confidences", "thumbnail",
    "highlight", "team_session", "bounce_heatmap"
]

ALL_BRONZE_TABLES = BRONZE_JSON_ARRAY_TABLES + BRONZE_JSON_SINGLETON_TABLES

def _ensure_table_has_task_id(conn, table: str):
    """
    Ensure bronze.<table> exists and has task_id TEXT and data JSONB.
    If table exists with only session_id, add task_id.
    """
    # Create table if missing
    conn.execute(sql_text(f"""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='bronze' AND table_name='{table}'
          ) THEN
            EXECUTE 'CREATE TABLE bronze.{table} (task_id TEXT, data JSONB)';
          END IF;
        END$$;
    """))
    # Add task_id if missing
    conn.execute(sql_text(f"""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='bronze' AND table_name='{table}' AND column_name='task_id'
          ) THEN
            EXECUTE 'ALTER TABLE bronze.{table} ADD COLUMN task_id TEXT';
          END IF;
        END$$;
    """))

def _run_bronze_init(conn) -> None:
    # schema
    conn.execute(sql_text("""
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='bronze') THEN
        EXECUTE 'CREATE SCHEMA bronze';
      END IF;

      -- session registry for quick meta (keyed by task_id now)
      IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='bronze' AND table_name='session'
      ) THEN
        EXECUTE 'CREATE TABLE bronze.session (
          task_id TEXT PRIMARY KEY,
          meta JSONB,
          created_at TIMESTAMPTZ DEFAULT now()
        )';
      END IF;

      -- raw_result keyed by task_id
      IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='bronze' AND table_name='raw_result'
      ) THEN
        EXECUTE 'CREATE TABLE bronze.raw_result (
          id BIGSERIAL PRIMARY KEY,
          task_id TEXT NOT NULL,
          payload_json JSONB,
          payload_gzip BYTEA,
          payload_sha256 TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )';
      END IF;

      -- submission_context keyed by task_id
      IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='bronze' AND table_name='submission_context'
      ) THEN
        EXECUTE 'CREATE TABLE bronze.submission_context (
          task_id TEXT PRIMARY KEY,
          data JSONB
        )';
      END IF;
    END$$;
    """))

    # Make sure all array/singleton tables exist and have task_id
    for t in ALL_BRONZE_TABLES:
        _ensure_table_has_task_id(conn, t)

# --------------------- Raw persistence ----------------------
def _persist_raw(conn, session_id: int, payload: Dict[str, Any], task_id: Optional[str], size_threshold: int = 5_000_000) -> None:
    js = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sha = _sha256_str(js)
    if len(js) <= size_threshold:
        try:
            conn.execute(sql_text("""
                INSERT INTO bronze.raw_result (session_id, task_id, payload_json, payload_sha256)
                VALUES (:sid, :tid, :j::jsonb, :sha)
            """), {"sid": session_id, "tid": task_id, "j": js, "sha": sha})
            return
        except Exception:
            pass
    gz = _gzip_bytes_str(js)
    conn.execute(sql_text("""
        INSERT INTO bronze.raw_result (session_id, task_id, payload_gzip, payload_sha256)
        VALUES (:sid, :tid, :gz, :sha)
    """), {"sid": session_id, "tid": task_id, "gz": gz, "sha": sha})


# --------------------- Submission context -------------------
def _attach_submission_context(conn, task_id: Optional[str]) -> None:
    if not task_id:
        return
    # If public.submission_context exists, copy lean data into bronze
    exists = conn.execute(sql_text(
        "SELECT to_regclass('public.submission_context') IS NOT NULL"
    )).scalar_one()
    if not exists:
        return

    keep = [
        "email","customer_name","match_date","start_time","location",
        "player_a_name","player_b_name","player_a_utr","player_b_utr",
        "video_url","share_url","task_id"
    ]
    row = conn.execute(sql_text("""
        SELECT *
        FROM public.submission_context
        WHERE task_id = :tid
        LIMIT 1
    """), {"tid": task_id}).mappings().first()

    sc = {"task_id": task_id}
    if row:
        for k in keep:
            if k in row and row[k] is not None:
                sc[k] = row[k]

    # upsert into bronze.submission_context
    conn.execute(sql_text("""
        INSERT INTO bronze.submission_context (task_id, data)
        VALUES (:tid, :j::jsonb)
        ON CONFLICT (task_id) DO UPDATE SET data = EXCLUDED.data
    """), {"tid": task_id, "j": json.dumps(sc)})

    # mirror into bronze.session.meta
    conn.execute(sql_text("""
        INSERT INTO bronze.session (task_id, meta)
        VALUES (:tid, jsonb_build_object('submission_context', :j::jsonb))
        ON CONFLICT (task_id) DO UPDATE
        SET meta = COALESCE(bronze.session.meta, '{}'::jsonb)
                 || jsonb_build_object('submission_context', :j::jsonb)
    """), {"tid": task_id, "j": json.dumps(sc)})

# --------------------- JSONB inserts ------------------------
def _insert_json_array(conn, table: str, session_id: int, arr) -> int:
    if not arr: return 0
    values = [{"sid": session_id, "j": json.dumps(x)} for x in arr if isinstance(x, dict)]
    if not values: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (session_id, data)
        SELECT :sid, :j::jsonb
    """), values)
    return len(values)

def _upsert_single(conn, table: str, session_id: int, obj) -> int:
    if obj is None: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (session_id, data)
        VALUES (:sid, :j::jsonb)
        ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
    """), {"sid": session_id, "j": json.dumps(obj)})
    return 1


def _ensure_bronze_session(conn, session_id: int, payload: Dict[str, Any]) -> None:
    meta = {
        "ingest_at": datetime.now(timezone.utc).isoformat(),
        "keys": list(payload.keys())[:50]
    }
    conn.execute(sql_text("""
        INSERT INTO bronze.session (session_id, meta)
        VALUES (:sid, :meta::jsonb)
        ON CONFLICT (session_id) DO UPDATE
        SET meta = COALESCE(bronze.session.meta, '{}'::jsonb) || :meta::jsonb
    """), {"sid": session_id, "meta": json.dumps(meta)})


# --------------------- Ingestion core -----------------------
def ingest_bronze_strict(conn, payload: Dict[str, Any], replace: bool = True,
                         forced_uid: Optional[str] = None, src_hint: Optional[str] = None,
                         task_id: Optional[str] = None) -> Dict[str, Any]:

    # task_id fallback if caller omitted it
    if not task_id:
        task_id = (
            payload.get("task_id")
            or (_as_dict(payload.get("metadata")).get("task_id") if isinstance(payload.get("metadata"), dict) else None)
        )
    if not task_id:
        raise ValueError("task_id is required")

    # ---- define & try-read session_id BEFORE using it ----
    session_id = _extract_session_id(payload)  # may return None

    # --- figure out if the DB has a NOT NULL session_uid column ---
    cols = conn.execute(sql_text("""
        SELECT column_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema='bronze' AND table_name='session'
    """)).mappings().all()
    has_session_uid = any(c["column_name"] == "session_uid" for c in cols)
    session_uid_required = any(c["column_name"] == "session_uid" and c["is_nullable"] == "NO" for c in cols)

    # allocate our own session row if SportAI didn't provide an id
    if not session_id:
        if session_uid_required:
            uid = _compute_session_uid(task_id, payload)
            sid = conn.execute(sql_text("""
                INSERT INTO bronze.session (session_uid, meta)
                VALUES (:uid, '{}'::jsonb)
                RETURNING session_id
            """), {"uid": uid}).scalar_one()
        else:
            sid = conn.execute(sql_text("""
                INSERT INTO bronze.session (meta)
                VALUES ('{}'::jsonb)
                RETURNING session_id
            """)).scalar_one()
        session_id = int(sid)


    # optional replace cleanup (by session_id)
    if replace:
        for t in ALL_BRONZE_TABLES + ["submission_context"]:
            conn.execute(sql_text(f"DELETE FROM bronze.{t} WHERE session_id = :sid"), {"sid": session_id})

    # 1) persist raw payload (note: function name has leading underscore)
    _persist_raw(conn, session_id, payload, task_id)

    # 2) ensure bronze.session meta exists (pass session_id, not task_id)
    _ensure_bronze_session(conn, session_id, payload)


    # 3) map JSON → bronze tables (bronze remains pristine)
    players = _as_list(payload.get("players"))
    rallies = _as_list(payload.get("rallies"))
    ball_positions = _as_list(payload.get("ball_positions"))
    ball_bounces = _as_list(payload.get("ball_bounces"))
    confidences = payload.get("confidences")
    thumbnails = payload.get("thumbnails") or payload.get("thumbnail_crops")
    highlights = payload.get("highlights")
    team_sessions = payload.get("team_sessions")
    bounce_heatmap = payload.get("bounce_heatmap")
    unmatched = payload.get("unmatched") or payload.get("unmatched_fields")
    debug_events = payload.get("debug_events") or payload.get("events_debug")

    counts: Dict[str, int] = {}
    counts["player"] = _insert_json_array(conn, "player", task_id, players)

    # player_swing: explode nested arrays, but DO NOT mutate objects
    swing_rows = []
    for p in players:
        if not isinstance(p, dict): continue
        for key in ("swings", "strokes", "swing_events"):
            for s in _as_list(p.get(key)):
                if isinstance(s, dict):
                    swing_rows.append(s)
        stats = _as_dict(p.get("statistics") or p.get("stats"))
        for key in ("swings", "strokes", "swing_events"):
            for s in _as_list(stats.get(key)):
                if isinstance(s, dict):
                    swing_rows.append(s)
    counts["player_swing"] = _insert_json_array(conn, "player_swing", task_id, swing_rows)

    counts["rally"] = _insert_json_array(conn, "rally", task_id, rallies)
    counts["ball_position"] = _insert_json_array(conn, "ball_position", task_id, ball_positions)
    counts["ball_bounce"] = _insert_json_array(conn, "ball_bounce", task_id, ball_bounces)
    counts["debug_event"] = _insert_json_array(conn, "debug_event", task_id, _as_list(debug_events))
    counts["unmatched_field"] = _insert_json_array(conn, "unmatched_field", task_id, _as_list(unmatched))

    # player_positions: store original dict AS-IS (one row per task)
    counts["player_position"] = _upsert_single(conn, "player_position", task_id, payload.get("player_positions"))

    # singletons
    counts["session_confidences"] = _upsert_single(conn, "session_confidences", task_id, confidences)
    counts["thumbnail"] = _upsert_single(conn, "thumbnail", task_id, thumbnails)
    counts["highlight"] = _upsert_single(conn, "highlight", task_id, highlights)
    counts["team_session"] = _upsert_single(conn, "team_session", task_id, team_sessions)
    counts["bounce_heatmap"] = _upsert_single(conn, "bounce_heatmap", task_id, bounce_heatmap)

    # 4) link submission_context
    _attach_submission_context(conn, task_id=task_id)

    return {"task_id": task_id, "counts": counts}

# -------------------------- Routes --------------------------
@ingest_bronze.get("/bronze/init")
def http_bronze_init():
    if not _guard(): return _forbid()
    try:
        with engine.begin() as conn:
            _run_bronze_init(conn)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

@ingest_bronze.post("/bronze/ingest-json")
def http_bronze_ingest_json():
    if not _guard(): return _forbid()
    try:
        body = _require_json()
        payload = body.get("payload") or body  # accepts raw payload or {"payload": {...}}
        replace = str(body.get("replace") or "true").lower() in ("1","true","yes","y")
        task_id = body.get("task_id")
        with engine.begin() as conn:
            _run_bronze_init(conn)
            out = ingest_bronze_strict(conn, payload, replace=replace, task_id=task_id, src_hint="api:ingest-json")
        return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 400

@ingest_bronze.post("/bronze/ingest-from-url")
def http_bronze_ingest_from_url():
    if not _guard(): return _forbid()
    body = request.get_json(silent=True) or {}
    url = body.get("result_url")
    replace = str(body.get("replace") or "true").lower() in ("1","true","yes","y")
    task_id = body.get("task_id")
    if not url:
        return jsonify({"ok": False, "error": "result_url required"}), 400
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        payload = r.json()
        with engine.begin() as conn:
            _run_bronze_init(conn)
            out = ingest_bronze_strict(conn, payload, replace=replace, task_id=task_id, src_hint="api:ingest-from-url")
        return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

@ingest_bronze.post("/bronze/reingest-from-raw")
def http_bronze_reingest_from_raw():
    if not _guard(): return _forbid()
    body = request.get_json(silent=True) or {}
    task_id = body.get("task_id")
    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400
    replace = str(body.get("replace") or "true").lower() in ("1","true","yes","y")
    try:
        with engine.begin() as conn:
            _run_bronze_init(conn)
            row = conn.execute(sql_text("""
                SELECT payload_json, payload_gzip
                FROM bronze.raw_result
                WHERE task_id = :tid
                ORDER BY created_at DESC
                LIMIT 1
            """), {"tid": task_id}).mappings().first()
            if not row:
                return jsonify({"ok": False, "error": f"no raw_result for task_id={task_id}"}), 404
            if row["payload_json"] is not None:
                payload = row["payload_json"] if isinstance(row["payload_json"], dict) else json.loads(row["payload_json"])
            elif row["payload_gzip"] is not None:
                payload = json.loads(gzip.decompress(row["payload_gzip"]).decode("utf-8"))
            else:
                return jsonify({"ok": False, "error": "no payload_json or payload_gzip present"}), 500

            out = ingest_bronze_strict(conn, payload, replace=replace, task_id=task_id, src_hint="api:reingest-from-raw")
            return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

# convenient test helper (scan recent raw_result if task_id unknown in payload)
@ingest_bronze.post("/bronze/reingest-by-task-id")
def http_bronze_reingest_by_task_id():
    if not _guard():
        return _forbid()
    body = request.get_json(silent=True) or {}
    task_id = body.get("task_id")
    replace = str(body.get("replace") or "true").lower() in ("1","true","yes","y")
    scan_limit = int(body.get("scan_limit") or 400)
    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    try:
        with engine.begin() as conn:
            _run_bronze_init(conn)
            rows = conn.execute(sql_text("""
                SELECT id, task_id, payload_json, payload_gzip, created_at
                FROM bronze.raw_result
                ORDER BY created_at DESC
                LIMIT :lim
            """), {"lim": scan_limit}).mappings().all()

            match = None
            for r in rows:
                pj = r["payload_json"]
                if pj is not None:
                    s = pj if isinstance(pj, str) else json.dumps(pj, separators=(",",":"))
                    if task_id in s:
                        match = r
                        break
                pgz = r["payload_gzip"]
                if pgz:
                    try:
                        txt = gzip.decompress(pgz).decode("utf-8", errors="ignore")
                        if task_id in txt:
                            match = r
                            break
                    except Exception:
                        pass

            if not match:
                return jsonify({"ok": True, "reingested": False, "reason": "task_id not found in recent raw_result", "scanned": len(rows)})

            if match["payload_json"] is not None:
                payload = match["payload_json"] if isinstance(match["payload_json"], dict) else json.loads(match["payload_json"])
            elif match["payload_gzip"] is not None:
                payload = json.loads(gzip.decompress(match["payload_gzip"]).decode("utf-8"))
            else:
                return jsonify({"ok": False, "error": "no payload_json or payload_gzip present for matched row"}), 500

            out = ingest_bronze_strict(conn, payload, replace=replace, task_id=task_id, src_hint="api:reingest-by-task-id")
            return jsonify({"ok": True, "reingested": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

