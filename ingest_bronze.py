# ingest_bronze.py — RAW-first, then bronze towers (simple JSONB form)
# Contract:
#   - RAW is *always* written & committed before bronze mapping.
#   - Bronze towers expect tables shaped as: (task_id TEXT, data JSONB, created_at TIMESTAMPTZ DEFAULT now()).
#   - If a tower table doesn't fit that simple shape, that tower is skipped (others continue).
# Endpoints:
#   GET  /bronze/init                 -> ensure bronze.raw_result exists (no other DDL)
#   POST /bronze/ingest-from-url      -> fetch JSON, save RAW, map to bronze towers
#   POST /bronze/ingest-json          -> accept payload inline, save RAW, map to bronze towers
#   POST /bronze/reingest-from-raw    -> reload from last RAW row for a task_id

import os, json, gzip, hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text

from db_init import engine

# ---------- Blueprint / Auth ----------
ingest_bronze = Blueprint("ingest_bronze", __name__)
OPS_KEY = os.getenv("OPS_KEY", "").strip()

def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return (not OPS_KEY) or supplied == OPS_KEY

def _forbid(): return Response("Forbidden", 403)

# ---------- Small utils ----------
def _require_json() -> Dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("JSON body required")
    return body

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _gzip_bytes(s: str) -> bytes:
    return gzip.compress(s.encode("utf-8"))

def _as_list(v) -> List[Any]:
    if isinstance(v, list): return v
    return []

def _derive_task_id(payload: dict | None, src_hint: str | None) -> Optional[str]:
    import re
    p = payload or {}
    md = p.get("metadata") or {}
    tid = p.get("task_id") or md.get("task_id")
    if isinstance(tid, str) and tid.strip():
        return tid.strip()
    if src_hint:
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', str(src_hint), re.I)
        if m: return m.group(1)
    return None

# ---------- Minimal DDL (RAW only) ----------
def _ensure_raw_table():
    with engine.begin() as conn:
        conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze;"))
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
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_bronze_raw_task ON bronze.raw_result(task_id);"))

# ---------- RAW persistence (committed separately) ----------
def _persist_raw_committed(task_id: str, payload: Dict[str, Any], size_threshold: int = 5_000_000) -> Dict[str, Any]:
    js = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sha = _sha256(js)
    with engine.begin() as c:
        if len(js) <= size_threshold:
            # Prefer JSONB column (smaller, queryable)
            try:
                c.execute(sql_text("""
                    INSERT INTO bronze.raw_result (task_id, payload_json, payload_sha256)
                    VALUES (:tid, CAST(:j AS JSONB), :sha)
                """), {"tid": task_id, "j": js, "sha": sha})
                return {"sha": sha, "stored": "json"}
            except Exception:
                pass  # fallback to gzip
        gz = _gzip_bytes(js)
        c.execute(sql_text("""
            INSERT INTO bronze.raw_result (task_id, payload_gzip, payload_sha256)
            VALUES (:tid, :gz, :sha)
        """), {"tid": task_id, "gz": gz, "sha": sha})
        return {"sha": sha, "stored": "gzip"}

# ---------- Bronze mapping helpers (JSONB towers only) ----------
# We *assume* bronze.<tower>(task_id TEXT, data JSONB, created_at TIMESTAMPTZ default now()).
# If the table has a different shape (e.g., legacy NOT NULL columns), we skip it gracefully.

_TARRAY = {
    "player": "players",
    "player_swing": None,          # derived from players[*].swings/strokes
    "rally": "rallies",
    "ball_position": "ball_positions",
    "ball_bounce": "ball_bounces",
    "unmatched_field": ["unmatched", "unmatched_fields"],
    "debug_event": ["debug_events", "events_debug"],
}

_TSINGLE = {
    "player_position": "player_positions",     # original dict
    "session_confidences": "confidences",
    "thumbnail": ["thumbnails", "thumbnail_crops"],
    "highlight": "highlights",
    "team_session": "team_sessions",
    "bounce_heatmap": "bounce_heatmap",
    "submission_context": None,               # optional (from public.submission_context), skipped here
}

