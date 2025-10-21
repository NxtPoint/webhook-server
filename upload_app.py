# upload_app.py
# Unified upload + SportAI ingest + Bronze population (lossless) + admin UI.
# - Keeps your upload flow (check -> presign -> submit -> poll) with S3 pre-signed uploads
# - Adds raw payload storage (raw_result.payload_json)
# - Calls db_init.ingest_all_for_session to populate Bronze towers + dim/fact
# - Exposes / and /health for Render
# - Mounts your UI blueprint at /upload

import os
import json
import time
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection

# --- optional CORS (safe no-op if not installed)
try:
    from flask_cors import CORS  # type: ignore
except Exception:  # pragma: no cover
    CORS = None  # type: ignore

# --- db + bronze ingest
from db_init import engine, ingest_all_for_session

# --- optional admin UI blueprint (your ui_app.py)
try:
    from ui_app import ui_bp
except Exception:
    ui_bp = None  # type: ignore

# --- optional boto3 for S3 pre-signing
try:
    import boto3  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None  # type: ignore


app = Flask(__name__)
if CORS:
    CORS(app, resources={r"/*": {"origins": "*"}})

SERVICE_NAME = os.getenv("SERVICE_NAME", "sportai-api")
DEFAULT_TIMEOUT = int(os.getenv("SPORTAI_HTTP_TIMEOUT", "600"))

# ---------------------------------------------------------------------------
# Helpers: raw_result persistence (so backfill always works)
# ---------------------------------------------------------------------------
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
    _ensure_raw_result_schema(conn)
    blob = json.dumps(payload_dict, ensure_ascii=False)
    sha  = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    conn.execute(sql_text("""
        INSERT INTO raw_result (session_id, session_uid, doc_type, source, payload_json, payload_sha256)
        VALUES (:sid, :suid, :dt, :src, CAST(:payload AS jsonb), :sha)
        ON CONFLICT DO NOTHING
    """), {"sid": session_id, "suid": session_uid, "dt": doc_type, "src": source, "payload": blob, "sha": sha})

def _update_raw_result_session_id(conn: Connection, *, session_id: int, session_uid: Optional[str]) -> None:
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

# ---------------------------------------------------------------------------
# Health + root (Render readiness)
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return jsonify(ok=True, service=SERVICE_NAME, ts=datetime.now(timezone.utc).isoformat())

@app.get("/")
def root():
    return jsonify(ok=True, service=SERVICE_NAME)

# ---------------------------------------------------------------------------
# SportAI: direct webhook (SportAI posts the final JSON here)
# ---------------------------------------------------------------------------
@app.post("/sportai/result")
def sportai_result():
    """
    SportAI -> POST final JSON to this endpoint.
    We save the payload to raw_result, populate Bronze, and link raw row to session_id.
    """
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            return jsonify(ok=False, error="Invalid JSON object"), 400

        suid = _detect_session_uid(payload)
        sid_hint = _detect_session_id(payload)

        with engine.begin() as conn:
            _store_raw_payload(conn, payload_dict=payload, session_id=sid_hint, session_uid=suid,
                               doc_type="sportai.result", source="webhook:/sportai/result")
            summary = _ingest_all(conn, payload)
            _update_raw_result_session_id(conn, session_id=summary["session_id"], session_uid=suid)

        return jsonify(ok=True, summary=summary)
    except HTTPException as he:
        raise he
    except Exception as e:
        app.logger.exception("sportai_result failed")
        return jsonify(ok=False, error=str(e)), 500

