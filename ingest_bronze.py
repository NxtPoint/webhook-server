# ingest_bronze.py — task_id-only bronze ingest (final, Nov 2025)
# Flow:
#   1) /bronze/ingest-from-url: fetch SportAI JSON, persist RAW (jsonb or gzip), then fan out to bronze towers
#   2) /bronze/ingest-json: same but payload posted directly
#   3) /bronze/reingest-from-raw: reload last RAW snapshot by task_id
#
# Contract:
#   - schema: bronze
#   - arrays: player, player_swing, rally, ball_position, ball_bounce, player_position, unmatched_field, debug_event
#   - singletons: session_confidences, thumbnail, highlight, team_session, bounce_heatmap, submission_context
#   - each array row has (id, task_id, data, created_at)
#   - data column holds leftover/unmapped keys ONLY; NULL if nothing left
# Hardened:
#   - Idempotent DDL (including raw_result new cols + chunk table)
#   - Transaction-scoped advisory locks (auto-release)
#   - Defensive JSON parsing & shape guards

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
    ph = _sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))[:10]
    return f"{task_id[:8]}-{ph}"

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

def _as_bool(x):
    if x is None: return None
    if isinstance(x, bool): return x
    s = str(x).strip().lower()
    if s in ("1","true","t","yes","y"): return True
    if s in ("0","false","f","no","n"): return False
    return None

def _clean_data(obj: dict | None, drop_keys: list[str]) -> dict | None:
    if not isinstance(obj, dict):
        return obj
    d = {k: v for k, v in obj.items() if k not in drop_keys}
    return d if d else None

def _generated_cols(conn, table: str, cols: list[str]) -> set[str]:
    if not cols:
        return set()
    rows = conn.execute(sql_text("""
        SELECT column_name, is_generated
          FROM information_schema.columns
         WHERE table_schema='bronze'
           AND table_name=:t
    """), {"t": table}).mappings().all()
    target = set(cols)
    return {
        r["column_name"]
        for r in rows
        if r["column_name"] in target and (r.get("is_generated") or "").upper() == "ALWAYS"
    }