def _table_accepts_jsonb(conn, table: str) -> bool:
    row = conn.execute(sql_text("""
        SELECT COUNT(*) = 2
        FROM information_schema.columns
        WHERE table_schema='bronze' AND table_name=:t AND column_name IN ('task_id','data')
    """), {"t": table}).scalar()
    return bool(row)

def _insert_many_jsonb(conn, table: str, task_id: str, items: List[Dict[str, Any]]) -> int:
    if not items: return 0
    if not _table_accepts_jsonb(conn, table): return 0
    values = [{"tid": task_id, "j": json.dumps(x)} for x in items if isinstance(x, dict)]
    if not values: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (task_id, data)
        VALUES (:tid, CAST(:j AS JSONB))
    """), values)
    return len(values)

def _insert_one_jsonb(conn, table: str, task_id: str, obj: Dict[str, Any]) -> int:
    if not isinstance(obj, dict): return 0
    if not _table_accepts_jsonb(conn, table): return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (task_id, data)
        VALUES (:tid, CAST(:j AS JSONB))
        ON CONFLICT DO NOTHING
    """), {"tid": task_id, "j": json.dumps(obj)})
    return 1

def _collect_swings(players: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in players or []:
        if not isinstance(p, dict): continue
        for key in ("swings", "strokes", "swing_events"):
            for s in _as_list(p.get(key)):
                if isinstance(s, dict): out.append(s)
        stats = p.get("statistics") or p.get("stats") or {}
        if isinstance(stats, dict):
            for key in ("swings", "strokes", "swing_events"):
                for s in _as_list(stats.get(key)):
                    if isinstance(s, dict): out.append(s)
    return out

# ---- COMPAT SHIMS (keep old imports working) -------------------------------

# List of bronze towers we may want to wipe when replace=True
_ALL_TOWERS = list(_TARRAY.keys()) + list(_TSINGLE.keys())

def _run_bronze_init(conn=None) -> bool:
    """
    Back-compat: previously accepted (optional) connection.
    Now we just ensure RAW exists; other DDL is out of scope for this minimal version.
    """
    if conn is None:
        _ensure_raw_table()
        return True
    # If a connection is provided, use it to ensure schema/table exists
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze;"))
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
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_bronze_raw_task ON bronze.raw_result(task_id);"))
    return True


def ingest_bronze_strict(
    conn,
    payload: dict,
    replace: bool = True,
    forced_uid: str | None = None,   # ignored (kept for signature compatibility)
    src_hint: str | None = None,
    task_id: str | None = None
) -> dict:
    """
    Back-compat ingestion entrypoint that your upload_app.py expects.

    Behavior:
      1) Ensures RAW table exists.
      2) Persists RAW (committed separately so we never lose it).
      3) Optionally deletes existing bronze rows for this task_id if replace=True.
      4) Maps JSON -> simple JSONB towers via _ingest_payload().
    """
    _ensure_raw_table()

    # Resolve task_id like before
    tid = task_id or _derive_task_id(payload, src_hint)
    if not tid:
        raise ValueError("task_id is required")

    # 1) Persist RAW (separate commit)
    _persist_raw_committed(tid, payload)

    # 2) Replace (best-effort, only for towers that have (task_id,data))
    if replace:
        try:
            for t in _ALL_TOWERS:
                # Only delete if the table has the expected simple shape
                with conn.begin():
                    if _table_accepts_jsonb(conn, t):
                        conn.execute(sql_text(f"DELETE FROM bronze.{t} WHERE task_id = :tid"), {"tid": tid})
        except Exception:
            # Don’t fail RAW persistence if some legacy table layout is incompatible
            pass

    # 3) Map to bronze towers
    return _ingest_payload(tid, payload)


# ---------- Core ingest (payload already loaded) ----------
def _ingest_payload(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}

    # ARRAY towers
    players = payload.get("players") or []
    counts["player"] = 0
    with engine.begin() as conn:
        counts["player"] = _insert_many_jsonb(conn, "player", task_id, players)

        swings = _collect_swings(players)
        counts["player_swing"] = _insert_many_jsonb(conn, "player_swing", task_id, swings)

        for table, key in _TARRAY.items():
            if table in ("player", "player_swing"):  # already handled
                continue
            keys = key if isinstance(key, list) else [key]
            src = None
            for k in (keys or []):
                if k and isinstance(payload.get(k), list):
                    src = payload.get(k); break
            if src is not None:
                counts[table] = _insert_many_jsonb(conn, table, task_id, src)
            else:
                counts[table] = 0

        # SINGLETON towers
        for table, key in _TSINGLE.items():
            if key is None:
                continue  # submission_context handled by your upload_app writer; skip here
            keys = key if isinstance(key, list) else [key]
            obj = None
            for k in keys:
                v = payload.get(k)
                if isinstance(v, dict): obj = v; break
            counts[table] = _insert_one_jsonb(conn, table, task_id, obj) if obj else 0

    return {"task_id": task_id, "counts": counts}

# ---------- HTTP Routes ----------
@ingest_bronze.get("/bronze/init")
def http_bronze_init():
    if not _guard(): return _forbid()
    try:
        _ensure_raw_table()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

@ingest_bronze.post("/bronze/ingest-json")
def http_bronze_ingest_json():
    if not _guard(): return _forbid()
    try:
        _ensure_raw_table()
        body = _require_json()
        payload = body.get("payload") or body
        task_id = body.get("task_id") or _derive_task_id(payload, "api:ingest-json")
        if not task_id: return jsonify({"ok": False, "error": "task_id required"}), 400

        raw_info = _persist_raw_committed(task_id, payload)
        out = _ingest_payload(task_id, payload)
        return jsonify({"ok": True, "raw": raw_info, **out})
    except Exception as e:
        # RAW is already safe; report the mapping failure
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 400

@ingest_bronze.post("/bronze/ingest-from-url")
def http_bronze_ingest_from_url():
    if not _guard(): return _forbid()
    try:
        _ensure_raw_table()
        body = request.get_json(silent=True) or {}
        url  = (body.get("result_url") or "").strip()
        if not url: return jsonify({"ok": False, "error": "result_url required"}), 400

        r = requests.get(url, timeout=300)
        r.raise_for_status()
        payload = r.json()
        task_id = body.get("task_id") or _derive_task_id(payload, url)
        if not task_id: return jsonify({"ok": False, "error": "task_id required"}), 400

        raw_info = _persist_raw_committed(task_id, payload)
        out = _ingest_payload(task_id, payload)
        return jsonify({"ok": True, "raw": raw_info, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

@ingest_bronze.post("/bronze/reingest-from-raw")
def http_bronze_reingest_from_raw():
    if not _guard(): return _forbid()
    try:
        _ensure_raw_table()
        body = request.get_json(silent=True) or {}
        task_id = (body.get("task_id") or "").strip()
        if not task_id: return jsonify({"ok": False, "error": "task_id required"}), 400

        with engine.begin() as conn:
            row = conn.execute(sql_text("""
                SELECT payload_json, payload_gzip
                  FROM bronze.raw_result
                 WHERE task_id=:tid
              ORDER BY id DESC
                 LIMIT 1
            """), {"tid": task_id}).mappings().first()
        if not row:
            return jsonify({"ok": False, "error": f"no raw_result for task_id={task_id}"}), 404

        if row["payload_json"] is not None:
            payload = row["payload_json"] if isinstance(row["payload_json"], dict) else json.loads(row["payload_json"])
        elif row["payload_gzip"] is not None:
            payload = json.loads(gzip.decompress(row["payload_gzip"]).decode("utf-8"))
        else:
            return jsonify({"ok": False, "error": "empty RAW row (no json or gzip)"}), 500

        out = _ingest_payload(task_id, payload)
        return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500
