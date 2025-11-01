# ingest_bronze.py — Task-ID canonical bronze (continuity-safe, Nov 2025)
# Matches your previous drop's API and table shapes, with fixes:
#  - task_id is canonical (TEXT) — no session_id required
#  - bronze.raw_result stores JSONB (small) or GZIP (large) + sha256
#  - submission_context mirrored into bronze.submission_context and bronze.session.meta
#  - player_swing exploded WITHOUT mutating original swing objects
#  - Back-compat shims: _run_bronze_init() (no-arg) and alias routes kept

import os, json, gzip, hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Iterable, List

import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text

from db_init import engine

ingest_bronze = Blueprint("ingest_bronze", __name__)
OPS_KEY = os.getenv("OPS_KEY", "").strip()

# -------------------------- Auth --------------------------
def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    # If OPS_KEY unset (local), allow through; else require match
    return (not OPS_KEY) or supplied == OPS_KEY

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

def _gzip_bytes(s: str) -> bytes:
    return gzip.compress(s.encode("utf-8"))

def _as_list(v) -> List[Any]:
    if v is None: return []
    return v if isinstance(v, list) else []

def _as_dict(v) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}

def _extract_session_id(payload: Dict[str, Any]) -> Optional[int]:
    """Try to read SportAI's numeric session id; return None if absent."""
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
    # Prefer explicit UID if present anywhere
    for k in ("session_uid", "sessionUid", "sessionUID"):
        v = payload.get(k) or (payload.get("session") or {}).get(k) or (payload.get("metadata") or {}).get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Deterministic fallback: <tid8>-<payloadhash10>
    tid = (task_id or "")[:8] or "nosrc"
    ph  = _sha256_str(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))[:10]
    return f"{tid}-{ph}"

# --------------------- Tables ---------------------
# Many-rows-per-task tables (arrays)
BRONZE_ARRAY = [
    "player",
    "player_swing",
    "rally",
    "ball_position",
    "ball_bounce",
    "unmatched_field",
    "debug_event",
]

# One-row-per-task tables (singletons) -> need a unique index on task_id
BRONZE_SINGLETON = [
    "player_position",        # we store the original dict as-is
    "session_confidences",
    "thumbnail",
    "highlight",
    "team_session",
    "bounce_heatmap",
    "submission_context",     # ensure exists here as well
]

ALL_BRONZE = BRONZE_ARRAY + BRONZE_SINGLETON