# ---------------- init / DDL (idempotent) ----------------
def _run_bronze_init_conn(conn):
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze;"))

    # raw snapshot store
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
    # new columns used by code paths — safe, idempotent
    conn.execute(sql_text("""
        ALTER TABLE bronze.raw_result
          ADD COLUMN IF NOT EXISTS payload_len   INTEGER,
          ADD COLUMN IF NOT EXISTS chunked       BOOLEAN NOT NULL DEFAULT FALSE,
          ADD COLUMN IF NOT EXISTS chunk_count   INTEGER
    """))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_bronze_raw_result_task ON bronze.raw_result(task_id)"))

    # chunk table for very large gzips
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.raw_result_chunk (
            id BIGSERIAL PRIMARY KEY,
            task_id TEXT NOT NULL,
            part_nr INTEGER NOT NULL,
            data BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sql_text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_bronze_raw_chunk_task_part
          ON bronze.raw_result_chunk(task_id, part_nr)
    """))

    # session registry
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.session (
            task_id TEXT PRIMARY KEY,
            session_uid TEXT,
            meta JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))

    # arrays
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

    # singletons
    for t in ["session_confidences","thumbnail","highlight","team_session","bounce_heatmap","submission_context"]:
        conn.execute(sql_text(f"""
            CREATE TABLE IF NOT EXISTS bronze.{t} (
                task_id TEXT PRIMARY KEY,
                data JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))

    # typed columns (idempotent) — keep DDL tiny; inserts will populate values
    conn.execute(sql_text("""
        ALTER TABLE bronze.ball_position
        ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS "timestamp" DOUBLE PRECISION
    """))

    conn.execute(sql_text("""
        ALTER TABLE bronze.player_position
          ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_x DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_y DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS timestamp DOUBLE PRECISION
    """))

    conn.execute(sql_text("""
        ALTER TABLE bronze.ball_bounce
          ADD COLUMN IF NOT EXISTS type TEXT,
          ADD COLUMN IF NOT EXISTS frame_nr INT,
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS timestamp DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_pos JSONB,
          ADD COLUMN IF NOT EXISTS image_pos JSONB
    """))

    conn.execute(sql_text("""
        ALTER TABLE bronze.player
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS activity_score DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS covered_distance DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS fastest_sprint DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS fastest_sprint_timestamp DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS location_heatmap JSONB,
          ADD COLUMN IF NOT EXISTS swing_count INT,
          ADD COLUMN IF NOT EXISTS swing_type_distribution JSONB
    """))

    conn.execute(sql_text("""
        ALTER TABLE bronze.player_swing
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS valid BOOLEAN,
          ADD COLUMN IF NOT EXISTS serve BOOLEAN,
          ADD COLUMN IF NOT EXISTS swing_type TEXT,
          ADD COLUMN IF NOT EXISTS volley BOOLEAN,
          ADD COLUMN IF NOT EXISTS is_in_rally BOOLEAN,
          -- scalar timing columns used in INSERT
          ADD COLUMN IF NOT EXISTS start_ts DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS start_frame INT,
          ADD COLUMN IF NOT EXISTS end_ts DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS end_frame INT,
          -- original JSON blobs (kept for safety / future use)
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
          ADD COLUMN IF NOT EXISTS annotations JSONB
    """))

    # scalar generated columns
    conn.execute(sql_text("""
    ALTER TABLE bronze.ball_position
      ADD COLUMN IF NOT EXISTS x double precision
        GENERATED ALWAYS AS (NULLIF(data->>'X','')::double precision) STORED,
      ADD COLUMN IF NOT EXISTS y double precision
        GENERATED ALWAYS AS (NULLIF(data->>'Y','')::double precision) STORED,
      ADD COLUMN IF NOT EXISTS "timestamp" double precision
        GENERATED ALWAYS AS (NULLIF(data->>'timestamp','')::double precision) STORED;
    """))

    conn.execute(sql_text("""
    ALTER TABLE bronze.ball_bounce
      ADD COLUMN IF NOT EXISTS court_x double precision
        GENERATED ALWAYS AS ((court_pos->>0)::double precision) STORED,
      ADD COLUMN IF NOT EXISTS court_y double precision
        GENERATED ALWAYS AS ((court_pos->>1)::double precision) STORED,
      ADD COLUMN IF NOT EXISTS image_x double precision
        GENERATED ALWAYS AS ((image_pos->>0)::double precision) STORED,
      ADD COLUMN IF NOT EXISTS image_y double precision
        GENERATED ALWAYS AS ((image_pos->>1)::double precision) STORED;
    """))

