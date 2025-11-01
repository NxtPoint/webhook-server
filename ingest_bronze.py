# ingest_bronze.py — Nov 2025
# Fast-mode bronze ingest for NextPoint Development
# Canonical key: task_id (UUID)
# Guarantees:
#   • Raw store is gzipped in schema raw.raw_result
#   • Bronze is JSONB-first; one table per “tower” with (task_id, session_id, idx, doc)
#   • submission_context is always captured (fixes previous miss)
#   • Works with: (a) direct JSON POST; (b) fetch by task_id via env URL template; (c) reprocess from RAW
#   • Zero mutation of source payload; idx is positional per array order
#   • Idempotent upserts on (task_id, idx) and (task_id) where applicable

import os
import json
import gzip
import hashlib
from io import BytesIO
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple, List

import requests
from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text

from db_init import engine  # existing SQLAlchemy Engine

# ----------------------------
# Configuration
# ----------------------------
# Auth: simple ops key check (query param ?key= or Authorization: Bearer <key>)
OPS_KEY = os.getenv("OPS_KEY", "")
# External fetch: either template "https://.../result/{task_id}" or base + suffix
SPORTAI_RESULT_URL_TEMPLATE = os.getenv("SPORTAI_RESULT_URL_TEMPLATE", "")
SPORTAI_API_KEY = os.getenv("SPORTAI_API_KEY", "")
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "25"))

# Canonical list of known array towers (ingests dynamically anyway)
KNOWN_ARRAY_TOWERS = [
    "rallies",
    "ball_bounces",
    "ball_positions",
    "players",
    "swings",
    "events",
]
# Canonical object towers that should be singletons per task
KNOWN_OBJECT_TOWERS = [
    "submission_context",
    "session",
    "task",
    "meta",
]

ingest_bronze = Blueprint("ingest_bronze", __name__)

# ----------------------------
# Utilities
# ----------------------------

def _auth_ok(req) -> bool:
    key = req.args.get("key") or (req.headers.get("Authorization", "").replace("Bearer ", "").strip() or None)
    if not OPS_KEY:
        # If no OPS_KEY configured, allow (useful for local)
        return True
    return key == OPS_KEY


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _gzip_bytes(data: bytes) -> bytes:
    out = BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=9) as gz:
        gz.write(data)
    return out.getvalue()


def _gunzip_bytes(data: bytes) -> bytes:
    with gzip.GzipFile(fileobj=BytesIO(data), mode="rb") as gz:
        return gz.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _coerce_uuid(s: str) -> str:
    # light validation/normalization; DB constraint is the ultimate guard
    return s.strip()


def _extract_session_id(payload: Dict[str, Any]) -> Optional[int]:
    # Try common shapes; remain permissive
    cand = (
        payload.get("session_id")
        or (payload.get("session") or {}).get("id")
        or (payload.get("submission_context") or {}).get("session_id")
        or payload.get("sessionId")
    )
    try:
        return int(cand) if cand is not None else None
    except (TypeError, ValueError):
        return None


def _fetch_external_result(task_id: str) -> Dict[str, Any]:
    if not SPORTAI_RESULT_URL_TEMPLATE:
        raise RuntimeError("SPORTAI_RESULT_URL_TEMPLATE env not set; send JSON in request body or set template.")
    url = SPORTAI_RESULT_URL_TEMPLATE.format(task_id=task_id)
    headers = {"Accept": "application/json"}
    if SPORTAI_API_KEY:
        headers["Authorization"] = f"Bearer {SPORTAI_API_KEY}"
    r = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ----------------------------
# Schema DDL
# ----------------------------
SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS bronze;

-- Raw gzipped payloads keyed by task_id
CREATE TABLE IF NOT EXISTS raw.raw_result (
    task_id       UUID PRIMARY KEY,
    session_id    BIGINT,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sha256_hex    TEXT NOT NULL,
    payload_gzip  BYTEA NOT NULL
);