# --------------------- Init / DDL ---------------------
def _ensure_table_has_task_id(conn, table: str, singleton: bool) -> None:
    # Ensure table exists with (task_id TEXT, data JSONB)
    conn.execute(sql_text(f"""
        CREATE TABLE IF NOT EXISTS bronze.{table} (
            task_id TEXT,
            data    JSONB
        );
    """))
    # Ensure task_id column exists (idempotent)
    conn.execute(sql_text(f"""
        ALTER TABLE bronze.{table}
        ADD COLUMN IF NOT EXISTS task_id TEXT;
    """))
    # For singletons ensure a unique index on task_id
    if singleton:
        conn.execute(sql_text(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_bronze_{table}_task
            ON bronze.{table}(task_id);
        """))



def _run_bronze_init_conn(conn) -> None:
    # Schema
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze;"))

    # Session registry keyed by task_id
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.session (
            task_id     TEXT PRIMARY KEY,
            session_uid TEXT,
            session_id  BIGINT,
            meta        JSONB,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))

    # RAW snapshot table (task_id + JSONB/GZIP + sha)
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.raw_result (
            id              BIGSERIAL PRIMARY KEY,
            task_id         TEXT NOT NULL,
            payload_json    JSONB,
            payload_gzip    BYTEA,
            payload_sha256  TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))

    # Ensure towers
    for t in BRONZE_ARRAY:
        _ensure_table_has_task_id(conn, t, singleton=False)
    for t in BRONZE_SINGLETON:
        _ensure_table_has_task_id(conn, t, singleton=True)

def _run_bronze_init(conn=None) -> bool:
    """
    Back-compat shim:
      - If called as _run_bronze_init(conn): use that connection
      - If called with no args: open a transaction and run init
    """
    if conn is not None:
        _run_bronze_init_conn(conn)
        return True
    from db_init import engine
    with engine.begin() as _c:
        _run_bronze_init_conn(_c)
    return True


# --------------------- Raw persistence ----------------------
def _persist_raw(conn, task_id: str, payload: Dict[str, Any], size_threshold: int = 5_000_000) -> None:
    js = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sha = _sha256_str(js)
    if len(js) <= size_threshold:
        try:
            conn.execute(sql_text("""
                INSERT INTO bronze.raw_result (task_id, payload_json, payload_sha256)
                VALUES (:tid, :j::JSONB, :sha)
            """), {"tid": task_id, "j": js, "sha": sha})
            return
        except Exception:
            pass
    gz = _gzip_bytes(js)
    conn.execute(sql_text("""
        INSERT INTO bronze.raw_result (task_id, payload_gzip, payload_sha256)
        VALUES (:tid, :gz, :sha)
    """), {"tid": task_id, "gz": gz, "sha": sha})

# --------------------- Submission context -------------------
def _attach_submission_context(conn, task_id: Optional[str]) -> None:
    if not task_id:
        return
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
        SELECT * FROM public.submission_context
         WHERE task_id = :tid
         LIMIT 1
    """), {"tid": task_id}).mappings().first()

    sc = {"task_id": task_id}
    if row:
        for k in keep:
            if k in row and row[k] is not None:
                sc[k] = row[k]

    # upsert task-scoped copy
    conn.execute(sql_text("""
        INSERT INTO bronze.submission_context (task_id, data)
        VALUES (:tid, :j::JSONB)
        ON CONFLICT (task_id) DO UPDATE SET data = EXCLUDED.data
    """), {"tid": task_id, "j": json.dumps(sc)})

    # mirror a lean copy into bronze.session.meta
    conn.execute(sql_text("""
        INSERT INTO bronze.session (task_id, meta)
        VALUES (:tid, JSONB_build_object('submission_context', :j::JSONB))
        ON CONFLICT (task_id) DO UPDATE
        SET meta = COALESCE(bronze.session.meta, '{}'::JSONB)
                 || JSONB_build_object('submission_context', :j::JSONB)
    """), {"tid": task_id, "j": json.dumps(sc)})

# --------------------- JSONB inserts ------------------------
def _insert_json_array(conn, table: str, task_id: str, arr) -> int:
    if not arr: return 0
    values = [{"tid": task_id, "j": json.dumps(x)} for x in arr if isinstance(x, dict)]
    if not values: return 0
    # Fast path: executemany with static SQL
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (task_id, data)
        VALUES (:tid, :j::JSONB)
    """), values)
    return len(values)

def _upsert_single(conn, table: str, task_id: str, obj) -> int:
    if obj is None: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (task_id, data)
        VALUES (:tid, :j::JSONB)
        ON CONFLICT (task_id) DO UPDATE SET data = EXCLUDED.data
    """), {"tid": task_id, "j": json.dumps(obj)})
    return 1

def _ensure_session_row(conn, task_id: str, payload: Dict[str, Any]) -> None:
    """Ensure bronze.session has a row for task_id and attach basic meta/ids."""
    session_id = _extract_session_id(payload)  # optional
    session_uid = _compute_session_uid(task_id, payload)
    meta_patch = {
        "ingest_at": datetime.now(timezone.utc).isoformat(),
        "keys": list(payload.keys())[:50]
    }
    conn.execute(sql_text("""
        INSERT INTO bronze.session (task_id, session_uid, session_id, meta)
        VALUES (:tid, :uid, :sid, :meta::JSONB)
        ON CONFLICT (task_id) DO UPDATE SET
          session_uid = COALESCE(bronze.session.session_uid, :uid),
          session_id  = COALESCE(bronze.session.session_id,  :sid),
          meta        = COALESCE(bronze.session.meta, '{}'::JSONB) || :meta::JSONB
    """), {"tid": task_id, "uid": session_uid, "sid": session_id, "meta": json.dumps(meta_patch)})

# --------------------- Ingestion core -----------------------

