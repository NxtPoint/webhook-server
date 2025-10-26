# ingest_bronze.py — Optimized v2 (JSONB bronze, gzip raw, submission_context-ready, player_swing exploded)

import os, json, gzip, hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Iterable

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

def _extract_session_id(payload: Dict[str, Any]) -> int:
    # Strict: only accept SportAI's session_id from common locations
    candidates = [
        payload.get("session_id"),
        (payload.get("data") or {}).get("session_id"),
        (payload.get("session") or {}).get("id"),
        (payload.get("session") or {}).get("session_id"),
    ]
    for c in candidates:
        if c is None: continue
        try:
            sid = int(str(c))
            if sid > 0:
                return sid
        except Exception:
            pass
    raise ValueError("SportAI session_id not found in payload")

def _as_list(v) -> Iterable:
    if v is None: return []
    if isinstance(v, list): return v
    return []

def _as_dict(v) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}

# --------------------- Schema bootstrap ---------------------
BRONZE_JSON_TABLES = [
    "player", "player_swing", "rally", "ball_position", "player_position", "ball_bounce",
    "session_confidences", "thumbnail", "highlight", "team_session",
    "bounce_heatmap", "unmatched_field", "debug_event"
]

def _run_bronze_init(conn) -> None:
    conn.execute(sql_text("""
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='bronze') THEN
        EXECUTE 'CREATE SCHEMA bronze';
      END IF;

      -- session (meta holder)
      IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='bronze' AND table_name='session'
      ) THEN
        EXECUTE 'CREATE TABLE bronze.session (
          session_id BIGINT PRIMARY KEY,
          meta JSONB,
          created_at TIMESTAMPTZ DEFAULT now()
        )';
      END IF;

      -- raw_result
      IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='bronze' AND table_name='raw_result'
      ) THEN
        EXECUTE 'CREATE TABLE bronze.raw_result (
          id BIGSERIAL PRIMARY KEY,
          session_id BIGINT NOT NULL,
          payload_json JSONB,
          payload_gzip BYTEA,
          payload_sha256 TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )';
      END IF;

      -- submission_context
      IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='bronze' AND table_name='submission_context'
      ) THEN
        EXECUTE 'CREATE TABLE bronze.submission_context (
          session_id BIGINT PRIMARY KEY,
          data JSONB
        )';
      END IF;
    END$$;
    """))

    for t in [
        "player","player_swing","rally","ball_position","player_position","ball_bounce",
        "session_confidences","thumbnail","highlight","team_session",
        "bounce_heatmap","unmatched_field","debug_event"
    ]:
        conn.execute(sql_text(f"""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='bronze' AND table_name='{t}'
          ) THEN
            EXECUTE 'CREATE TABLE bronze.{t} (session_id BIGINT, data JSONB)';
          END IF;
        END$$;
        """))

    # Create JSONB tables using a simple loop
    for t in BRONZE_JSON_TABLES:
        conn.execute(sql_text(f"""
            DO $$ BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='bronze' AND table_name='{t}'
              ) THEN
                EXECUTE 'CREATE TABLE bronze.{t} (session_id BIGINT, data JSONB)';
              END IF;
            END $$;
        """))

# --------------------- Raw persistence ----------------------
def _persist_raw(conn, session_id: int, payload: Dict[str, Any], size_threshold: int = 5_000_000) -> None:
    """
    Store raw payload: JSONB if small, else gzip+sha256 (JSONB stays NULL to save space).
    """
    js = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sha = _sha256_str(js)
    if len(js) <= size_threshold:
        try:
            conn.execute(sql_text("""
                INSERT INTO bronze.raw_result (session_id, payload_json, payload_sha256)
                VALUES (:sid, :j::jsonb, :sha)
            """), {"sid": session_id, "j": js, "sha": sha})
            return
        except Exception:
            pass
    gz = _gzip_bytes_str(js)
    conn.execute(sql_text("""
        INSERT INTO bronze.raw_result (session_id, payload_gzip, payload_sha256)
        VALUES (:sid, :gz, :sha)
    """), {"sid": session_id, "gz": gz, "sha": sha})

