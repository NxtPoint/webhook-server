# upload_app.py
# Unified ingestion app for SportAI -> Postgres Bronze.
# - Persists raw payloads in raw_result (payload_json) for replay/backfill
# - Ingests *all* Bronze towers + dim/fact via db_init.ingest_all_for_session
# - Handles direct JSON webhook and "fetch-by-URL" task flow
# - Captures submission_context from your frontend

import os
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify
from werkzeug.exceptions import HTTPException

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection

# ---- our unified Bronze ingest + engine
from db_init import engine, ingest_all_for_session  # noqa: E402

# ------------------------------------------------------------------------------
# App
# ------------------------------------------------------------------------------
app = Flask(__name__)

# mount the admin UI
from ui_app import ui_bp
app.register_blueprint(ui_bp, url_prefix="/upload")

# -----------------------------------------------------------
# Video pre-check (used by the Upload & Submit page)
# Accepts JSON like: { "fileName": "match.mp4", "sizeMB": 123.4 }
# Returns: { ok: true/false, reasons: [], max_mb: 150 }
# -----------------------------------------------------------
@app.post("/check")
@app.post("/upload/check")  # alias in case the page calls relative to /upload
def check_video():
    try:
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("fileName") or data.get("filename") or "").strip()
        size_mb = float(data.get("sizeMB") or data.get("fileSizeMB") or 0)

        max_mb = int(os.getenv("MAX_CONTENT_MB", os.getenv("MAX_UPLOAD_MB", "150")))
        allowed_ext = (".mp4", ".mov", ".mkv", ".avi", ".m4v")

        reasons = []
        if not name:
            reasons.append("Missing file name.")
        elif not name.lower().endswith(allowed_ext):
            reasons.append(f"Unsupported file type. Allowed: {', '.join(allowed_ext)}")

        if size_mb and size_mb > max_mb:
            reasons.append(f"File is larger than allowed limit of {max_mb} MB.")

        ok = len(reasons) == 0
        return jsonify(ok=ok, reasons=reasons, max_mb=max_mb)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400
# -----------------------------------------------------------
# Video pre-check (used by the Upload & Submit page)
# Accepts JSON or form: { fileName, sizeMB } (sizeMB optional)
# Returns: { ok, reasons[], max_mb }
# We expose multiple aliases to catch whatever the UI calls.
# -----------------------------------------------------------
def _parse_check_payload():
    # Try JSON first
    data = request.get_json(silent=True) or {}
    # Fallback to form/query
    if not data:
        data = {
            "fileName": request.values.get("fileName") or request.values.get("filename"),
            "sizeMB": request.values.get("sizeMB") or request.values.get("fileSizeMB"),
        }
    name = (data.get("fileName") or "").strip()
    size_mb_raw = data.get("sizeMB")
    try:
        size_mb = float(size_mb_raw) if size_mb_raw not in (None, "", "null") else 0.0
    except Exception:
        size_mb = 0.0
    return name, size_mb

def _check_logic():
    name, size_mb = _parse_check_payload()
    max_mb = int(os.getenv("MAX_CONTENT_MB", os.getenv("MAX_UPLOAD_MB", "150")))
    allowed_ext = (".mp4", ".mov", ".mkv", ".avi", ".m4v")
    reasons = []
    if not name:
        reasons.append("Missing file name.")
    elif not name.lower().endswith(allowed_ext):
        reasons.append(f"Unsupported file type. Allowed: {', '.join(allowed_ext)}")
    if size_mb and size_mb > max_mb:
        reasons.append(f"File is larger than allowed limit of {max_mb} MB.")
    ok = len(reasons) == 0
    # helpful log while we stabilize front-end paths
    app.logger.info("pre-check name=%s sizeMB=%s ok=%s reasons=%s", name, size_mb, ok, reasons)
    return jsonify(ok=ok, reasons=reasons, max_mb=max_mb)

@app.post("/check")
@app.post("/check-video")
@app.post("/api/check")
@app.post("/api/check-video")
@app.post("/upload/check")
@app.post("/upload/check-video")
@app.post("/upload/api/check")
@app.post("/upload/api/check-video")
def check_video():
    try:
        return _check_logic()
    except Exception as e:
        app.logger.exception("check endpoint failed")
        return jsonify(ok=False, error=str(e)), 400

# Optional: simple CORS (relaxed; tighten in prod as needed)
try:
    from flask_cors import CORS
    CORS(app, resources={r"/*": {"origins": "*"}})
except Exception:
    pass

SERVICE_NAME = os.getenv("SERVICE_NAME", "sportai-api")
DEFAULT_TIMEOUT = int(os.getenv("SPORTAI_HTTP_TIMEOUT", "600"))