def ingest_bronze_strict(
    conn,
    payload: Dict[str, Any],
    replace: bool = True,
    forced_uid: Optional[str] = None,   # kept for API parity; unused
    src_hint: Optional[str] = None,
    task_id: Optional[str] = None
) -> Dict[str, Any]:

    # task_id is canonical
    if not task_id:
        md = _as_dict(payload.get("metadata"))
        task_id = payload.get("task_id") or md.get("task_id")
    if not task_id:
        raise ValueError("task_id is required")

    # Idempotent cleanup by task_id
    if replace:
        for t in ALL_BRONZE:
            conn.execute(sql_text(f"DELETE FROM bronze.{t} WHERE task_id = :tid"), {"tid": task_id})

    # Persist raw snapshot
    _persist_raw(conn, task_id, payload)

    # Ensure/patch bronze.session (stores derived session_uid + optional session_id and meta)
    _ensure_session_row(conn, task_id, payload)

    # ---- Map JSON → bronze towers (no mutation of original objects) ----
    players         = _as_list(payload.get("players"))
    rallies         = _as_list(payload.get("rallies"))
    ball_positions  = _as_list(payload.get("ball_positions"))
    ball_bounces    = _as_list(payload.get("ball_bounces"))
    confidences     = payload.get("confidences")
    thumbnails      = payload.get("thumbnails") or payload.get("thumbnail_crops")
    highlights      = payload.get("highlights")
    team_sessions   = payload.get("team_sessions")
    bounce_heatmap  = payload.get("bounce_heatmap")
    unmatched       = payload.get("unmatched") or payload.get("unmatched_fields")
    debug_events    = payload.get("debug_events") or payload.get("events_debug")

    counts: Dict[str, int] = {}

    # players
    counts["player"] = _insert_json_array(conn, "player", task_id, players)

    # player_swing: collect nested arrays without adding any fields
    swing_rows = []
    for p in players:
        if not isinstance(p, dict):
            continue
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

    # bulk arrays
    counts["rally"]          = _insert_json_array(conn, "rally", task_id, rallies)
    counts["ball_position"]  = _insert_json_array(conn, "ball_position", task_id, ball_positions)
    counts["ball_bounce"]    = _insert_json_array(conn, "ball_bounce", task_id, ball_bounces)
    counts["debug_event"]    = _insert_json_array(conn, "debug_event", task_id, _as_list(debug_events))
    counts["unmatched_field"]= _insert_json_array(conn, "unmatched_field", task_id, _as_list(unmatched))

    # player_positions: store original dict as-is (one row)
    counts["player_position"]    = _upsert_single(conn, "player_position", task_id, payload.get("player_positions"))

    # singletons
    counts["session_confidences"]= _upsert_single(conn, "session_confidences", task_id, confidences)
    counts["thumbnail"]          = _upsert_single(conn, "thumbnail", task_id, thumbnails)
    counts["highlight"]          = _upsert_single(conn, "highlight", task_id, highlights)
    counts["team_session"]       = _upsert_single(conn, "team_session", task_id, team_sessions)
    counts["bounce_heatmap"]     = _upsert_single(conn, "bounce_heatmap", task_id, bounce_heatmap)

    # link submission_context if available
    _attach_submission_context(conn, task_id=task_id)

    return {"task_id": task_id, "counts": counts}

# -------------------------- Routes --------------------------
@ingest_bronze.get("/bronze/init")
def http_bronze_init():
    if not _guard(): return _forbid()
    try:
        with engine.begin() as conn:
            _run_bronze_init_conn(conn)
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
            _run_bronze_init_conn(conn)
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
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        payload = r.json()
        with engine.begin() as conn:
            _run_bronze_init_conn(conn)
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
            _run_bronze_init_conn(conn)
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

@ingest_bronze.post("/bronze/reingest-by-task-id")
def http_bronze_reingest_by_task_id():
    if not _guard(): return _forbid()
    body = request.get_json(silent=True) or {}
    task_id = body.get("task_id")
    replace = str(body.get("replace") or "true").lower() in ("1","true","yes","y")
    scan_limit = int(body.get("scan_limit") or 400)
    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400
    try:
        with engine.begin() as conn:
            _run_bronze_init_conn(conn)
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
                    s = pj if isinstance(pj, str) else json.dumps(pj, separators=(",", ":"))
                    if task_id in s:
                        match = r; break
                pgz = r["payload_gzip"]
                if pgz:
                    try:
                        txt = gzip.decompress(pgz).decode("utf-8", errors="ignore")
                        if task_id in txt:
                            match = r; break
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

# Back-compat alias: some older code imported this name directly
# It points to the same blueprint
ingest_bronze_strict_blueprint = ingest_bronze
