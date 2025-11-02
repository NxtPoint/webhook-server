# ingest_bronze.py — RAW→BRONZE (task_id only, no session_id), Nov 2025
# Endpoints kept: /bronze/init, /bronze/ingest-json, /bronze/ingest-from-url, /bronze/reingest-from-raw
# Writes to schema "bronze_task" to avoid legacy constraints. Flip SCHEMA="bronze" once legacy is retired.

import os, json, gzip, hashlib, re
from typing import Any, Dict, List, Optional
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text
import requests

from db_init import engine  # reuse your engine

ingest_bronze = Blueprint("ingest_bronze", __name__)

OPS_KEY = os.getenv("OPS_KEY", "").strip()
SCHEMA = os.getenv("BRONZE_SCHEMA", "bronze_task")  # <- change to "bronze" when ready
RAW_TABLE = "raw_result"

# Towers (exact JSON headers)
ARRAY_TOWERS = [
    "player",
    "player_swing",
    "rally",
    "ball_position",
    "ball_bounce",
    "unmatched_field",
    "debug_event",
]
SINGLETON_TOWERS = [
    "player_position",
    "session_confidences",
    "thumbnail",
    "highlight",
    "team_session",
    "bounce_heatmap",
    "submission_context",
]
ALL_TOWERS = ARRAY_TOWERS + SINGLETON_TOWERS

# ---------------- auth ----------------
def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return (not OPS_KEY) or supplied == OPS_KEY

def _forbid(): return Response("Forbidden", 403)

# -------------- utils -----------------
def _require_json() -> Dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("JSON body required")
    return body

def _sha(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _as_list(v) -> List[Dict[str, Any]]:
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []

def _as_dict(v) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}

def _derive_task_id(payload: Dict[str, Any], src_hint: Optional[str]) -> Optional[str]:
    md = _as_dict(payload.get("metadata"))
    for t in (payload.get("task_id"), md.get("task_id")):
        if isinstance(t, str) and t.strip():
            return t.strip()
    if src_hint:
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', str(src_hint), re.I)
        if m: return m.group(1)
    return None

