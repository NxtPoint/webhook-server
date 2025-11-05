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

    # ARRAYS (note: player_position is now an array table)
    for t in ["player","player_swing","rally","ball_position","ball_bounce",
            "unmatched_field","debug_event","player_position"]:
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
    for t in ["session_confidences","thumbnail","highlight","team_session","bounce_heatmap","submission_context"]:
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

def _as_float(x):
    try:
        if x is None: return None
        return float(x)
    except Exception:
        return None

def _as_int(x):
    try:
        if x is None: return None
        return int(x)
    except Exception:
        return None

def _insert_players(conn, task_id: str, players: list) -> int:
    if not players: return 0
    rows = []
    for p in players:
        if not isinstance(p, dict): 
            continue
        rows.append({
            "tid": task_id,
            "j": json.dumps(p),
            "player_id": _as_int(p.get("player_id")),
            "activity_score": _as_float(p.get("activity_score")),
            "covered_distance": _as_float(p.get("covered_distance")),
            "fastest_sprint": _as_float(p.get("fastest_sprint")),
            "fastest_sprint_timestamp": _as_float(p.get("fastest_sprint_timestamp")),
            "swing_count": _as_int(p.get("swing_count")),
            "swing_type_distribution": json.dumps(p.get("swing_type_distribution")) if p.get("swing_type_distribution") is not None else None,
            "location_heatmap": json.dumps(p.get("location_heatmap")) if p.get("location_heatmap") is not None else None,
        })
    if not rows: return 0

    conn.execute(sql_text("""
        INSERT INTO bronze.player (
            task_id, data,
            player_id, activity_score, covered_distance, fastest_sprint, fastest_sprint_timestamp,
            swing_count, swing_type_distribution, location_heatmap
        ) VALUES (
            :tid, CAST(:j AS JSONB),
            :player_id, :activity_score, :covered_distance, :fastest_sprint, :fastest_sprint_timestamp,
            :swing_count, CAST(:swing_type_distribution AS JSONB), CAST(:location_heatmap AS JSONB)
        )
    """), rows)
    return len(rows)

def _insert_player_swings(conn, task_id: str, swings: list) -> int:
    if not swings: return 0
    rows = []
    for s in swings:
        if not isinstance(s, dict):
            continue
        start = s.get("start") or {}
        end   = s.get("end")   or {}
        rows.append({
            "tid": task_id,
            "j": json.dumps(s),
            "start_ts": _as_float(start.get("timestamp")),
            "start_frame": _as_int(start.get("frame_nr")),
            "end_ts": _as_float(end.get("timestamp")),
            "end_frame": _as_int(end.get("frame_nr")),
            "player_id": _as_int(s.get("player_id")),
            "valid": bool(s.get("valid")) if s.get("valid") is not None else None,
            "serve": bool(s.get("serve")) if s.get("serve") is not None else None,
            "swing_type": (s.get("swing_type") or None),
            "volley": bool(s.get("volley")) if s.get("volley") is not None else None,
            "is_in_rally": bool(s.get("is_in_rally")) if s.get("is_in_rally") is not None else None,
            "rally": json.dumps(s.get("rally")) if s.get("rally") is not None else None,
            "ball_hit": json.dumps(s.get("ball_hit")) if s.get("ball_hit") is not None else None,
            "confidence_swing_type": _as_float(s.get("confidence_swing_type")),
            "confidence": _as_float(s.get("confidence")),
            "confidence_volley": _as_float(s.get("confidence_volley")),
            "ball_hit_location": json.dumps(s.get("ball_hit_location")) if s.get("ball_hit_location") is not None else None,
            "ball_player_distance": _as_float(s.get("ball_player_distance")),
            "ball_speed": _as_float(s.get("ball_speed")),
            "ball_impact_location": json.dumps(s.get("ball_impact_location")) if s.get("ball_impact_location") is not None else None,
            "ball_impact_type": (s.get("ball_impact_type") or None),
            "intercepting_player_id": _as_int(s.get("intercepting_player_id")),
            "ball_trajectory": json.dumps(s.get("ball_trajectory")) if s.get("ball_trajectory") is not None else None,
            "annotations": json.dumps(s.get("annotations")) if s.get("annotations") is not None else None,
        })
    if not rows: return 0

    conn.execute(sql_text("""
        INSERT INTO bronze.player_swing (
            task_id, data,
            start_ts, start_frame, end_ts, end_frame,
            player_id, valid, serve, swing_type, volley, is_in_rally,
            rally, ball_hit,
            confidence_swing_type, confidence, confidence_volley,
            ball_hit_location, ball_player_distance, ball_speed,
            ball_impact_location, ball_impact_type, intercepting_player_id,
            ball_trajectory, annotations
        ) VALUES (
            :tid, CAST(:j AS JSONB),
            :start_ts, :start_frame, :end_ts, :end_frame,
            :player_id, :valid, :serve, :swing_type, :volley, :is_in_rally,
            CAST(:rally AS JSONB), CAST(:ball_hit AS JSONB),
            :confidence_swing_type, :confidence, :confidence_volley,
            CAST(:ball_hit_location AS JSONB), :ball_player_distance, :ball_speed,
            CAST(:ball_impact_location AS JSONB), :ball_impact_type, :intercepting_player_id,
            CAST(:ball_trajectory AS JSONB), CAST(:annotations AS JSONB)
        )
    """), rows)
    return len(rows)

