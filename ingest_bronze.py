# ingest_bronze.py — clean, task_id-only bronze ingest (Nov 2025)
# Flow:
#   1) /bronze/ingest-from-url: fetch SportAI JSON, persist RAW (jsonb or gzip), then fan out to bronze towers
#   2) /bronze/ingest-json: same but payload posted directly
#   3) /bronze/reingest-from-raw: reload from last RAW snapshot by task_id
# Schema contract:
#   - schema: bronze
#   - tables: raw_result, session (+ arrays/singletons with columns id, task_id, data, created_at)

import os, json, gzip, hashlib, re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List
import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text
from db_init import engine

SCHEMA = "bronze"
OPS_KEY = os.getenv("OPS_KEY", "").strip()
ingest_bronze = Blueprint("ingest_bronze", __name__)

# ------------------- auth -------------------
def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return (not OPS_KEY) or supplied == OPS_KEY

def _forbid(): return Response("Forbidden", 403)

# ---------------- utils ----------------
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
    if v is None: return []
    return v if isinstance(v, list) else []

def _as_dict(v) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}

def _derive_task_id(payload: dict | None, src_hint: str | None) -> Optional[str]:
    p = payload or {}
    md = _as_dict(p.get("metadata"))
    tid = p.get("task_id") or md.get("task_id")
    if isinstance(tid, str) and tid.strip():
        return tid.strip()
    if src_hint:
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', str(src_hint), re.I)
        if m: return m.group(1)
    return None

def _compute_session_uid(task_id: str, payload: Dict[str, Any]) -> str:
    # stable but not critical; helps identify a run in bronze.session
    ph = _sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))[:10]
    return f"{task_id[:8]}-{ph}"

def _first_list(p: Dict[str, Any], *keys: str) -> list:
    """Return the first list found under any of the candidate keys (top-level or under .statistics)."""
    if not isinstance(p, dict):
        return []
    for k in keys:
        v = p.get(k)
        if isinstance(v, list):
            return v
    stats = p.get("statistics")
    if isinstance(stats, dict):
        for k in keys:
            v = stats.get(k)
            if isinstance(v, list):
                return v
    return []

# ---------------- jsonb sweeper--------------------------
def _pg_cast_for(data_type: str) -> str:
    dt = (data_type or "").lower()
    if dt in ("text", "character varying", "character", "citext"):
        return "::text"
    if dt in ("integer", "int4"):
        return "::integer"
    if dt in ("bigint", "int8"):
        return "::bigint"
    if dt in ("smallint", "int2"):
        return "::smallint"
    if dt in ("real", "float4"):
        return "::real"
    if dt in ("double precision", "float8"):
        return "::double precision"
    if dt.startswith("numeric"):
        return "::numeric"
    if dt in ("boolean",):
        return "::boolean"
    if dt in ("uuid",):
        return "::uuid"
    if dt in ("date",):
        return "::date"
    if dt in ("timestamp with time zone", "timestamptz"):
        return "::timestamptz"
    if dt in ("timestamp without time zone", "timestamp"):
        return "::timestamp"
    if dt in ("jsonb", "json"):
        return "::jsonb"
    # default: try text
    return "::text"