# -------------- DDL (idempotent) --------------
DDL_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS {s};
"""

DDL_RAW = """
CREATE TABLE IF NOT EXISTS {s}.{raw} (
  id              BIGSERIAL PRIMARY KEY,
  task_id         TEXT NOT NULL,
  payload_json    JSONB,
  payload_gzip    BYTEA,
  payload_sha256  TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_{s}_{raw}_task ON {s}.{raw}(task_id);
CREATE INDEX IF NOT EXISTS ix_{s}_{raw}_created ON {s}.{raw}(created_at DESC);
"""

DDL_ARRAY = """
CREATE TABLE IF NOT EXISTS {s}.{t} (
  id         BIGSERIAL PRIMARY KEY,
  task_id    TEXT NOT NULL,
  data       JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_{s}_{t}_task ON {s}.{t}(task_id);
"""

DDL_SINGLETON = """
CREATE TABLE IF NOT EXISTS {s}.{t} (
  id         BIGSERIAL PRIMARY KEY,
  task_id    TEXT NOT NULL UNIQUE,
  data       JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

def _run_bronze_init(conn=None) -> bool:
    if conn is None:
        with engine.begin() as c:
            return _run_bronze_init(c)
    conn.execute(sql_text(DDL_SCHEMA.format(s=SCHEMA)))
    conn.execute(sql_text(DDL_RAW.format(s=SCHEMA, raw=RAW_TABLE)))
    for t in ARRAY_TOWERS:
        conn.execute(sql_text(DDL_ARRAY.format(s=SCHEMA, t=t)))
    for t in SINGLETON_TOWERS:
        conn.execute(sql_text(DDL_SINGLETON.format(s=SCHEMA, t=t)))
    return True

# -------------- RAW persist --------------
def _persist_raw(conn, task_id: str, payload: Dict[str, Any]) -> None:
    txt = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sha = _sha(txt)
    if len(txt) <= 5_000_000:
        conn.execute(sql_text(f"""
            INSERT INTO {SCHEMA}.{RAW_TABLE} (task_id, payload_json, payload_sha256)
            VALUES (:tid, CAST(:j AS JSONB), :sha)
        """), {"tid": task_id, "j": txt, "sha": sha})
    else:
        gz = gzip.compress(txt.encode("utf-8"))
        conn.execute(sql_text(f"""
            INSERT INTO {SCHEMA}.{RAW_TABLE} (task_id, payload_gzip, payload_sha256)
            VALUES (:tid, :gz, :sha)
        """), {"tid": task_id, "gz": gz, "sha": sha})

# -------------- Bronze transpose --------------
def _insert_array(conn, table: str, task_id: str, arr) -> int:
    rows = [{"tid": task_id, "j": json.dumps(x, separators=(",", ":"), ensure_ascii=False)}
            for x in _as_list(arr)]
    if not rows: return 0
    conn.execute(sql_text(f"""
        INSERT INTO {SCHEMA}.{table} (task_id, data)
        VALUES (:tid, CAST(:j AS JSONB))
    """), rows)
    return len(rows)

def _upsert_singleton(conn, table: str, task_id: str, obj) -> int:
    if not isinstance(obj, (dict, list)):  # allow dict or list for singletons if SportAI uses arrays
        return 0
    conn.execute(sql_text(f"""
        INSERT INTO {SCHEMA}.{table} (task_id, data)
        VALUES (:tid, CAST(:j AS JSONB))
        ON CONFLICT (task_id) DO UPDATE SET data = EXCLUDED.data
    """), {"tid": task_id, "j": json.dumps(obj, separators=(",", ":"), ensure_ascii=False)})
    return 1

def _transpose_bronze(conn, task_id: str, payload: Dict[str, Any]) -> Dict[str, int]:
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
    player_positions= payload.get("player_positions")

    # player_swing: flatten nested swings/strokes without mutating
    swing_rows: List[Dict[str, Any]] = []
    for p in players:
        for key in ("swings", "strokes", "swing_events"):
            swing_rows.extend(_as_list(p.get(key)))
        stats = _as_dict(p.get("statistics") or p.get("stats"))
        for key in ("swings", "strokes", "swing_events"):
            swing_rows.extend(_as_list(stats.get(key)))

    counts = {}
    counts["player"]           = _insert_array(conn, "player", task_id, players)
    counts["player_swing"]     = _insert_array(conn, "player_swing", task_id, swing_rows)
    counts["rally"]            = _insert_array(conn, "rally", task_id, rallies)
    counts["ball_position"]    = _insert_array(conn, "ball_position", task_id, ball_positions)
    counts["ball_bounce"]      = _insert_array(conn, "ball_bounce", task_id, ball_bounces)
    counts["unmatched_field"]  = _insert_array(conn, "unmatched_field", task_id, unmatched)
    counts["debug_event"]      = _insert_array(conn, "debug_event", task_id, debug_events)

    counts["player_position"]      = _upsert_singleton(conn, "player_position", task_id, player_positions)
    counts["session_confidences"]  = _upsert_singleton(conn, "session_confidences", task_id, confidences)
    counts["thumbnail"]            = _upsert_singleton(conn, "thumbnail", task_id, thumbnails)
    counts["highlight"]            = _upsert_singleton(conn, "highlight", task_id, highlights)
    counts["team_session"]         = _upsert_singleton(conn, "team_session", task_id, team_sessions)
    counts["bounce_heatmap"]       = _upsert_singleton(conn, "bounce_heatmap", task_id, bounce_heatmap)

    # mirror submission_context if upstream provided it (or leave empty)
    sub_ctx = _as_dict(payload.get("submission_context") or {})
    _upsert_singleton(conn, "submission_context", task_id, sub_ctx)

    return counts

# -------------- core --------------
def ingest_bronze_strict(conn, payload: Dict[str, Any], replace: bool = True,
                         src_hint: Optional[str] = None, task_id: Optional[str] = None, **_):
    task_id = task_id or _derive_task_id(payload, src_hint)
    if not task_id:
        raise ValueError("task_id is required")

    # raw first (always)
    _persist_raw(conn, task_id, payload)

    # optional replace
    if replace:
        for t in ALL_TOWERS:
            conn.execute(sql_text(f"DELETE FROM {SCHEMA}.{t} WHERE task_id = :tid"), {"tid": task_id})

    # transpose
    counts = _transpose_bronze(conn, task_id, payload)
    return {"ok": True, "task_id": task_id, "counts": counts}

# -------------- routes --------------
@ingest_bronze.get("/bronze/init")
def http_bronze_init():
    if not _guard(): return _forbid()
    try:
        with engine.begin() as c:
            _run_bronze_init(c)
        return jsonify({"ok": True, "schema": SCHEMA})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

@ingest_bronze.post("/bronze/ingest-json")
def http_bronze_ingest_json():
    if not _guard(): return _forbid()
    try:
        body = _require_json()
        payload = body.get("payload") or body
        replace = str(body.get("replace") or "true").lower() in ("1","true","yes","y")
        task_id = body.get("task_id")
        with engine.begin() as c:
            _run_bronze_init(c)
            out = ingest_bronze_strict(c, payload, replace=replace, task_id=task_id, src_hint="api:ingest-json")
        return jsonify(out)
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
        with engine.begin() as c:
            _run_bronze_init(c)
            out = ingest_bronze_strict(c, payload, replace=replace, task_id=task_id, src_hint=url)
        return jsonify(out)
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
        with engine.begin() as c:
            _run_bronze_init(c)
            row = c.execute(sql_text(f"""
                SELECT payload_json, payload_gzip
                  FROM {SCHEMA}.{RAW_TABLE}
                 WHERE task_id = :tid
              ORDER BY created_at DESC
                 LIMIT 1
            """), {"tid": task_id}).mappings().first()
            if not row:
                return jsonify({"ok": False, "error": f"no RAW for task_id={task_id}"}), 404
            if row["payload_json"] is not None:
                payload = row["payload_json"] if isinstance(row["payload_json"], dict) else json.loads(row["payload_json"])
            elif row["payload_gzip"] is not None:
                payload = json.loads(gzip.decompress(row["payload_gzip"]).decode("utf-8"))
            else:
                return jsonify({"ok": False, "error": "RAW row had no JSON"}), 500
            out = ingest_bronze_strict(c, payload, replace=replace, task_id=task_id, src_hint="reingest-from-raw")
            return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

# Back-compat alias if anything imports this name elsewhere
ingest_bronze_strict_blueprint = ingest_bronze