def _run_bronze_init(conn=None):
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
            INSERT INTO bronze.raw_result (task_id, payload_json, payload_sha256, payload_len, chunked, chunk_count)
            VALUES (:tid, CAST(:j AS JSONB), :sha, :len, FALSE, NULL)
        """), {"tid": task_id, "j": s, "sha": sha, "len": len(s)})
    else:
        conn.execute(sql_text("""
            INSERT INTO bronze.raw_result (task_id, payload_gzip, payload_sha256, payload_len, chunked, chunk_count)
            VALUES (:tid, :gz, :sha, :len, FALSE, NULL)
        """), {"tid": task_id, "gz": _gzip_bytes(s), "sha": sha, "len": len(s)})

# --------------- fan-out helpers (all flatten-on-insert) ---------------
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

def _insert_players(conn, task_id: str, players: list) -> int:
    if not players: return 0
    rows = []
    drop = ["player_id","activity_score","covered_distance","fastest_sprint",
            "fastest_sprint_timestamp","swing_count","swing_type_distribution",
            "location_heatmap","swings","strokes","swing_events"]
    for p in players:
        if not isinstance(p, dict):
            continue
        j_clean = _clean_data(p, drop)
        rows.append({
            "tid": task_id,
            "j": json.dumps(j_clean) if j_clean is not None else None,
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
    drop = [
        "start","end","player_id","valid","serve","swing_type","volley","is_in_rally",
        "rally","ball_hit","confidence_swing_type","confidence","confidence_volley",
        "ball_hit_location","ball_player_distance","ball_speed","ball_impact_location",
        "ball_impact_type","intercepting_player_id","ball_trajectory","annotations",
    ]
    for s in swings:
        if not isinstance(s, dict):
            continue
        start = _as_dict(s.get("start"))
        end   = _as_dict(s.get("end"))
        j_clean = _clean_data(s, drop)
        rows.append({
            "tid": task_id,
            "j": json.dumps(j_clean) if j_clean is not None else None,
            "start_ts": _as_float(start.get("timestamp")) if start else None,
            "start_frame": _as_int(start.get("frame_nr")) if start else None,
            "end_ts": _as_float(end.get("timestamp")) if end else None,
            "end_frame": _as_int(end.get("frame_nr")) if end else None,
            "player_id": _as_int(s.get("player_id")),
            "valid": _as_bool(s.get("valid")),
            "serve": _as_bool(s.get("serve")),
            "swing_type": (s.get("swing_type") or None),
            "volley": _as_bool(s.get("volley")),
            "is_in_rally": _as_bool(s.get("is_in_rally")),
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

def _insert_ball_positions(conn, task_id: str, items: list) -> int:
    if not items:
        return 0
    rows = []
    drop = ["X", "Y", "timestamp"]
    for b in items:
        if not isinstance(b, dict):
            continue
        j_clean = {k: v for k, v in b.items() if k not in drop} or None
        rows.append({
            "tid": task_id,
            "j": json.dumps(j_clean) if j_clean is not None else None,
            "x":  _as_float(b.get("X")),
            "y":  _as_float(b.get("Y")),
            "ts": _as_float(b.get("timestamp")),
        })
    if not rows:
        return 0
    conn.execute(sql_text("""
        INSERT INTO bronze.ball_position (task_id, data, x, y, "timestamp")
        VALUES (:tid, CAST(:j AS JSONB), :x, :y, :ts)
    """), rows)
    return len(rows)

def _insert_player_positions(conn, task_id: str, items: list) -> int:
    if not items:
        return 0
    target_cols = ["x", "y", "court_x", "court_y", "timestamp"]
    gen = _generated_cols(conn, "player_position", target_cols)

    rows = []
    if gen:
        for it in items:
            if not isinstance(it, dict): continue
            rows.append({"tid": task_id, "j": json.dumps(it)})
        if not rows: return 0
        conn.execute(sql_text("""
            INSERT INTO bronze.player_position (task_id, data)
            VALUES (:tid, CAST(:j AS JSONB))
        """), rows)
        return len(rows)

    drop = ["X", "Y", "court_X", "court_Y", "timestamp"]
    for it in items:
        if not isinstance(it, dict): continue
        j_clean = {k: v for k, v in it.items() if k not in drop} or None
        rows.append({
            "tid": task_id,
            "j": json.dumps(j_clean) if j_clean is not None else None,
            "x":  it.get("X"),
            "y":  it.get("Y"),
            "cx": it.get("court_X"),
            "cy": it.get("court_Y"),
            "ts": it.get("timestamp"),
        })
    if not rows:
        return 0
    conn.execute(sql_text("""
        INSERT INTO bronze.player_position (task_id, data, x, y, court_x, court_y, timestamp)
        VALUES (:tid, CAST(:j AS JSONB), :x, :y, :cx, :cy, :ts)
    """), rows)
    return len(rows)

def _insert_ball_bounces(conn, task_id: str, items: list) -> int:
    if not items: return 0
    target_cols = ["type","frame_nr","player_id","timestamp","court_pos","image_pos"]
    gen = _generated_cols(conn, "ball_bounce", target_cols)

    rows = []
    if gen:
        for b in items:
            if not isinstance(b, dict): continue
            rows.append({"tid": task_id, "j": json.dumps(b)})
        if not rows: return 0
        conn.execute(sql_text("""
            INSERT INTO bronze.ball_bounce (task_id, data)
            VALUES (:tid, CAST(:j AS JSONB))
        """), rows)
        return len(rows)

    for b in items:
        if not isinstance(b, dict): continue
        j_clean = {k: v for k, v in b.items() if k not in target_cols} or None
        rows.append({
            "tid": task_id,
            "j": json.dumps(j_clean) if j_clean is not None else None,
            "type": b.get("type"),
            "frame_nr": _as_int(b.get("frame_nr")),
            "player_id": _as_int(b.get("player_id")),
            "ts": _as_float(b.get("timestamp")),
            "court_pos": json.dumps(b.get("court_pos")) if b.get("court_pos") is not None else None,
            "image_pos": json.dumps(b.get("image_pos")) if b.get("image_pos") is not None else None,
        })
    if not rows: return 0
    conn.execute(sql_text("""
        INSERT INTO bronze.ball_bounce
            (task_id, data, type, frame_nr, player_id, timestamp, court_pos, image_pos)
        VALUES
            (:tid, CAST(:j AS JSONB), :type, :frame_nr, :player_id, :ts, CAST(:court_pos AS JSONB), CAST(:image_pos AS JSONB))
    """), rows)
    return len(rows)

def _insert_rallies(conn, task_id: str, payload: dict) -> int:
    candidates = [
        payload.get("rallies"),
        payload.get("rally_events"),
        payload.get("rally"),
        payload.get("rally_segments"),
        (payload.get("statistics") or {}).get("rallies"),
    ]
    r = next((v for v in candidates if isinstance(v, (list, dict))), None)
    if isinstance(r, dict):
        r = r.get("rallies") or r.get("items") or r.get("data") or []
    out = []
    if isinstance(r, list):
        for x in r:
            out.append(x if isinstance(x, dict) else {"value": x})
    if not out:
        return 0

    rows = [{"tid": task_id, "j": json.dumps(x, ensure_ascii=False)} for x in out]
    conn.execute(sql_text("""
        INSERT INTO bronze.rally (task_id, data)
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