# ------------------------------------------------------------------------------
# Helpers: raw_result persistence
# ------------------------------------------------------------------------------
def _ensure_raw_result_schema(conn: Connection) -> None:
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS raw_result (
          raw_result_id   BIGSERIAL PRIMARY KEY,
          created_at      timestamptz NOT NULL DEFAULT now(),
          session_id      int,
          session_uid     text,
          doc_type        text,
          source          text,
          payload_json    jsonb,
          payload         jsonb,
          payload_gzip    bytea,
          payload_sha256  text
        );
    """))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS raw_result_session_id_idx ON raw_result(session_id)"))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS raw_result_session_uid_idx ON raw_result(session_uid)"))

def _store_raw_payload(
    conn: Connection,
    *,
    payload_dict: Dict[str, Any],
    session_id: Optional[int] = None,
    session_uid: Optional[str] = None,
    doc_type: Optional[str] = None,
    source: Optional[str] = None,
) -> None:
    """Persist raw JSON once per (session_id, doc_type) when available; use content hash to avoid dupes."""
    _ensure_raw_result_schema(conn)
    blob = json.dumps(payload_dict, ensure_ascii=False)
    sha  = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    conn.execute(sql_text("""
        INSERT INTO raw_result (session_id, session_uid, doc_type, source, payload_json, payload_sha256)
        VALUES (:sid, :suid, :dt, :src, CAST(:payload AS jsonb), :sha)
        ON CONFLICT DO NOTHING
    """), {"sid": session_id, "suid": session_uid, "dt": doc_type, "src": source, "payload": blob, "sha": sha})

def _update_raw_result_session_id(conn: Connection, *, session_id: int, session_uid: Optional[str]) -> None:
    """Attach numeric session_id to any previously-saved rows by uid."""
    if not session_uid:
        return
    conn.execute(sql_text("""
        UPDATE raw_result
           SET session_id = :sid
         WHERE session_id IS NULL
           AND session_uid = :suid
    """), {"sid": session_id, "suid": session_uid})

def _detect_session_uid(payload: Dict[str, Any]) -> Optional[str]:
    return payload.get("session_uid") or payload.get("sessionId") or payload.get("uid")

def _detect_session_id(payload: Dict[str, Any]) -> Optional[int]:
    sid = payload.get("session_id") or payload.get("sessionId")
    try:
        return int(sid) if sid is not None else None
    except Exception:
        return None

# ------------------------------------------------------------------------------
# Health
# ------------------------------------------------------------------------------
@app.get("/health")
def health():
    return jsonify(ok=True, service=SERVICE_NAME, ts=datetime.now(timezone.utc).isoformat())

@app.get("/")
def root():
    return jsonify(ok=True, service=SERVICE_NAME)

# ------------------------------------------------------------------------------
# Ingest path 1: direct JSON webhook from SportAI
# ------------------------------------------------------------------------------
@app.post("/sportai/result")
def sportai_result():
    """
    SportAI posts the final JSON here.
    We:
      1) save raw to raw_result
      2) ingest Bronze towers + dim/fact
      3) link raw_result row to numeric session_id
    """
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            return jsonify(error="Invalid JSON; expected object"), 400

        suid = _detect_session_uid(payload)
        sid_hint = _detect_session_id(payload)

        with engine.begin() as conn:
            _store_raw_payload(
                conn,
                payload_dict=payload,
                session_id=sid_hint,
                session_uid=suid,
                doc_type="sportai.result",
                source="webhook:/sportai/result",
            )
            summary = _ingest_all(conn, payload)
            # ensure linkage by uid after ingest
            _update_raw_result_session_id(conn, session_id=summary["session_id"], session_uid=suid)

        return jsonify({"ok": True, "summary": summary})
    except HTTPException as he:
        raise he
    except Exception as e:
        app.logger.exception("sportai_result failed")
        return jsonify(ok=False, error=str(e)), 500

# ------------------------------------------------------------------------------
# Ingest path 2: fetch-by-URL (ops task) – we GET result_url, then ingest
# ------------------------------------------------------------------------------
@app.post("/ops/ingest-task")
def ops_ingest_task():
    """
    Body:
      {
        "result_url": "https://.../result.json",
        "task_id": "optional",
        "submission_context": { ... }   # optional: save alongside
      }
    """
    try:
        data = request.get_json(force=True, silent=False) or {}
        result_url = data.get("result_url")
        if not result_url:
            return jsonify(error="Missing result_url"), 400

        # 1) fetch JSON from SportAI
        r = requests.get(result_url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict):
            return jsonify(error="Fetched payload is not a JSON object"), 400

        suid = _detect_session_uid(payload)
        sid_hint = _detect_session_id(payload)

        # 2) optionally persist submission_context from caller
        submission_context = data.get("submission_context")
        task_id = data.get("task_id")

        with engine.begin() as conn:
            # save raw first
            _store_raw_payload(
                conn,
                payload_dict=payload,
                session_id=sid_hint,
                session_uid=suid,
                doc_type="sportai.result",
                source=result_url,
            )

            # save submission_context if present (you said this table is perfect)
            if isinstance(submission_context, dict) and submission_context:
                conn.execute(sql_text("""
                    CREATE TABLE IF NOT EXISTS submission_context (
                      session_id int PRIMARY KEY,
                      data jsonb,
                      created_at timestamptz DEFAULT now(),
                      ingest_finished_at timestamptz,
                      ingest_error text
                    );
                """))
                # Allow pre-session save using a temp session_id of NULL; we key by session_id on upsert later.
                conn.execute(sql_text("""
                    INSERT INTO submission_context (session_id, data)
                    VALUES (COALESCE(:sid, 0), CAST(:d AS jsonb))
                    ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
                """), {"sid": sid_hint, "d": json.dumps(submission_context)})

            # 3) Bronze ingest
            summary = _ingest_all(conn, payload)

            # 4) Link raw_result rows by uid and stamp submission_context with final session_id
            _update_raw_result_session_id(conn, session_id=summary["session_id"], session_uid=suid)

            # If we saved submission_context with a 0 sid, move it to real sid
            if isinstance(submission_context, dict) and submission_context:
                conn.execute(sql_text("""
                    INSERT INTO submission_context (session_id, data, ingest_finished_at, ingest_error)
                    VALUES (:sid, CAST(:d AS jsonb), now(), NULL)
                    ON CONFLICT (session_id) DO UPDATE
                      SET data = EXCLUDED.data,
                          ingest_finished_at = now(),
                          ingest_error = NULL;
                    -- clean any temp row
                """), {"sid": summary["session_id"], "d": json.dumps(submission_context)})
                conn.execute(sql_text("DELETE FROM submission_context WHERE session_id = 0"))

        return jsonify({"ok": True, "summary": summary})
    except HTTPException as he:
        raise he
    except Exception as e:
        app.logger.exception("ops_ingest_task failed")
        return jsonify(ok=False, error=str(e)), 500

# ------------------------------------------------------------------------------
# Submission context (frontend form) – standalone
# ------------------------------------------------------------------------------
@app.post("/submission-context")
def submission_context_upsert():
    """
    Body:
      {
        "session_id": 1234,              # optional at submit-time
        "submission_context": { ... }    # required
      }
    """
    try:
        body = request.get_json(force=True, silent=False) or {}
        sid = body.get("session_id")
        sc  = body.get("submission_context")
        if not isinstance(sc, dict) or not sc:
            return jsonify(error="submission_context must be a non-empty object"), 400

        with engine.begin() as conn:
            conn.execute(sql_text("""
                CREATE TABLE IF NOT EXISTS submission_context (
                  session_id int PRIMARY KEY,
                  data jsonb,
                  created_at timestamptz DEFAULT now(),
                  ingest_finished_at timestamptz,
                  ingest_error text
                );
            """))
            if sid is None:
                # stash under 0 until the ingest knows real session_id
                conn.execute(sql_text("""
                    INSERT INTO submission_context (session_id, data)
                    VALUES (0, CAST(:d AS jsonb))
                    ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
                """), {"d": json.dumps(sc)})
            else:
                conn.execute(sql_text("""
                    INSERT INTO submission_context (session_id, data)
                    VALUES (:sid, CAST(:d AS jsonb))
                    ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
                """), {"sid": int(sid), "d": json.dumps(sc)})
        return jsonify(ok=True)
    except HTTPException as he:
        raise he
    except Exception as e:
        app.logger.exception("submission_context_upsert failed")
        return jsonify(ok=False, error=str(e)), 500

# ------------------------------------------------------------------------------
# Internal: call unified Bronze ingest and return a compact summary
# ------------------------------------------------------------------------------
def _ingest_all(conn: Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Delegates to db_init.ingest_all_for_session, then returns a concise summary.
    """
    # The unified ingest decides/derives the final session_id.
    # If none in payload, it will still populate based on data it maps.
    temp_sid = _detect_session_id(payload) or -1  # hint only
    summary = ingest_all_for_session(conn, temp_sid, payload)

    out = {
        "session_id": summary.get("session_id"),
        "players": summary.get("players", 0),
        "rallies": summary.get("rallies", 0),
        "swings": summary.get("swings", 0),
        "ball_bounces": summary.get("ball_bounces", 0),
        "ball_positions": summary.get("ball_positions", 0),
        "player_positions": summary.get("player_positions", 0),
        "team_sessions": summary.get("team_sessions", 0),
        "highlights": summary.get("highlights", 0),
    }
    return out

# ------------------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------------------
@app.errorhandler(Exception)
def handle_errors(e):
    if isinstance(e, HTTPException):
        code = e.code or 500
        msg = getattr(e, "description", str(e))
        return jsonify(ok=False, error=msg), code
    app.logger.exception("Unhandled error")
    return jsonify(ok=False, error=str(e)), 500

# ------------------------------------------------------------------------------
# Local run
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
