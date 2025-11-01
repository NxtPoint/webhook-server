# ingest_bronze.py — minimal, aligned with upload_app.py + db_init.py
# Responsibilities:
#   1) Ensure RAW table exists (public.raw_result)
#   2) Extract session_id from SportAI JSON
#   3) Persist RAW (JSONB or GZIP) with session_id
#   4) Call db_init.ingest_all_for_session(conn, session_id, payload)
#   5) Expose Blueprint (for /bronze/init), _run_bronze_init, and ingest_bronze_strict

import os, json, gzip, hashlib
from typing import Any, Dict, Optional
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response
from sqlalchemy import text as sql_text

from db_init import engine, ingest_all_for_session  # uses your provided orchestrator

ingest_bronze = Blueprint("ingest_bronze", __name__)
OPS_KEY = os.getenv("OPS_KEY", "").strip()

# ---------- tiny auth ----------
def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    # If OPS_KEY set, require match; if unset (local/dev), allow through
    return (not OPS_KEY) or supplied == OPS_KEY

def _forbid():
    return Response("Forbidden", 403)

# ---------- helpers ----------
def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _extract_session_id(payload: Dict[str, Any]) -> Optional[int]:
    # SportAI places the numeric session id in a few places; be permissive
    candidates = [
        payload.get("session_id"),
        (payload.get("session") or {}).get("id"),
        (payload.get("session") or {}).get("session_id"),
        (payload.get("metadata") or {}).get("session_id"),
        payload.get("sessionId"),
        (payload.get("data") or {}).get("session_id"),
    ]
    for c in candidates:
        if c is None:
            continue
        try:
            sid = int(str(c))
            if sid > 0:
                return sid
        except Exception:
            pass
    return None

def _persist_raw(conn, session_id: int, payload: Dict[str, Any], size_threshold: int = 5_000_000) -> None:
    # Store JSON into public.raw_result with session_id + sha; JSONB if small, GZIP if large
    js = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sha = _sha256(js)

    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS raw_result (
          id             BIGSERIAL PRIMARY KEY,
          session_id     INT NOT NULL,
          payload_json   JSONB,
          payload_gzip   BYTEA,
          payload_sha256 TEXT,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))

    if len(js) <= size_threshold:
        conn.execute(sql_text("""
            INSERT INTO raw_result (session_id, payload_json, payload_sha256)
            VALUES (:sid, CAST(:j AS JSONB), :sha)
        """), {"sid": session_id, "j": js, "sha": sha})
    else:
        conn.execute(sql_text("""
            INSERT INTO raw_result (session_id, payload_gzip, payload_sha256)
            VALUES (:sid, :gz, :sha)
        """), {"sid": session_id, "gz": gzip.compress(js.encode("utf-8")), "sha": sha})

# ---------- public API expected by upload_app.py ----------
def _run_bronze_init(conn=None) -> bool:
    # kept for compatibility; we only guarantee raw_result exists here
    if conn is None:
        with engine.begin() as c:
            _run_bronze_init(c)
        return True
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS raw_result (
          id             BIGSERIAL PRIMARY KEY,
          session_id     INT NOT NULL,
          payload_json   JSONB,
          payload_gzip   BYTEA,
          payload_sha256 TEXT,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    # Helpful index for backfills / queries
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_raw_result_session_id ON raw_result(session_id)"))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_raw_result_created_at ON raw_result(created_at DESC)"))
    return True

def ingest_bronze_strict(
    conn,
    payload: Dict[str, Any],
    replace: bool = True,
    **kwargs
) -> Dict[str, Any]:
    """
    Minimal orchestrator expected by upload_app:
      - Extract session_id
      - Persist RAW
      - Fan-out to columnar bronze/dim/fact via db_init.ingest_all_for_session
    """
    session_id = _extract_session_id(payload)
    if not session_id:
        raise ValueError("SportAI payload missing numeric session_id")

    # Always keep a RAW snapshot first
    _persist_raw(conn, session_id, payload)

    # Columnar ingest (your db_init.py)
    summary = ingest_all_for_session(conn, session_id, payload)

    # Return shape that upload_app expects (session_id is the important one)
    return {"session_id": session_id, "summary": summary}

# ---------- tiny route for readiness ----------
@ingest_bronze.get("/bronze/init")
def http_bronze_init():
    if not _guard(): return _forbid()
    try:
        with engine.begin() as c:
            _run_bronze_init(c)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500