-- Generic JSONB-first bronze tables (one per tower)
-- Arrays: composite PK (task_id, idx); Objects: PK (task_id)
CREATE TABLE IF NOT EXISTS bronze.rallies (
    task_id    UUID NOT NULL,
    session_id BIGINT,
    idx        INT NOT NULL,
    doc        JSONB NOT NULL,
    PRIMARY KEY (task_id, idx)
);
CREATE TABLE IF NOT EXISTS bronze.ball_bounces (
    task_id    UUID NOT NULL,
    session_id BIGINT,
    idx        INT NOT NULL,
    doc        JSONB NOT NULL,
    PRIMARY KEY (task_id, idx)
);
CREATE TABLE IF NOT EXISTS bronze.ball_positions (
    task_id    UUID NOT NULL,
    session_id BIGINT,
    idx        INT NOT NULL,
    doc        JSONB NOT NULL,
    PRIMARY KEY (task_id, idx)
);
CREATE TABLE IF NOT EXISTS bronze.players (
    task_id    UUID NOT NULL,
    session_id BIGINT,
    idx        INT NOT NULL,
    doc        JSONB NOT NULL,
    PRIMARY KEY (task_id, idx)
);
CREATE TABLE IF NOT EXISTS bronze.swings (
    task_id    UUID NOT NULL,
    session_id BIGINT,
    idx        INT NOT NULL,
    doc        JSONB NOT NULL,
    PRIMARY KEY (task_id, idx)
);
CREATE TABLE IF NOT EXISTS bronze.events (
    task_id    UUID NOT NULL,
    session_id BIGINT,
    idx        INT NOT NULL,
    doc        JSONB NOT NULL,
    PRIMARY KEY (task_id, idx)
);