def _sweep_json_into_columns(conn, table: str, where_sql: str, where_params: dict) -> int:
    """
    For bronze.<table>, copy values from JSONB 'data' into existing typed columns (if NULL),
    then remove those keys from 'data'. Returns number of rows updated.
    - We never overwrite non-NULL typed columns.
    - We cast based on information_schema column types.
    - Keys absent or empty strings remain NULL.
    """
    cols = conn.execute(sql_text("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema='bronze'
          AND table_name=:t
          AND column_name NOT IN ('id','created_at','data')
    """), {"t": table}).mappings().all()

    # Select only “target” columns that are not task_id-like and not JSON holder
    targets = []
    for c in cols:
        name = c["column_name"]
        if name in ("task_id",):  # PK/UK we don’t populate from data
            continue
        targets.append((name, c["data_type"]))

    if not targets:
        return 0

    set_parts = []
    params = dict(where_params or {})
    # Build SET clauses like: col = COALESCE(col, NULLIF(data->>'col','')::type)
    for i, (col, dt) in enumerate(targets):
        cast = _pg_cast_for(dt)
        if cast == "::jsonb":
            # For jsonb columns, take data->col (not ->>)
            expr = f"CASE WHEN data ? :k{i} THEN (data->:k{i}){cast} END"
        else:
            expr = f"NULLIF(data->>:k{i}, ''){cast}"
        set_parts.append(f"{col} = COALESCE({col}, {expr})")
        params[f"k{i}"] = col

    # Build “strip keys” expression: (((data - 'k1') - 'k2') - 'k3') …
    strip_expr = "data"
    for i, (col, _) in enumerate(targets):
        strip_expr = f"({strip_expr} - :s{i})"
        params[f"s{i}"] = col

    # Set data to NULL if empty after stripping
    set_data = f"data = CASE WHEN {strip_expr} = '{{}}'::jsonb THEN NULL ELSE {strip_expr} END"

    sql = f"""
        UPDATE bronze.{table}
        SET {", ".join(set_parts + [set_data])}
        WHERE {where_sql} AND data IS NOT NULL
    """
    res = conn.execute(sql_text(sql), params)
    return res.rowcount or 0

# ---------------- init / DDL (idempotent) ----------------
def _run_bronze_init_conn(conn):
    """Create or verify bronze schema + tables (requires open connection)."""
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze;"))

    # RAW
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.raw_result (
            id BIGSERIAL PRIMARY KEY,
            task_id TEXT NOT NULL,
            payload_json JSONB,
            payload_gzip BYTEA,
            payload_sha256 TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_bronze_raw_result_task ON bronze.raw_result(task_id)"))

    # SESSION
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.session (
            task_id TEXT PRIMARY KEY,
            session_uid TEXT,
            meta JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))

    # ARRAYS
    for t in ["player","player_swing","rally","ball_position","ball_bounce","unmatched_field","debug_event"]:
        conn.execute(sql_text(f"""
            CREATE TABLE IF NOT EXISTS bronze.{t} (
                id BIGSERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                data JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))
        conn.execute(sql_text(f"CREATE INDEX IF NOT EXISTS ix_bronze_{t}_task ON bronze.{t}(task_id)"))

    # SINGLETONS
    for t in ["player_position","session_confidences","thumbnail","highlight","team_session","bounce_heatmap","submission_context"]:
        conn.execute(sql_text(f"""
            CREATE TABLE IF NOT EXISTS bronze.{t} (
                task_id TEXT PRIMARY KEY,
                data JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))

def _run_bronze_init(conn=None):
    from db_init import engine
    if conn is not None:
        _run_bronze_init_conn(conn)
    else:
        with engine.begin() as c:
            _run_bronze_init_conn(c)
    return True


# --------------- raw persistence ---------------
def _persist_raw(conn, task_id: str, payload: Dict[str, Any], size_threshold: int = 5_000_000):
    s = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sha = _sha256(s)
    if len(s) <= size_threshold:
        conn.execute(sql_text("""
            INSERT INTO bronze.raw_result (task_id, payload_json, payload_sha256)
            VALUES (:tid, CAST(:j AS JSONB), :sha)
        """), {"tid": task_id, "j": s, "sha": sha})
    else:
        conn.execute(sql_text("""
            INSERT INTO bronze.raw_result (task_id, payload_gzip, payload_sha256)
            VALUES (:tid, :gz, :sha)
        """), {"tid": task_id, "gz": _gzip_bytes(s), "sha": sha})

# --------------- fan-out helpers ---------------
def _insert_json_array(conn, table: str, task_id: str, arr) -> int:
    if not arr: return 0
    rows = [{"tid": task_id, "j": json.dumps(x)} for x in arr if isinstance(x, dict)]
    if not rows: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (task_id, data)
        VALUES (:tid, CAST(:j AS JSONB))
    """), rows)
    return len(rows)

def _upsert_single(conn, table: str, task_id: str, obj) -> int:
    if obj is None: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (task_id, data)
        VALUES (:tid, CAST(:j AS JSONB))
        ON CONFLICT (task_id) DO UPDATE SET data = EXCLUDED.data
    """), {"tid": task_id, "j": json.dumps(obj)})
    return 1

def _ensure_session(conn, task_id: str, payload: Dict[str, Any]):
    meta_patch = {"ingest_at": datetime.now(timezone.utc).isoformat(),
                  "keys": list(payload.keys())[:50]}
    conn.execute(sql_text("""
        INSERT INTO bronze.session (task_id, session_uid, meta)
        VALUES (:tid, :uid, CAST(:meta AS JSONB))
        ON CONFLICT (task_id) DO UPDATE SET
          session_uid = COALESCE(bronze.session.session_uid, :uid),
          meta        = COALESCE(bronze.session.meta, '{}'::jsonb) || CAST(:meta AS JSONB)
    """), {"tid": task_id, "uid": _compute_session_uid(task_id, payload), "meta": json.dumps(meta_patch)})

# --------------- core ingest ---------------
def ingest_bronze_strict(
    conn,
    payload: Dict[str, Any],
    replace: bool = True,
    forced_uid: Optional[str] = None,
    src_hint: Optional[str] = None,
    task_id: Optional[str] = None,
    **_,
) -> Dict[str, Any]:

    if not task_id:
        task_id = _derive_task_id(payload, None)
    if not task_id:
        raise ValueError("task_id is required")

    if replace:
        for t in ["player","player_swing","rally","ball_position","ball_bounce",
                  "unmatched_field","debug_event","player_position","session_confidences",
                  "thumbnail","highlight","team_session","bounce_heatmap","submission_context"]:
            conn.execute(sql_text(f"DELETE FROM bronze.{t} WHERE task_id=:tid"), {"tid": task_id})

    _persist_raw(conn, task_id, payload)
    _ensure_session(conn, task_id, payload)

    players         = _as_list(payload.get("players"))
    ball_positions  = _as_list(payload.get("ball_positions"))
    ball_bounces    = _as_list(payload.get("ball_bounces"))
    confidences     = payload.get("confidences")
    thumbnails      = payload.get("thumbnails") or payload.get("thumbnail_crops")
    highlights      = payload.get("highlights")
    team_sessions   = payload.get("team_sessions")
    bounce_heatmap  = payload.get("bounce_heatmap")
    unmatched       = payload.get("unmatched") or payload.get("unmatched_fields")
    debug_events    = payload.get("debug_events") or payload.get("events_debug")
    # Robust rally extraction: accept list OR dict wrapper, and fall back to .statistics.*
    _rally_candidates = [
        payload.get("rallies"),
        payload.get("rally_events"),
        payload.get("rally"),
        payload.get("rally_segments"),
        (payload.get("statistics") or {}).get("rallies"),
    ]

    _r = next((v for v in _rally_candidates if isinstance(v, (list, dict))), None)

    if isinstance(_r, dict):
        # common wrappers: { "rallies": [...] } or { "items": [...] } or { "data": [...] }
        _r = _r.get("rallies") or _r.get("items") or _r.get("data") or []

    # Normalize to array of dicts (if entries are scalars, wrap them)
    rallies = []
    if isinstance(_r, list):
        for x in _r:
            rallies.append(x if isinstance(x, dict) else {"value": x})


    # player_swing without mutating original
    swing_rows = []
    for p in players:
        if not isinstance(p, dict): continue
        for k in ("swings","strokes","swing_events"):
            for s in _as_list(p.get(k)):
                if isinstance(s, dict):
                    swing_rows.append(s)
        stats = _as_dict(p.get("statistics") or p.get("stats"))
        for k in ("swings","strokes","swing_events"):
            for s in _as_list(stats.get(k)):
                if isinstance(s, dict):
                    swing_rows.append(s)

    counts = {}
    counts["player"]             = _insert_json_array(conn, "player", task_id, players)
    counts["player_swing"]       = _insert_json_array(conn, "player_swing", task_id, swing_rows)
    counts["rally"]              = _insert_json_array(conn, "rally", task_id, rallies)
    counts["ball_position"]      = _insert_json_array(conn, "ball_position", task_id, ball_positions)
    counts["ball_bounce"]        = _insert_json_array(conn, "ball_bounce", task_id, ball_bounces)
    counts["debug_event"]        = _insert_json_array(conn, "debug_event", task_id, debug_events)
    counts["unmatched_field"]    = _insert_json_array(conn, "unmatched_field", task_id, unmatched)
    counts["player_position"]    = _upsert_single(conn, "player_position", task_id, payload.get("player_positions"))
    counts["session_confidences"]= _upsert_single(conn, "session_confidences", task_id, confidences)
    counts["thumbnail"]          = _upsert_single(conn, "thumbnail", task_id, thumbnails)
    counts["highlight"]          = _upsert_single(conn, "highlight", task_id, highlights)
    counts["team_session"]       = _upsert_single(conn, "team_session", task_id, team_sessions)
    counts["bounce_heatmap"]     = _upsert_single(conn, "bounce_heatmap", task_id, bounce_heatmap)

    # submission_context mirror (optional: if you keep a public.submission_context)
    sc_row = None
    try:
        sc_row = conn.execute(sql_text("""
            SELECT row_to_json(t) AS j
            FROM public.submission_context t
            WHERE task_id=:tid
            LIMIT 1
        """), {"tid": task_id}).scalar()
    except Exception:
        sc_row = None
    if sc_row:
        counts["submission_context"] = _upsert_single(conn, "submission_context", task_id, sc_row)
        # Auto-sweep JSON → typed columns for any columns already present on the table
        _sweep_json_into_columns(conn, "submission_context", "task_id = :tid", {"tid": task_id})

    else:
        counts["submission_context"] = 0

    return {"task_id": task_id, "counts": counts}

# ------------------ routes ------------------
@ingest_bronze.get("/bronze/init")
def http_bronze_init():
    if not _guard(): return _forbid()
    try:
        _run_bronze_init()
        return jsonify({"ok": True})
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
        with engine.begin() as conn:
            _run_bronze_init()
            out = ingest_bronze_strict(conn, payload, task_id=task_id, replace=replace)
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
        # derive task_id if not provided
        if not task_id:
            task_id = _derive_task_id(payload, url)
        with engine.begin() as conn:
            _run_bronze_init()
            out = ingest_bronze_strict(conn, payload, task_id=task_id, replace=replace)
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
            _run_bronze_init()
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
                return jsonify({"ok": False, "error": "no payload_json or payload_gzip present"}), 500
            out = ingest_bronze_strict(conn, payload, task_id=task_id, replace=replace)
        return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

# Provide the blueprint symbol and function names upload_app imports
ingest_bronze_strict_blueprint = ingest_bronze