# ---------------------------------------------------------------------------
# SportAI: ingest by result_url (ops/manual)
# Body: { "result_url": "https://...", "submission_context": {...} }
# ---------------------------------------------------------------------------
@app.post("/ops/ingest-task")
def ops_ingest_task():
    try:
        data = request.get_json(force=True, silent=False) or {}
        result_url = data.get("result_url")
        if not result_url:
            return jsonify(ok=False, error="Missing result_url"), 400

        r = requests.get(result_url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict):
            return jsonify(ok=False, error="Result is not a JSON object"), 400

        suid = _detect_session_uid(payload)
        sid_hint = _detect_session_id(payload)
        submission_context = data.get("submission_context")

        with engine.begin() as conn:
            _store_raw_payload(conn, payload_dict=payload, session_id=sid_hint, session_uid=suid,
                               doc_type="sportai.result", source=result_url)

            # (optional) store submission_context you send alongside the task
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
                conn.execute(sql_text("""
                    INSERT INTO submission_context (session_id, data)
                    VALUES (COALESCE(:sid, 0), CAST(:d AS jsonb))
                    ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
                """), {"sid": sid_hint, "d": json.dumps(submission_context)})

            summary = _ingest_all(conn, payload)
            _update_raw_result_session_id(conn, session_id=summary["session_id"], session_uid=suid)

            if isinstance(submission_context, dict) and submission_context:
                conn.execute(sql_text("""
                    INSERT INTO submission_context (session_id, data, ingest_finished_at, ingest_error)
                    VALUES (:sid, CAST(:d AS jsonb), now(), NULL)
                    ON CONFLICT (session_id) DO UPDATE
                      SET data = EXCLUDED.data,
                          ingest_finished_at = now(),
                          ingest_error = NULL;
                """), {"sid": summary["session_id"], "d": json.dumps(submission_context)})
                conn.execute(sql_text("DELETE FROM submission_context WHERE session_id = 0"))

        return jsonify(ok=True, summary=summary)
    except HTTPException as he:
        raise he
    except Exception as e:
        app.logger.exception("ops_ingest_task failed")
        return jsonify(ok=False, error=str(e)), 500

# ---------------------------------------------------------------------------
# Submission Context (standalone upsert if your UI posts it independently)
# ---------------------------------------------------------------------------
@app.post("/submission-context")
def submission_context_upsert():
    try:
        body = request.get_json(force=True, silent=False) or {}
        sid = body.get("session_id")
        sc  = body.get("submission_context")
        if not isinstance(sc, dict) or not sc:
            return jsonify(ok=False, error="submission_context must be an object"), 400

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

# ---------------------------------------------------------------------------
# Upload API (frontend flow)
#   1) /upload/api/check-video     -> quick validation
#   2) /upload/api/presign         -> S3 pre-signed POST (or error if not configured)
#   3) /upload/api/submit          -> create SportAI task from uploaded URL
#   4) /upload/api/task-status     -> poll task; on complete, fetch JSON, store raw, ingest Bronze
# ---------------------------------------------------------------------------
def _allowed_ext() -> Tuple[str, ...]:
    return (".mp4", ".mov", ".mkv", ".avi", ".m4v")

def _max_upload_mb() -> int:
    return int(os.getenv("MAX_CONTENT_MB", os.getenv("MAX_UPLOAD_MB", "150")))

@app.post("/upload/api/check-video")
@app.post("/api/check-video")  # alias
def api_check_video():
    try:
        data = request.get_json(silent=True) or request.values
        name = (data.get("fileName") or data.get("filename") or "").strip()
        size = data.get("sizeMB") or data.get("fileSizeMB") or 0
        try:
            size = float(size)
        except Exception:
            size = 0.0

        reasons = []
        if not name:
            reasons.append("Missing file name.")
        elif not name.lower().endswith(_allowed_ext()):
            reasons.append(f"Unsupported file type. Allowed: {', '.join(_allowed_ext())}")
        if size and size > _max_upload_mb():
            reasons.append(f"File is larger than allowed limit of {_max_upload_mb()} MB.")

        return jsonify(ok=len(reasons) == 0, reasons=reasons, max_mb=_max_upload_mb())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.post("/upload/api/presign")
def api_presign():
    """
    Returns a pre-signed POST for S3 the frontend can use to upload the video file.
    Body: { "fileName":"match.mp4" }
    """
    try:
        if not boto3:
            return jsonify(ok=False, error="boto3 not installed on server"), 500

        data = request.get_json(force=True, silent=False) or {}
        file_name = (data.get("fileName") or "").strip()
        if not file_name:
            return jsonify(ok=False, error="fileName required"), 400

        bucket = os.getenv("UPLOAD_S3_BUCKET")
        region = os.getenv("AWS_REGION", "us-east-1")
        if not bucket:
            return jsonify(ok=False, error="UPLOAD_S3_BUCKET not set"), 500

        key_prefix = os.getenv("UPLOAD_S3_PREFIX", "incoming/")
        key = f"{key_prefix.rstrip('/')}/{int(time.time())}_{file_name}"

        s3 = boto3.client("s3", region_name=region)
        fields = {"acl": "private"}
        conditions = [["content-length-range", 0, _max_upload_mb() * 1024 * 1024]]

        presigned = s3.generate_presigned_post(
            Bucket=bucket,
            Key=key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=3600,
        )
        # where the uploaded file will be accessible by your backend (not necessarily public)
        video_url = f"s3://{bucket}/{key}"
        return jsonify(ok=True, presigned=presigned, video_url=video_url)
    except Exception as e:
        app.logger.exception("api_presign failed")
        return jsonify(ok=False, error=str(e)), 500