# --------------------- Submission context -------------------
def _attach_submission_context(conn, task_id: Optional[str], session_id: int) -> None:
    if not task_id:
        return
    # If public.submission_context exists, link + copy lean data into bronze
    exists = conn.execute(sql_text(
        "SELECT to_regclass('public.submission_context') IS NOT NULL"
    )).scalar_one()
    if not exists:
        return

    # ensure public.submission_context has session_id column
    conn.execute(sql_text("""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='submission_context' AND column_name='session_id'
          ) THEN
            ALTER TABLE public.submission_context ADD COLUMN session_id BIGINT;
          END IF;
        END $$;
    """))

    # link this task_id to current session_id if row found
    row = conn.execute(sql_text("""
        SELECT *
        FROM public.submission_context
        WHERE task_id = :tid
        LIMIT 1
    """), {"tid": task_id}).mappings().first()
    if not row:
        return

    conn.execute(sql_text("""
        UPDATE public.submission_context SET session_id = :sid WHERE task_id = :tid
    """), {"sid": session_id, "tid": task_id})

    keep = [
        "email","customer_name","match_date","start_time","location",
        "player_a_name","player_b_name","player_a_utr","player_b_utr",
        "video_url","share_url","task_id"
    ]
    sc = {k: row[k] for k in keep if k in row and row[k] is not None}
    sc["task_id"] = task_id

    # upsert into bronze.submission_context
    conn.execute(sql_text("""
        INSERT INTO bronze.submission_context (session_id, data)
        VALUES (:sid, :j::jsonb)
        ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
    """), {"sid": session_id, "j": json.dumps(sc)})

    # mirror a lean copy into bronze.session.meta
    conn.execute(sql_text("""
        INSERT INTO bronze.session (session_id, meta)
        VALUES (:sid, jsonb_build_object('submission_context', :j::jsonb))
        ON CONFLICT (session_id) DO UPDATE
        SET meta = COALESCE(bronze.session.meta, '{}'::jsonb)
                 || jsonb_build_object('submission_context', :j::jsonb)
    """), {"sid": session_id, "j": json.dumps(sc)})

# --------------------- JSONB inserts ------------------------
def _insert_json_array(conn, table: str, session_id: int, arr) -> int:
    """Insert a list of JSON objects into bronze.<table>(session_id, data)."""
    if not arr: return 0
    values = [{"sid": session_id, "j": json.dumps(x)} for x in arr if isinstance(x, dict)]
    if not values: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (session_id, data)
        SELECT :sid, :j::jsonb
    """), values)
    return len(values)

def _upsert_single(conn, table: str, session_id: int, obj) -> int:
    """Upsert a single JSONB object into a UNIQUE(session_id) table."""
    if obj is None: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (session_id, data)
        VALUES (:sid, :j::jsonb)
        ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
    """), {"sid": session_id, "j": json.dumps(obj)})
    return 1