def _task_lock(conn, task_id: str):
    # transaction-scoped advisory lock; auto-released on commit/rollback
    conn.execute(sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:t, 42))"), {"t": task_id})

def _post_ingest_transforms(conn, task_id: str):
    _task_lock(conn, task_id)

    # rally columns add (idempotent)
    conn.execute(sql_text("""
        ALTER TABLE bronze.rally
        ADD COLUMN IF NOT EXISTS rally_id TEXT,
        ADD COLUMN IF NOT EXISTS start_ts DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS end_ts   DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS len_s    DOUBLE PRECISION
    """))

    # Top-level {id,start,end}
    conn.execute(sql_text("""
        UPDATE bronze.rally
        SET rally_id = COALESCE(rally_id, NULLIF(data->>'id','')),
            start_ts = COALESCE(start_ts, NULLIF(data->>'start','')::double precision),
            end_ts   = COALESCE(end_ts,   NULLIF(data->>'end','')::double precision)
        WHERE task_id = :tid AND data IS NOT NULL
          AND (data ? 'start' OR data ? 'end' OR data ? 'id')
    """), {"tid": task_id})

    # Wrapped object {value:{...}}
    conn.execute(sql_text("""
        UPDATE bronze.rally
        SET rally_id = COALESCE(rally_id, NULLIF(data->'value'->>'id','')),
            start_ts = COALESCE(start_ts, NULLIF(data->'value'->>'start','')::double precision),
            end_ts   = COALESCE(end_ts,   NULLIF(data->'value'->>'end','')::double precision)
        WHERE task_id = :tid AND data IS NOT NULL
          AND jsonb_typeof(data->'value') = 'object'
    """), {"tid": task_id})

    # Wrapped array {value:[start,end,(id)]}
    conn.execute(sql_text("""
        UPDATE bronze.rally
        SET start_ts = COALESCE(start_ts, NULLIF(data->'value'->>0,'')::double precision),
            end_ts   = COALESCE(end_ts,   NULLIF(data->'value'->>1,'')::double precision),
            rally_id = COALESCE(rally_id, NULLIF(data->'value'->>2,''))
        WHERE task_id = :tid AND data IS NOT NULL
          AND jsonb_typeof(data->'value') = 'array'
    """), {"tid": task_id})

    # Compute len_s
    conn.execute(sql_text("""
        UPDATE bronze.rally
        SET len_s = COALESCE(len_s,
                CASE WHEN start_ts IS NOT NULL AND end_ts IS NOT NULL
                     THEN end_ts - start_ts END)
        WHERE task_id = :tid
    """), {"tid": task_id})

    # Strip mapped keys
    conn.execute(sql_text("""
        UPDATE bronze.rally
        SET data = NULLIF(
                CASE
                WHEN data ? 'value' THEN
                    CASE
                    WHEN jsonb_typeof(data->'value')='object' THEN (COALESCE(data,'{}'::jsonb) - 'value')
                    WHEN jsonb_typeof(data->'value')='array'  THEN '{}'::jsonb
                    ELSE (COALESCE(data,'{}'::jsonb) - 'value')
                    END
                ELSE (COALESCE(data,'{}'::jsonb) - 'id' - 'start' - 'end')
                END,
                '{}'::jsonb)
        WHERE task_id = :tid
    """), {"tid": task_id})

    # NOTE: submission_context is now owned by upload_app in bronze.submission_context.
    # We intentionally do not touch it here anymore.

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
                  "thumbnail","highlight","team_session","bounce_heatmap"]:
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

    swing_rows = []
    for p in players:
        if not isinstance(p, dict): continue
        for k in ("swings","strokes","swing_events"):
            for s in _as_list(p.get(k)):
                if isinstance(s, dict): swing_rows.append(s)
        stats = _as_dict(p.get("statistics") or p.get("stats"))
        for k in ("swings","strokes","swing_events"):
            for s in _as_list(stats.get(k)):
                if isinstance(s, dict): swing_rows.append(s)

    player_positions_raw = payload.get("player_positions")
    player_positions_flat = []
    if isinstance(player_positions_raw, dict):
        for v in player_positions_raw.values():
            if isinstance(v, list):
                player_positions_flat.extend(v)
    elif isinstance(player_positions_raw, list):
        player_positions_flat = player_positions_raw

    counts = {}
    counts["player"]             = _insert_players(conn, task_id, players)
    counts["player_swing"]       = _insert_player_swings(conn, task_id, swing_rows)
    counts["rally"]              = _insert_rallies(conn, task_id, payload)
    counts["ball_position"]      = _insert_ball_positions(conn, task_id, ball_positions)
    counts["ball_bounce"]        = _insert_ball_bounces(conn, task_id, ball_bounces)
    counts["player_position"]    = _insert_player_positions(conn, task_id, player_positions_flat)
    counts["debug_event"]        = _insert_json_array(conn, "debug_event", task_id, debug_events:=debug_events)
    counts["unmatched_field"]    = _insert_json_array(conn, "unmatched_field", task_id, unmatched:=unmatched)
    counts["session_confidences"]= _upsert_single(conn, "session_confidences", task_id, confidences)
    counts["thumbnail"]          = _upsert_single(conn, "thumbnail", task_id, thumbnails)
    counts["highlight"]          = _upsert_single(conn, "highlight", task_id, highlights)
    counts["team_session"]       = _upsert_single(conn, "team_session", task_id, team_sessions)
    counts["bounce_heatmap"]     = _upsert_single(conn, "bounce_heatmap", task_id, bounce_heatmap)
    counts["submission_context"] = 0  # owned by upload_app, not touched here

    _post_ingest_transforms(conn, task_id)

    # Optionally return session_uid
    sid = conn.execute(sql_text("""
        SELECT session_uid FROM bronze.session WHERE task_id=:tid LIMIT 1
    """), {"tid": task_id}).scalar()
    return {"task_id": task_id, "session_id": sid, "counts": counts}

def _insert_json_array(conn, table: str, task_id: str, arr) -> int:
    if not arr: return 0
    rows = [{"tid": task_id, "j": json.dumps(x)} for x in arr if isinstance(x, dict)]
    if not rows: return 0
    conn.execute(sql_text(f"""
        INSERT INTO bronze.{table} (task_id, data)
        VALUES (:tid, CAST(:j AS JSONB))
    """), rows)
    return len(rows)

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
            _run_bronze_init(conn)
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
        if not task_id:
            task_id = _derive_task_id(payload, url)
        with engine.begin() as conn:
            _run_bronze_init(conn)
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
            _run_bronze_init(conn)

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

@ingest_bronze.post("/bronze/reingest-by-task-id")
def http_bronze_reingest_by_task_id():
    return http_bronze_reingest_from_raw()