def _sportai_headers():
    token = os.getenv("SPORTAI_TOKEN") or os.getenv("SPORT_AI_TOKEN")
    if not token:
        raise RuntimeError("SPORTAI_TOKEN not set")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def _sportai_base():
    return os.getenv("SPORTAI_API_BASE", "https://api.sportai.example.com")

@app.post("/upload/api/submit")
def api_submit():
    """
    Body:
      { "video_url":"https://...", "metadata":{...}, "submission_context":{...} }
    Creates a SportAI job and returns a task_id (and optional result_url if immediate).
    """
    try:
        body = request.get_json(force=True, silent=False) or {}
        video_url = body.get("video_url")
        metadata   = body.get("metadata") or {}
        sub_ctx    = body.get("submission_context") or {}

        if not video_url:
            return jsonify(ok=False, error="video_url required"), 400

        # Call SportAI to create a processing task
        create_url = f"{_sportai_base().rstrip('/')}/v1/jobs"
        resp = requests.post(create_url, headers=_sportai_headers(),
                             json={"video_url": video_url, "metadata": metadata}, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        job = resp.json()  # expected: {"task_id": "..."} or {"id": "..."}
        task_id = job.get("task_id") or job.get("id")

        # Optionally save submission context now (0 sid placeholder)
        if isinstance(sub_ctx, dict) and sub_ctx:
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
                conn.execute(sql_text("""
                    INSERT INTO submission_context (session_id, data)
                    VALUES (0, CAST(:d AS jsonb))
                    ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
                """), {"d": json.dumps(sub_ctx)})

        return jsonify(ok=True, task_id=task_id)
    except HTTPException as he:
        raise he
    except Exception as e:
        app.logger.exception("api_submit failed")
        return jsonify(ok=False, error=str(e)), 500

@app.get("/upload/api/task-status")
def api_task_status():
    """
    Query SportAI job status. If complete and a result_url exists,
    fetch JSON, persist raw, and ingest Bronze immediately.
    Query: ?task_id=xxx
    """
    try:
        task_id = request.args.get("task_id")
        if not task_id:
            return jsonify(ok=False, error="task_id required"), 400

        status_url = f"{_sportai_base().rstrip('/')}/v1/jobs/{task_id}"
        r = requests.get(status_url, headers=_sportai_headers(), timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        status = r.json()

        # If job finished and we have a result_url, hydrate Bronze
        result_url = status.get("result_url") or status.get("result") or status.get("links", {}).get("result")
        if result_url:
            rr = requests.get(result_url, timeout=DEFAULT_TIMEOUT)
            rr.raise_for_status()
            payload = rr.json()
            if isinstance(payload, dict):
                suid = _detect_session_uid(payload)
                sid_hint = _detect_session_id(payload)
                with engine.begin() as conn:
                    _store_raw_payload(conn, payload_dict=payload, session_id=sid_hint, session_uid=suid,
                                       doc_type="sportai.result", source=result_url)
                    summary = _ingest_all(conn, payload)
                    _update_raw_result_session_id(conn, session_id=summary["session_id"], session_uid=suid)
                status["bronze_summary"] = summary  # helpful for UI

        return jsonify(ok=True, status=status)
    except HTTPException as he:
        raise he
    except Exception as e:
        app.logger.exception("api_task_status failed")
        return jsonify(ok=False, error=str(e)), 500

# ---------------------------------------------------------------------------
# Internal: call unified Bronze ingest and return compact summary
# ---------------------------------------------------------------------------
def _ingest_all(conn: Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    sid = _detect_session_id(payload) or -1
    summary = ingest_all_for_session(conn, sid, payload)
    return {
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

# ---------------------------------------------------------------------------
# Mount admin UI blueprint (your ui_app.py)
# ---------------------------------------------------------------------------
if ui_bp is not None:
    try:
        app.register_blueprint(ui_bp, url_prefix="/upload")
    except Exception as e:
        app.logger.warning("ui blueprint not mounted: %s", e)

# ---------------------------------------------------------------------------
# Local run (Render uses wsgi.py with python wsgi.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