def _ensure_bronze_session(conn, session_id: int, payload: Dict[str, Any]) -> None:
    # Add discoverable meta: minimal fingerprint and top-level keys
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
def ingest_bronze_strict(
    conn,
    payload: Dict[str, Any],
    replace: bool = True,
    forced_uid: Optional[str] = None,  # kept for API parity; unused here
    src_hint: Optional[str] = None,
    task_id: Optional[str] = None
) -> Dict[str, Any]:

    # 1) Resolve session_id from SportAI (strict)
    session_id = _extract_session_id(payload)

    # 2) Optionally clear prior rows for this session
    if replace:
        for t in BRONZE_JSON_TABLES + ["submission_context"]:
            conn.execute(sql_text(f"DELETE FROM bronze.{t} WHERE session_id = :sid"), {"sid": session_id})

    # 3) Persist raw payload snapshot (JSONB or gzip)
    _persist_raw(conn, session_id, payload)

    # 4) Ensure bronze.session meta exists
    _ensure_bronze_session(conn, session_id, payload)

    # 5) Ingest JSON → bronze JSONB tables
    players = _as_list(payload.get("players"))
    rallies = _as_list(payload.get("rallies"))
    ball_positions = _as_list(payload.get("ball_positions"))
    player_positions = _as_dict(payload.get("player_positions"))  # dict[str_uid] -> [positions]
    ball_bounces = _as_list(payload.get("ball_bounces"))
    confidences = payload.get("confidences")
    thumbnails = payload.get("thumbnails") or payload.get("thumbnail_crops")
    highlights = payload.get("highlights")
    team_sessions = payload.get("team_sessions")
    bounce_heatmap = payload.get("bounce_heatmap")
    unmatched = payload.get("unmatched") or payload.get("unmatched_fields")
    debug_events = payload.get("debug_events") or payload.get("events_debug")

    counts: Dict[str, int] = {}

    # 5a) players (raw player objects)
    counts["player"] = _insert_json_array(conn, "player", session_id, players)

    # 5b) player_swing (explode nested swings under each player)
    swing_rows = []
    for p in players:
        if not isinstance(p, dict): continue
        p_uid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        # typical keys for nested swings
        for key in ("swings", "strokes", "swing_events"):
            for s in _as_list(p.get(key)):
                if isinstance(s, dict):
                    s2 = dict(s)
                    s2["player_uid"] = p_uid
                    swing_rows.append(s2)
        # sometimes nested under player.statistics / stats
        stats = _as_dict(p.get("statistics") or p.get("stats"))
        for key in ("swings", "strokes", "swing_events"):
            for s in _as_list(stats.get(key)):
                if isinstance(s, dict):
                    s2 = dict(s)
                    s2["player_uid"] = p_uid
                    swing_rows.append(s2)

    counts["player_swing"] = _insert_json_array(conn, "player_swing", session_id, swing_rows)

    # 5c) rallies, ball, positions, etc.
    counts["rally"] = _insert_json_array(conn, "rally", session_id, rallies)
    counts["ball_position"] = _insert_json_array(conn, "ball_position", session_id, ball_positions)

    # normalize player_positions (dict of arrays) into array with player_uid
    pp_rows = []
    for puid, arr in player_positions.items():
        for obj in _as_list(arr):
            if isinstance(obj, dict):
                o = dict(obj)
                o["player_uid"] = str(puid)
                pp_rows.append(o)
    counts["player_position"] = _insert_json_array(conn, "player_position", session_id, pp_rows)

    counts["ball_bounce"] = _insert_json_array(conn, "ball_bounce", session_id, ball_bounces)
    counts["debug_event"] = _insert_json_array(conn, "debug_event", session_id, _as_list(debug_events))
    counts["unmatched_field"] = _insert_json_array(conn, "unmatched_field", session_id, _as_list(unmatched))

    # singletons
    counts["session_confidences"] = _upsert_single(conn, "session_confidences", session_id, confidences)
    counts["thumbnail"] = _upsert_single(conn, "thumbnail", session_id, thumbnails)
    counts["highlight"] = _upsert_single(conn, "highlight", session_id, highlights)
    counts["team_session"] = _upsert_single(conn, "team_session", session_id, team_sessions)
    counts["bounce_heatmap"] = _upsert_single(conn, "bounce_heatmap", session_id, bounce_heatmap)

    # 6) submission_context linking (if the caller passes task_id)
    _attach_submission_context(conn, task_id=task_id, session_id=session_id)

    return {"session_id": session_id, "counts": counts}

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
        task_id = body.get("task_id")  # optional, to link submission_context
        with engine.begin() as conn:
            _run_bronze_init(conn)
            out = ingest_bronze_strict(conn, payload, replace=replace, task_id=task_id, src_hint="api:ingest-json")
        return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 400

@ingest_bronze.post("/bronze/reingest-from-raw")
def http_bronze_reingest_from_raw():
    if not _guard(): return _forbid()
    body = request.get_json(silent=True) or {}
    try:
        sid = int(body.get("session_id"))
    except Exception:
        return jsonify({"ok": False, "error": "session_id required"}), 400
    replace = str(body.get("replace") or "true").lower() in ("1","true","yes","y")
    try:
        with engine.begin() as conn:
            _run_bronze_init(conn)
            row = conn.execute(sql_text("""
                SELECT payload_json, payload_gzip
                FROM bronze.raw_result
                WHERE session_id = :sid
                ORDER BY created_at DESC
                LIMIT 1
            """), {"sid": sid}).mappings().first()
            if not row:
                return jsonify({"ok": False, "error": f"no raw_result for session_id={sid}"}), 404
            if row["payload_json"] is not None:
                payload = row["payload_json"] if isinstance(row["payload_json"], dict) else json.loads(row["payload_json"])
            elif row["payload_gzip"] is not None:
                payload = json.loads(gzip.decompress(row["payload_gzip"]).decode("utf-8"))
            else:
                return jsonify({"ok": False, "error": "no payload_json or payload_gzip present"}), 500

            out = ingest_bronze_strict(conn, payload, replace=replace, src_hint="api:reingest-from-raw")
            return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

# this is to run indivudaul files for testing only - potential code we can kill later
@ingest_bronze.post("/bronze/reingest-by-task-id")
def http_bronze_reingest_by_task_id():
    """
    Body: {"task_id":"<uuid>", "replace":true, "scan_limit":400}
    Finds the raw_result row whose JSON (jsonb or gzip) contains the task_id,
    then re-ingests that payload into bronze for its session_id.
    """
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
                SELECT id, session_id, payload_json, payload_gzip, created_at
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

            # load payload
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