def _apply_transforms_and_strip(conn, task_id: str):
    """
    Populate flattened columns from data, then strip mapped keys so data only contains leftovers (or NULL).
    Keep this minimal & table-local; add keys only when you truly need them downstream.
    """

    # ---- ball_position (X,Y,timestamp -> x,y,timestamp)
    conn.execute(sql_text("""
        -- create columns if not exist
        ALTER TABLE bronze.ball_position
            ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION GENERATED ALWAYS AS ((data->>'X')::double precision) STORED,
            ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION GENERATED ALWAYS AS ((data->>'Y')::double precision) STORED,
            ADD COLUMN IF NOT EXISTS timestamp DOUBLE PRECISION GENERATED ALWAYS AS ((data->>'timestamp')::double precision) STORED;

        -- populate columns
        UPDATE bronze.ball_position
           SET x = COALESCE(x, (data->>'X')::double precision),
               y = COALESCE(y, (data->>'Y')::double precision),
               timestamp = COALESCE(timestamp, (data->>'timestamp')::double precision)
         WHERE task_id = :tid AND data IS NOT NULL;
    """), {"tid": task_id})

    conn.execute(sql_text("""
        -- strip mapped keys, set data=NULL if empty
        UPDATE bronze.ball_position
           SET data = NULLIF(
                 COALESCE(data, '{}'::jsonb) - 'X' - 'Y' - 'timestamp',
                 '{}'::jsonb
               )
         WHERE task_id = :tid;
    """), {"tid": task_id})

    # ---- player_position (X,Y,court_X,court_Y,timestamp)
    conn.execute(sql_text("""
        ALTER TABLE bronze.player_position
          ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_x DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_y DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS timestamp DOUBLE PRECISION;

        UPDATE bronze.player_position
           SET x = COALESCE(x, (data->>'X')::double precision),
               y = COALESCE(y, (data->>'Y')::double precision),
               court_x = COALESCE(court_x, (data->>'court_X')::double precision),
               court_y = COALESCE(court_y, (data->>'court_Y')::double precision),
               timestamp = COALESCE(timestamp, (data->>'timestamp')::double precision)
         WHERE task_id = :tid AND data IS NOT NULL;
    """), {"tid": task_id})

    conn.execute(sql_text("""
        UPDATE bronze.player_position
           SET data = NULLIF(
                 COALESCE(data, '{}'::jsonb) - 'X' - 'Y' - 'court_X' - 'court_Y' - 'timestamp',
                 '{}'::jsonb
               )
         WHERE task_id = :tid;
    """), {"tid": task_id})

    # ---- ball_bounce (type, frame_nr, player_id, timestamp, court_pos, image_pos)
    conn.execute(sql_text("""
        ALTER TABLE bronze.ball_bounce
          ADD COLUMN IF NOT EXISTS type TEXT,
          ADD COLUMN IF NOT EXISTS frame_nr INT,
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS timestamp DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_pos JSONB,
          ADD COLUMN IF NOT EXISTS image_pos JSONB;

        UPDATE bronze.ball_bounce
           SET type = COALESCE(type, data->>'type'),
               frame_nr = COALESCE(frame_nr, NULLIF(data->>'frame_nr','')::int),
               player_id = COALESCE(player_id, NULLIF(data->>'player_id','')::int),
               timestamp = COALESCE(timestamp, NULLIF(data->>'timestamp','')::double precision),
               court_pos = COALESCE(court_pos, data->'court_pos'),
               image_pos = COALESCE(image_pos, data->'image_pos')
         WHERE task_id = :tid AND data IS NOT NULL;
    """), {"tid": task_id})

    conn.execute(sql_text("""
        UPDATE bronze.ball_bounce
           SET data = NULLIF(
                 COALESCE(data, '{}'::jsonb) - 'type' - 'frame_nr' - 'player_id' - 'timestamp' - 'court_pos' - 'image_pos',
                 '{}'::jsonb
               )
         WHERE task_id = :tid;
    """), {"tid": task_id})

    # ---- player (flat stats; swings moved to player_swing)
    conn.execute(sql_text("""
        ALTER TABLE bronze.player
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS activity_score DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS covered_distance DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS fastest_sprint DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS fastest_sprint_timestamp DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS location_heatmap JSONB,
          ADD COLUMN IF NOT EXISTS swing_count INT,
          ADD COLUMN IF NOT EXISTS swing_type_distribution JSONB;

        UPDATE bronze.player
           SET player_id = COALESCE(player_id, NULLIF(data->>'player_id','')::int),
               activity_score = COALESCE(activity_score, NULLIF(data->>'activity_score','')::double precision),
               covered_distance = COALESCE(covered_distance, NULLIF(data->>'covered_distance','')::double precision),
               fastest_sprint = COALESCE(fastest_sprint, NULLIF(data->>'fastest_sprint','')::double precision),
               fastest_sprint_timestamp = COALESCE(fastest_sprint_timestamp, NULLIF(data->>'fastest_sprint_timestamp','')::double precision),
               location_heatmap = COALESCE(location_heatmap, data->'location_heatmap'),
               swing_count = COALESCE(swing_count, NULLIF(data->>'swing_count','')::int),
               swing_type_distribution = COALESCE(swing_type_distribution, data->'swing_type_distribution')
         WHERE task_id = :tid AND data IS NOT NULL;
    """), {"tid": task_id})

    conn.execute(sql_text("""
        UPDATE bronze.player
           SET data = NULLIF(
                 COALESCE(data, '{}'::jsonb)
                   - 'player_id' - 'activity_score' - 'covered_distance'
                   - 'fastest_sprint' - 'fastest_sprint_timestamp'
                   - 'location_heatmap' - 'swing_count' - 'swing_type_distribution'
                   - 'swings' - 'strokes' - 'swing_events', -- explicitly drop nested swings present in some payloads
                 '{}'::jsonb
               )
         WHERE task_id = :tid;
    """), {"tid": task_id})

    # ---- player_swing (wide but we keep annotations/rally as JSONB)
    conn.execute(sql_text("""
        ALTER TABLE bronze.player_swing
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS valid BOOLEAN,
          ADD COLUMN IF NOT EXISTS serve BOOLEAN,
          ADD COLUMN IF NOT EXISTS swing_type TEXT,
          ADD COLUMN IF NOT EXISTS volley BOOLEAN,
          ADD COLUMN IF NOT EXISTS is_in_rally BOOLEAN,
          ADD COLUMN IF NOT EXISTS start JSONB,
          ADD COLUMN IF NOT EXISTS "end" JSONB,
          ADD COLUMN IF NOT EXISTS ball_hit JSONB,
          ADD COLUMN IF NOT EXISTS ball_hit_location JSONB,
          ADD COLUMN IF NOT EXISTS ball_player_distance DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS ball_speed DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS ball_impact_location JSONB,
          ADD COLUMN IF NOT EXISTS ball_impact_type TEXT,
          ADD COLUMN IF NOT EXISTS ball_trajectory JSONB,
          ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS confidence_swing_type DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS confidence_volley DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS intercepting_player_id INT,
          ADD COLUMN IF NOT EXISTS rally JSONB,
          ADD COLUMN IF NOT EXISTS annotations JSONB;

        UPDATE bronze.player_swing
           SET player_id = COALESCE(player_id, NULLIF(data->>'player_id','')::int),
               valid = COALESCE(valid, (data->>'valid')::boolean),
               serve = COALESCE(serve, (data->>'serve')::boolean),
               swing_type = COALESCE(swing_type, data->>'swing_type'),
               volley = COALESCE(volley, (data->>'volley')::boolean),
               is_in_rally = COALESCE(is_in_rally, (data->>'is_in_rally')::boolean),
               start = COALESCE(start, data->'start'),
               "end" = COALESCE("end", data->'end'),
               ball_hit = COALESCE(ball_hit, data->'ball_hit'),
               ball_hit_location = COALESCE(ball_hit_location, data->'ball_hit_location'),
               ball_player_distance = COALESCE(ball_player_distance, NULLIF(data->>'ball_player_distance','')::double precision),
               ball_speed = COALESCE(ball_speed, NULLIF(data->>'ball_speed','')::double precision),
               ball_impact_location = COALESCE(ball_impact_location, data->'ball_impact_location'),
               ball_impact_type = COALESCE(ball_impact_type, data->>'ball_impact_type'),
               ball_trajectory = COALESCE(ball_trajectory, data->'ball_trajectory'),
               confidence = COALESCE(confidence, NULLIF(data->>'confidence','')::double precision),
               confidence_swing_type = COALESCE(confidence_swing_type, NULLIF(data->>'confidence_swing_type','')::double precision),
               confidence_volley = COALESCE(confidence_volley, NULLIF(data->>'confidence_volley','')::double precision),
               intercepting_player_id = COALESCE(intercepting_player_id, NULLIF(data->>'intercepting_player_id','')::int),
               rally = COALESCE(rally, data->'rally'),
               annotations = COALESCE(annotations, data->'annotations')
         WHERE task_id = :tid AND data IS NOT NULL;
    """), {"tid": task_id})

    conn.execute(sql_text("""
        UPDATE bronze.player_swing
           SET data = NULLIF(
                 COALESCE(data, '{}'::jsonb)
                 - 'start' - 'end' - 'player_id' - 'valid' - 'serve' - 'swing_type' - 'volley'
                 - 'is_in_rally' - 'rally'
                 - 'ball_hit' - 'ball_hit_location' - 'ball_player_distance' - 'ball_speed'
                 - 'ball_impact_location' - 'ball_impact_type' - 'ball_trajectory'
                 - 'confidence' - 'confidence_swing_type' - 'confidence_volley'
                 - 'intercepting_player_id'
                 - 'annotations',
                 '{}'::jsonb
               )
         WHERE task_id = :tid;
    """), {"tid": task_id})

    # ---- rally (minimal facts; keep payload variability in data if needed)
    conn.execute(sql_text("""
        ALTER TABLE bronze.rally
          ADD COLUMN IF NOT EXISTS rally_id TEXT,
          ADD COLUMN IF NOT EXISTS start_ts DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS end_ts   DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS len_s    DOUBLE PRECISION;

        -- If your structure is { "value": { "id":..., "start":..., "end":... } }
        UPDATE bronze.rally
           SET rally_id = COALESCE(rally_id, data->'value'->>'id'),
               start_ts = COALESCE(start_ts, NULLIF(data->'value'->>'start','')::double precision),
               end_ts   = COALESCE(end_ts,   NULLIF(data->'value'->>'end','')::double precision),
               len_s    = COALESCE(len_s, CASE
                          WHEN (data->'value'->>'start') IS NOT NULL AND (data->'value'->>'end') IS NOT NULL
                          THEN (data->'value'->>'end')::double precision - (data->'value'->>'start')::double precision
                          ELSE NULL END)
         WHERE task_id = :tid AND data IS NOT NULL;
    """), {"tid": task_id})

    conn.execute(sql_text("""
        -- If you mapped everything you care about from 'value', strip it wholly; else keep it.
        UPDATE bronze.rally
           SET data = NULLIF(
                 COALESCE(data, '{}'::jsonb) - 'value',
                 '{}'::jsonb
               )
         WHERE task_id = :tid;
    """), {"tid": task_id})

    # ---- submission_context (only if you flattened columns for it)
    conn.execute(sql_text("""
        ALTER TABLE bronze.submission_context
          ADD COLUMN IF NOT EXISTS email TEXT,
          ADD COLUMN IF NOT EXISTS location TEXT,
          ADD COLUMN IF NOT EXISTS video_url TEXT,
          ADD COLUMN IF NOT EXISTS share_url TEXT,
          ADD COLUMN IF NOT EXISTS match_date DATE,
          ADD COLUMN IF NOT EXISTS start_time TEXT,
          ADD COLUMN IF NOT EXISTS player_a_name TEXT,
          ADD COLUMN IF NOT EXISTS player_b_name TEXT,
          ADD COLUMN IF NOT EXISTS player_a_utr TEXT,
          ADD COLUMN IF NOT EXISTS player_b_utr TEXT,
          ADD COLUMN IF NOT EXISTS customer_name TEXT;

        UPDATE bronze.submission_context
           SET email = COALESCE(email, data->>'email'),
               location = COALESCE(location, data->>'location'),
               video_url = COALESCE(video_url, data->>'video_url'),
               share_url = COALESCE(share_url, data->>'share_url'),
               match_date = COALESCE(match_date, NULLIF(data->>'match_date','')::date),
               start_time = COALESCE(start_time, data->>'start_time'),
               player_a_name = COALESCE(player_a_name, data->>'player_a_name'),
               player_b_name = COALESCE(player_b_name, data->>'player_b_name'),
               player_a_utr  = COALESCE(player_a_utr, data->>'player_a_utr'),
               player_b_utr  = COALESCE(player_b_utr, data->>'player_b_utr'),
               customer_name = COALESCE(customer_name, data->>'customer_name')
         WHERE task_id = :tid AND data IS NOT NULL;
    """), {"tid": task_id})

    conn.execute(sql_text("""
        UPDATE bronze.submission_context
           SET data = NULLIF(
                 COALESCE(data, '{}'::jsonb)
                 - 'email' - 'task_id' - 'location' - 'raw_meta' - 'share_url' - 'video_url'
                 - 'created_at' - 'match_date' - 'session_id' - 'start_time'
                 - 'player_a_utr' - 'player_b_utr' - 'customer_name'
                 - 'player_a_name' - 'player_b_name',
                 '{}'::jsonb
               )
         WHERE task_id = :tid;
    """), {"tid": task_id})

    # ---- performance-friendly indexes (idempotent)
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_ball_position_task_ts  ON bronze.ball_position (task_id, timestamp)"))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_player_position_task_ts ON bronze.player_position (task_id, timestamp)"))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_player_swing_task_pid ON bronze.player_swing (task_id, player_id)"))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_ball_bounce_task_ts   ON bronze.ball_bounce (task_id, timestamp)"))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_rally_task_start       ON bronze.rally (task_id, start_ts)"))


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
    counts["player"]       = _insert_players(conn, task_id, players)
    counts["player_swing"] = _insert_player_swings(conn, task_id, swing_rows)
    counts["rally"]              = _insert_json_array(conn, "rally", task_id, rallies)
    counts["ball_position"]      = _insert_json_array(conn, "ball_position", task_id, ball_positions)
    counts["ball_bounce"]        = _insert_json_array(conn, "ball_bounce", task_id, ball_bounces)
    counts["debug_event"]        = _insert_json_array(conn, "debug_event", task_id, debug_events)
    counts["unmatched_field"]    = _insert_json_array(conn, "unmatched_field", task_id, unmatched)
    counts["session_confidences"]= _upsert_single(conn, "session_confidences", task_id, confidences)
    counts["thumbnail"]          = _upsert_single(conn, "thumbnail", task_id, thumbnails)
    counts["highlight"]          = _upsert_single(conn, "highlight", task_id, highlights)
    counts["team_session"]       = _upsert_single(conn, "team_session", task_id, team_sessions)
    counts["bounce_heatmap"]     = _upsert_single(conn, "bounce_heatmap", task_id, bounce_heatmap)
   
     # After fan-out inserts, transpose columns and strip mapped keys (single source of truth = here)
    _apply_transforms_and_strip(conn, task_id)

    # handle player_positions: each item may be wrapped in dicts by player_id
    player_positions_raw = payload.get("player_positions")
    player_positions_flat = []
    if isinstance(player_positions_raw, dict):
        for v in player_positions_raw.values():
            if isinstance(v, list):
                player_positions_flat.extend(v)
    elif isinstance(player_positions_raw, list):
        player_positions_flat = player_positions_raw

    counts["player_position"] = _insert_json_array(conn, "player_position", task_id, player_positions_flat)

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