-- Object/singleton towers
CREATE TABLE IF NOT EXISTS bronze.submission_context (
    task_id    UUID PRIMARY KEY,
    session_id BIGINT,
    doc        JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS bronze.session (
    task_id    UUID PRIMARY KEY,
    session_id BIGINT,
    doc        JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS bronze.task (
    task_id    UUID PRIMARY KEY,
    session_id BIGINT,
    doc        JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS bronze.meta (
    task_id    UUID PRIMARY KEY,
    session_id BIGINT,
    doc        JSONB NOT NULL
);

-- Helpful indexes
CREATE INDEX IF NOT EXISTS idx_raw_session ON raw.raw_result (session_id);
CREATE INDEX IF NOT EXISTS idx_bronze_rallies_session ON bronze.rallies (session_id);
CREATE INDEX IF NOT EXISTS idx_bronze_ball_bounces_session ON bronze.ball_bounces (session_id);
CREATE INDEX IF NOT EXISTS idx_bronze_ball_positions_session ON bronze.ball_positions (session_id);
CREATE INDEX IF NOT EXISTS idx_bronze_players_session ON bronze.players (session_id);
CREATE INDEX IF NOT EXISTS idx_bronze_swings_session ON bronze.swings (session_id);
CREATE INDEX IF NOT EXISTS idx_bronze_events_session ON bronze.events (session_id);
CREATE INDEX IF NOT EXISTS idx_bronze_submission_context_session ON bronze.submission_context (session_id);
CREATE INDEX IF NOT EXISTS idx_bronze_session_session ON bronze.session (session_id);
"""

# ----------------------------
# Core ingest functions
# ----------------------------

def _upsert_raw(task_id: str, session_id: Optional[int], payload: Dict[str, Any]) -> None:
    data_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    gz = _gzip_bytes(data_bytes)
    sha = _sha256(gz)
    with engine.begin() as cx:
        cx.execute(sql_text(
            """
            INSERT INTO raw.raw_result(task_id, session_id, fetched_at, sha256_hex, payload_gzip)
            VALUES (:task_id::uuid, :session_id, now(), :sha, :gz)
            ON CONFLICT (task_id) DO UPDATE SET
              session_id=EXCLUDED.session_id,
              fetched_at=now(),
              sha256_hex=EXCLUDED.sha256_hex,
              payload_gzip=EXCLUDED.payload_gzip
            """
        ), {"task_id": task_id, "session_id": session_id, "sha": sha, "gz": gz})


def _clear_bronze_for_task(task_id: str) -> None:
    with engine.begin() as cx:
        for tbl in (
            "bronze.rallies","bronze.ball_bounces","bronze.ball_positions",
            "bronze.players","bronze.swings","bronze.events",
            "bronze.submission_context","bronze.session","bronze.task","bronze.meta",
        ):
            cx.execute(sql_text(f"DELETE FROM {tbl} WHERE task_id = :task_id::uuid"), {"task_id": task_id})


def _ingest_array_tower(task_id: str, session_id: Optional[int], arr: Iterable[Any], table: str) -> int:
    rows = []
    for i, item in enumerate(arr):
        rows.append({"task_id": task_id, "session_id": session_id, "idx": i, "doc": json.dumps(item)})
    if not rows:
        return 0
    # batch insert via VALUES
    values_sql = ",".join(["(:task_id::uuid, :session_id, :idx, :doc::jsonb)"] * len(rows))
    params: Dict[str, Any] = {"task_id": task_id, "session_id": session_id}
    # flatten idx/doc pairs
    flat_params: List[Any] = []
    for r in rows:
        flat_params.extend([r["idx"], r["doc"]])
    # Build parametrized statement safely
    # Use UNNEST with JSONB to keep things tidy
    with engine.begin() as cx:
        cx.execute(sql_text(
            f"""
            WITH data(idx, doc) AS (
              SELECT * FROM UNNEST(:idxs::int[], :docs::jsonb[])
            )
            INSERT INTO {table} (task_id, session_id, idx, doc)
            SELECT :task_id::uuid, :session_id, d.idx, d.doc FROM data d
            ON CONFLICT (task_id, idx) DO UPDATE SET doc = EXCLUDED.doc, session_id = EXCLUDED.session_id
            """
        ), {
            "task_id": task_id,
            "session_id": session_id,
            "idxs": [r["idx"] for r in rows],
            "docs": [json.loads(r["doc"]) for r in rows],
        })
    return len(rows)


def _ingest_object_tower(task_id: str, session_id: Optional[int], obj: Dict[str, Any], table: str) -> None:
    with engine.begin() as cx:
        cx.execute(sql_text(
            f"""
            INSERT INTO {table}(task_id, session_id, doc)
            VALUES (:task_id::uuid, :session_id, :doc::jsonb)
            ON CONFLICT (task_id) DO UPDATE SET doc = EXCLUDED.doc, session_id = EXCLUDED.session_id
            """
        ), {"task_id": task_id, "session_id": session_id, "doc": json.dumps(obj)})


def _ingest_bronze(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _extract_session_id(payload)

    # Always persist raw first (idempotent)
    _upsert_raw(task_id, session_id, payload)

    # Clear prior bronze rows for this task
    _clear_bronze_for_task(task_id)

    counts: Dict[str, int] = {}

    # Arrays – explicit known towers first
    for tower in KNOWN_ARRAY_TOWERS:
        if isinstance(payload.get(tower), list):
            counts[tower] = _ingest_array_tower(task_id, session_id, payload[tower], f"bronze.{tower}")

    # Objects – explicit known singleton towers
    for tower in KNOWN_OBJECT_TOWERS:
        if isinstance(payload.get(tower), dict):
            _ingest_object_tower(task_id, session_id, payload[tower], f"bronze.{tower}")
            counts[tower] = 1

    # Dynamic pass: any additional top-level arrays/objects not covered above
    for k, v in payload.items():
        if k in KNOWN_ARRAY_TOWERS or k in KNOWN_OBJECT_TOWERS:
            continue
        if isinstance(v, list) and v and isinstance(v[0], (dict, list)):
            # create table if missing (array form) – name-safe alnum+underscores only
            tbl = _safe_tower_name(k)
            _ensure_array_table(tbl)
            counts[tbl] = _ingest_array_tower(task_id, session_id, v, f"bronze.{tbl}")
        elif isinstance(v, dict):
            tbl = _safe_tower_name(k)
            _ensure_object_table(tbl)
            _ingest_object_tower(task_id, session_id, v, f"bronze.{tbl}")
            counts[tbl] = 1

    return {"task_id": task_id, "session_id": session_id, "counts": counts}


def _safe_tower_name(name: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", name).lower()
    if s[0].isdigit():
        s = f"t_{s}"
    return s


def _ensure_array_table(tower: str) -> None:
    with engine.begin() as cx:
        cx.execute(sql_text(
            f"""
            CREATE TABLE IF NOT EXISTS bronze.{tower} (
              task_id    UUID NOT NULL,
              session_id BIGINT,
              idx        INT NOT NULL,
              doc        JSONB NOT NULL,
              PRIMARY KEY (task_id, idx)
            );
            CREATE INDEX IF NOT EXISTS idx_{tower}_session ON bronze.{tower}(session_id);
            """
        ))


def _ensure_object_table(tower: str) -> None:
    with engine.begin() as cx:
        cx.execute(sql_text(
            f"""
            CREATE TABLE IF NOT EXISTS bronze.{tower} (
              task_id    UUID PRIMARY KEY,
              session_id BIGINT,
              doc        JSONB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_{tower}_session ON bronze.{tower}(session_id);
            """
        ))

# ----------------------------
# Back-compat shims (for older upload_app.py imports)
# ----------------------------
# Some older code expects these names. Keep them as no-ops/aliases.

def _run_bronze_init() -> bool:
    """Back-compat: initialize schemas/tables programmatically.
    Older code calls this at boot. Returns True on success."""
    with engine.begin() as cx:
        cx.execute(sql_text(SCHEMA_SQL))
    return True

# Older deployments imported a "strict" blueprint; alias to the current one.
ingest_bronze_strict = ingest_bronze


# ----------------------------
# Routes
# ----------------------------

@ingest_bronze.route("/bronze/init", methods=["GET"])
def bronze_init() -> Response:
    if not _auth_ok(request):
        return Response("Unauthorized", status=401)
    with engine.begin() as cx:
        cx.execute(sql_text(SCHEMA_SQL))
    return jsonify({"ok": True, "ts": _now_utc().isoformat()})


@ingest_bronze.route("/bronze/ingest", methods=["POST"])
def bronze_ingest() -> Response:
    if not _auth_ok(request):
        return Response("Unauthorized", status=401)

    # Modes: body.json_payload OR task_id to fetch
    body = request.get_json(silent=True) or {}
    task_id = _coerce_uuid(body.get("task_id") or request.args.get("task_id", "").strip())

    if not task_id:
        return Response("task_id required", status=400)

    payload: Optional[Dict[str, Any]] = body.get("json_payload")
    if payload is None:
        # Fetch externally via URL template
        try:
            payload = _fetch_external_result(task_id)
        except Exception as e:
            return jsonify({"ok": False, "error": f"fetch_failed: {e}"}), 502

    try:
        result = _ingest_bronze(task_id, payload)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@ingest_bronze.route("/bronze/reprocess-from-raw", methods=["POST"])
def bronze_reprocess_from_raw() -> Response:
    if not _auth_ok(request):
        return Response("Unauthorized", status=401)
    body = request.get_json(silent=True) or {}
    task_id = _coerce_uuid(body.get("task_id") or request.args.get("task_id", "").strip())
    if not task_id:
        return Response("task_id required", status=400)

    with engine.begin() as cx:
        row = cx.execute(sql_text(
            "SELECT payload_gzip FROM raw.raw_result WHERE task_id = :task_id::uuid"
        ), {"task_id": task_id}).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "raw_not_found"}), 404
        raw_bytes: bytes = row[0]

    try:
        payload = json.loads(_gunzip_bytes(raw_bytes))
        result = _ingest_bronze(task_id, payload)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@ingest_bronze.route("/bronze/debug-link", methods=["GET"])
def bronze_debug_link() -> Response:
    # utility: quick peek of what linked where
    if not _auth_ok(request):
        return Response("Unauthorized", status=401)
    task_id = request.args.get("task_id", "").strip()
    if not task_id:
        return Response("task_id required", status=400)
    with engine.begin() as cx:
        raw_row = cx.execute(sql_text(
            "SELECT session_id, fetched_at FROM raw.raw_result WHERE task_id=:t::uuid"
        ), {"t": task_id}).fetchone()
        subs_row = cx.execute(sql_text(
            "SELECT session_id FROM bronze.submission_context WHERE task_id=:t::uuid"
        ), {"t": task_id}).fetchone()
        rallies = cx.execute(sql_text(
            "SELECT count(*) FROM bronze.rallies WHERE task_id=:t::uuid"
        ), {"t": task_id}).scalar()
    return jsonify({
        "task_id": task_id,
        "raw": {"session_id": raw_row[0] if raw_row else None, "fetched_at": raw_row[1].isoformat() if raw_row else None},
        "submission_context_session": subs_row[0] if subs_row else None,
        "rallies_count": rallies
    })


# ----------------------------
# Minimal WSGI glue (optional)
# ----------------------------
# If you mount this file directly, you can do:
#   from flask import Flask
#   app = Flask(__name__)
#   app.register_blueprint(ingest_bronze)
#   if __name__ == "__main__":
#       app.run(host="0.0.0.0", port=8080, debug=False)
