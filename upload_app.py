# upload_app.py
# NextPoint Upload & Ingest service
# Restored scaffolding with health, upload UI, Dropbox upload, SportAI webhook,
# raw -> bronze loaders, recon, and ops utilities.
# Non-critical integrations degrade gracefully if their env isn’t set.

from __future__ import annotations

import os
import io
import json
import time
import uuid
import hmac
import hashlib
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from flask import (
    Flask, jsonify, request, Response, send_from_directory,
    render_template, abort
)

# --- Boot tag (helps us prove which commit is running) ------------------------
BOOT_TAG = os.getenv("DEPLOY_TAG", os.getenv("RENDER_GIT_COMMIT", "local"))[:7] or "local"
SERVICE_NAME = "upload_app"
print(f"=== BOOT {SERVICE_NAME} === tag={BOOT_TAG}")

app = Flask(__name__, template_folder="templates", static_folder="static")

# -----------------------------------------------------------------------------
# Config / environment
# -----------------------------------------------------------------------------
OPS_KEY = os.getenv("OPS_KEY", "")  # gatekeeper for /ops/* endpoints
DATABASE_URL = os.getenv("DATABASE_URL", "")
DROPBOX_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "")
DROPBOX_FOLDER = os.getenv("DROPBOX_UPLOAD_FOLDER", "/incoming")
SPORTAI_TOKEN = os.getenv("SPORT_AI_TOKEN", os.getenv("SPORTAI_TOKEN", ""))  # optional

# Soft dependencies (DB, Dropbox) are lazy-imported so app still boots if missing
_sqla = None
_dp_sdk = None

def _now_utc_iso() -> str:
    return dt.datetime.utcnow().isoformat()

def _json(obj, code=200):
    return Response(json.dumps(obj, ensure_ascii=False, default=str), status=code, mimetype="application/json")

def _err(msg: str, code=400):
    return _json({"ok": False, "error": msg}, code)

def _guard_ok() -> bool:
    """Ops gate. Supports query ?key=.. or ?ops_key=.. or header Authorization: Bearer <key> or X-OPS-Key."""
    qk = request.args.get("key") or request.args.get("ops_key")
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        bearer = bearer.split(" ", 1)[1].strip()
    hk = request.headers.get("X-OPS-Key") or bearer
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

def _require_ops() -> Optional[Response]:
    if not _guard_ok():
        return Response("Forbidden", 403)
    return None

# -----------------------------------------------------------------------------
# Optional: DB engine (lazy)
# -----------------------------------------------------------------------------
def _db():
    """Return SQLAlchemy engine (lazy)."""
    global _sqla
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    if _sqla is None:
        try:
            from sqlalchemy import create_engine, text  # type: ignore
            _sqla = create_engine(
                DATABASE_URL,
                pool_pre_ping=True,
                pool_recycle=300,
                future=True,
            )
            # quick ping
            with _sqla.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as e:
            raise RuntimeError(f"SQLAlchemy unavailable or DB error: {e}")
    return _sqla

# Small helper to run SQL safely
def _run_sql(sql: str, params: Optional[Dict[str, Any]] = None, autocommit: bool = False):
    from sqlalchemy import text  # lazy import inside
    eng = _db()
    if autocommit:
        with eng.begin() as c:
            return c.execute(text(sql), params or {})
    else:
        with eng.connect() as c:
            return c.execute(text(sql), params or {})

# -----------------------------------------------------------------------------
# Optional: Dropbox client (lazy)
# -----------------------------------------------------------------------------
def _dropbox():
    global _dp_sdk
    if not DROPBOX_TOKEN:
        raise RuntimeError("DROPBOX_ACCESS_TOKEN not set")
    if _dp_sdk is None:
        try:
            import dropbox  # type: ignore
            _dp_sdk = dropbox.Dropbox(DROPBOX_TOKEN, timeout=60)
            # Test auth
            _dp_sdk.users_get_current_account()
        except Exception as e:
            raise RuntimeError(f"Dropbox SDK unavailable or auth failed: {e}")
    return _dp_sdk

# -----------------------------------------------------------------------------
# Health, diagnostics, route dump
# -----------------------------------------------------------------------------
@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/healthz")
def healthz_ok():
    return _json({"ok": True, "service": SERVICE_NAME, "tag": BOOT_TAG})

@app.get("/__whoami")
def __whoami():
    port = os.getenv("PORT", "10000")
    return _json({
        "ok": True, "service": SERVICE_NAME, "tag": BOOT_TAG,
        "port": port,
        "render_service": os.getenv("RENDER_SERVICE_ID"),
        "commit": os.getenv("RENDER_GIT_COMMIT"),
        "branch": os.getenv("RENDER_GIT_BRANCH"),
    })

@app.get("/__routes")
def __routes_open():
    routes = [
        {
            "rule": r.rule,
            "endpoint": r.endpoint,
            "methods": sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})
        }
        for r in app.url_map.iter_rules()
    ]
    routes.sort(key=lambda x: x["rule"])
    return _json({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def __routes_locked():
    if (resp := _require_ops()) is not None:
        return resp
    return __routes_open()

# -----------------------------------------------------------------------------
# Upload UI (template fallback kept so deploys never 404)
# -----------------------------------------------------------------------------
INLINE_UPLOAD = """<!doctype html>
<meta charset="utf-8">
<title>Upload</title>
<body style="font-family:system-ui;background:#0b1220;color:#fff;padding:24px">
<h2>🎾 NextPoint – Upload</h2>
<p>Inline fallback UI. If <code>templates/upload.html</code> exists, it will be used instead.</p>
<form id="f">
  <input type="email" name="email" placeholder="Email" required style="padding:6px"> 
  <input type="file" name="file" required style="padding:6px">
  <button style="padding:6px 10px">Upload</button>
</form>
<pre id="s" style="white-space:pre-wrap"></pre>
<script>
const f = document.getElementById('f'); 
const s = document.getElementById('s');
f.onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(f);
  s.textContent = 'Uploading...';
  const r = await fetch('/api/upload', { method:'POST', body: fd });
  const j = await r.json();
  s.textContent = JSON.stringify(j, null, 2);
};
</script>
"""

@app.get("/upload")
@app.get("/upload/")
def upload_page():
    try:
        return render_template("upload.html")
    except Exception:
        return Response(INLINE_UPLOAD, mimetype="text/html")

@app.get("/upload/static/<path:filename>")
def upload_static(filename: str):
    base = os.path.join(app.root_path, "static", "upload")
    return send_from_directory(base, filename)

# -----------------------------------------------------------------------------
# Upload API -> Dropbox
# -----------------------------------------------------------------------------
@app.post("/api/upload")
def api_upload():
    """
    Accepts a single file and an email; uploads to Dropbox at:
      {DROPBOX_UPLOAD_FOLDER}/{email}/{YYYY}/{MM}/{DD}/{ts}_{safe_name}
    Returns the Dropbox path + idempotency token.
    """
    if not request.files or "file" not in request.files:
        return _err("file missing")
    email = request.form.get("email", "").strip().lower()
    if not email:
        return _err("email missing")

    f = request.files["file"]
    filename = f.filename or "upload.bin"
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    safe_name = filename.replace("\\", "/").split("/")[-1]
    dp_path = f"{DROPBOX_FOLDER.rstrip('/')}/{email}/{ts[:4]}/{ts[4:6]}/{ts[6:8]}/{ts}_{safe_name}"
    idem = str(uuid.uuid4())

    # if Dropbox not configured, report a simulated path so UI continues to work
    if not DROPBOX_TOKEN:
        print("[upload] DROPBOX_ACCESS_TOKEN not set; simulating upload")
        size = f.stream.seek(0, io.SEEK_END) or 0
        f.stream.seek(0)
        return _json({
            "ok": True,
            "simulated": True,
            "path": dp_path,
            "size": size,
            "idempotency": idem,
        })

    try:
        dbx = _dropbox()
        data = f.stream.read()
        dbx.files_upload(data, dp_path, mode=_dropbox().files.WriteMode("add"))
        return _json({"ok": True, "path": dp_path, "idempotency": idem})
    except Exception as e:
        return _err(f"dropbox upload failed: {e}", 500)

# -----------------------------------------------------------------------------
# SportAI webhook -> store JSON into raw_result (then we can load raw/bronze)
# -----------------------------------------------------------------------------
def _check_sportai_signature(body: bytes) -> bool:
    """Optional verification: HMAC-SHA256 using SPORTAI_TOKEN."""
    if not SPORTAI_TOKEN:
        return True  # no shared secret => accept
    sig_hdr = request.headers.get("X-Signature", "")
    if not sig_hdr:
        return False
    mac = hmac.new(SPORTAI_TOKEN.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, sig_hdr)

@app.post("/webhooks/sportai")
def webhooks_sportai():
    """
    Receives JSON payloads from SportAI. We store them raw in `raw_result`
    with a generated `session_uid` if not present.
    """
    body = request.get_data() or b""
    if not _check_sportai_signature(body):
        return _err("invalid signature", 403)
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return _err("invalid json body")

    if not DATABASE_URL:
        # Accept and log even without DB; helps testing
        print("[sportai] DB not configured; payload accepted (simulated)")
        return _json({"ok": True, "simulated": True})

    session_uid = (
        payload.get("session_uid")
        or payload.get("sessionId")
        or str(uuid.uuid4())
    )
    try:
        _ensure_raw_tables()
        _insert_raw_result(session_uid, payload)
        return _json({"ok": True, "session_uid": session_uid})
    except Exception as e:
        return _err(f"store raw_result failed: {e}", 500)

def _ensure_raw_tables():
    """
    Create minimal raw_result & dim_session if missing.
    (You can replace this with your full DDL bootstrap.)
    """
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS dim_session (
            session_id BIGSERIAL PRIMARY KEY,
            session_uid TEXT UNIQUE NOT NULL,
            created_utc TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS raw_result (
            raw_id BIGSERIAL PRIMARY KEY,
            session_id BIGINT NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
            payload JSONB NOT NULL,
            created_utc TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ]
    for s in ddl:
        _run_sql(s, autocommit=True)

def _insert_raw_result(session_uid: str, payload: Dict[str, Any]):
    # ensure session row
    _run_sql("""
        INSERT INTO dim_session(session_uid)
        VALUES (:u)
        ON CONFLICT (session_uid) DO NOTHING;
    """, {"u": session_uid}, autocommit=True)

    _run_sql("""
        INSERT INTO raw_result(session_id, payload)
        SELECT session_id, CAST(:p AS JSONB) FROM dim_session WHERE session_uid=:u;
    """, {"u": session_uid, "p": json.dumps(payload)}, autocommit=True)

# -----------------------------------------------------------------------------
# ETL: raw -> bronze (7 towers). These are placeholders you can swap with your
# production SQL. They’re written idempotently and fast to iterate.
# -----------------------------------------------------------------------------
def _ensure_bronze_tables():
    ddl = [
        # Replace with your actual bronze DDLs / materialized views
        "CREATE TABLE IF NOT EXISTS bronze_player (session_id BIGINT, player_uid TEXT, data JSONB);",
        "CREATE TABLE IF NOT EXISTS bronze_rally (session_id BIGINT, rally_id BIGINT, data JSONB);",
        "CREATE TABLE IF NOT EXISTS bronze_swing (session_id BIGINT, swing_id BIGINT, data JSONB);",
        "CREATE TABLE IF NOT EXISTS bronze_bounce (session_id BIGINT, bounce_id BIGINT, data JSONB);",
        "CREATE TABLE IF NOT EXISTS bronze_ball_position (session_id BIGINT, idx BIGINT, data JSONB);",
        "CREATE TABLE IF NOT EXISTS bronze_player_position (session_id BIGINT, idx BIGINT, data JSONB);",
        "CREATE TABLE IF NOT EXISTS bronze_snapshot (session_id BIGINT, idx BIGINT, data JSONB);",
    ]
    for s in ddl:
        _run_sql(s, autocommit=True)

def _truncate_bronze_for_session(session_uid: str):
    sqls = [
        "DELETE FROM bronze_player WHERE session_id=(SELECT session_id FROM dim_session WHERE session_uid=:u);",
        "DELETE FROM bronze_rally WHERE session_id=(SELECT session_id FROM dim_session WHERE session_uid=:u);",
        "DELETE FROM bronze_swing WHERE session_id=(SELECT session_id FROM dim_session WHERE session_uid=:u);",
        "DELETE FROM bronze_bounce WHERE session_id=(SELECT session_id FROM dim_session WHERE session_uid=:u);",
        "DELETE FROM bronze_ball_position WHERE session_id=(SELECT session_id FROM dim_session WHERE session_uid=:u);",
        "DELETE FROM bronze_player_position WHERE session_id=(SELECT session_id FROM dim_session WHERE session_uid=:u);",
        "DELETE FROM bronze_snapshot WHERE session_id=(SELECT session_id FROM dim_session WHERE session_uid=:u);",
    ]
    for s in sqls:
        _run_sql(s, {"u": session_uid}, autocommit=True)

def _load_bronze_from_raw(session_uid: str) -> Dict[str, int]:
    """
    Example loader: copies raw JSON into bronze tables as-is.
    Replace with your transforms (parsing payload into normalized bronze).
    """
    _ensure_bronze_tables()
    _truncate_bronze_for_session(session_uid)

    # SAMPLE: fan out raw JSON arrays if present; otherwise store payload snapshot.
    counts = {"player": 0, "rally": 0, "swing": 0, "bounce": 0, "ball_position": 0, "player_position": 0, "snapshot": 0}
    rows = _run_sql("""
        SELECT r.payload
        FROM raw_result r
        JOIN dim_session s USING(session_id)
        WHERE s.session_uid=:u
        ORDER BY r.raw_id;
    """, {"u": session_uid}).mappings().all()

    if not rows:
        return counts

    payloads = [dict(r)["payload"] for r in rows]
    # Choose the last payload for simplicity
    payload = payloads[-1] if payloads else {}

    def _inserts(tbl: str, arr: Any, keyname: str):
        nonlocal counts
        if not isinstance(arr, list):
            return
        for idx, item in enumerate(arr):
            _run_sql(
                f"""
                INSERT INTO {tbl}(session_id, {keyname}, data)
                SELECT s.session_id, :k, CAST(:j AS JSONB)
                FROM dim_session s
                WHERE s.session_uid=:u;
                """,
                {"u": session_uid, "k": idx, "j": json.dumps(item)},
                autocommit=True
            )
        counts[tbl.replace("bronze_", "").replace("_", " " if "_" in tbl else "")] = len(arr)

    # Map your JSON schema here. This is just a placeholder.
    _inserts("bronze_player", payload.get("players") or payload.get("player", []), "player_uid")
    _inserts("bronze_rally", payload.get("rallies", []), "rally_id")
    _inserts("bronze_swing", payload.get("swings", []), "swing_id")
    _inserts("bronze_bounce", payload.get("bounces", []), "bounce_id")
    _inserts("bronze_ball_position", payload.get("ball_positions", []), "idx")
    _inserts("bronze_player_position", payload.get("player_positions", []), "idx")
    _inserts("bronze_snapshot", payload.get("snapshots", []), "idx")

    return counts

# -----------------------------------------------------------------------------
# Recon: compare counts across JSON/raw/bronze for a session
# -----------------------------------------------------------------------------
def _bronze_counts(session_uid: str) -> Dict[str, int]:
    q = """
    WITH sid AS (SELECT session_id FROM dim_session WHERE session_uid=:u)
    SELECT
      (SELECT count(*) FROM bronze_player p JOIN sid s ON p.session_id=s.session_id) AS players,
      (SELECT count(*) FROM bronze_rally r  JOIN sid s ON r.session_id=s.session_id) AS rallies,
      (SELECT count(*) FROM bronze_swing sw JOIN sid s ON sw.session_id=s.session_id) AS swings,
      (SELECT count(*) FROM bronze_bounce b JOIN sid s ON b.session_id=s.session_id) AS bounces,
      (SELECT count(*) FROM bronze_ball_position bp JOIN sid s ON bp.session_id=s.session_id) AS ball_positions,
      (SELECT count(*) FROM bronze_player_position pp JOIN sid s ON pp.session_id=s.session_id) AS player_positions,
      (SELECT count(*) FROM bronze_snapshot sn JOIN sid s ON sn.session_id=s.session_id) AS snapshots
    """
    row = _run_sql(q, {"u": session_uid}).first()
    if not row:
        return {}
    keys = ["players", "rallies", "swings", "bounces", "ball_positions", "player_positions", "snapshots"]
    return {k: int(getattr(row, k)) for k in keys}

@app.get("/ops/recon-session")
def ops_recon_session():
    if (resp := _require_ops()) is not None:
        return resp
    session_uid = request.args.get("session_uid", "")
    if not session_uid:
        return _err("session_uid required")
    if not DATABASE_URL:
        return _err("DATABASE_URL not set", 500)
    try:
        bc = _bronze_counts(session_uid)
        return _json({"ok": True, "session_uid": session_uid, "bronze": bc})
    except Exception as e:
        return _err(str(e), 500)

# -----------------------------------------------------------------------------
# Ops endpoints: DB ping, load raw->bronze, manual ingest, delete session, etc.
# -----------------------------------------------------------------------------
@app.get("/ops/db-ping")
def ops_db_ping():
    if (resp := _require_ops()) is not None:
        return resp
    try:
        _run_sql("SELECT now() AT TIME ZONE 'utc' AS now_utc")
        return _json({"ok": True, "now_utc": _now_utc_iso()})
    except Exception as e:
        return _json({"ok": False, "error": f"{e}"})

@app.post("/ops/ingest-json")
def ops_ingest_json():
    if (resp := _require_ops()) is not None:
        return resp
    if not DATABASE_URL:
        return _err("DATABASE_URL not set", 500)
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return _err("invalid json body")
    session_uid = payload.get("session_uid") or str(uuid.uuid4())
    try:
        _ensure_raw_tables()
        _insert_raw_result(session_uid, payload)
        return _json({"ok": True, "session_uid": session_uid})
    except Exception as e:
        return _err(f"ingest-json failed: {e}", 500)

@app.post("/ops/build-bronze")
def ops_build_bronze():
    if (resp := _require_ops()) is not None:
        return resp
    session_uid = request.args.get("session_uid", "")
    if not session_uid:
        return _err("session_uid required")
    if not DATABASE_URL:
        return _err("DATABASE_URL not set", 500)
    try:
        _ensure_raw_tables()
        _ensure_bronze_tables()
        counts = _load_bronze_from_raw(session_uid)
        recon = _bronze_counts(session_uid)
        return _json({"ok": True, "session_uid": session_uid, "loaded": counts, "recon": recon})
    except Exception as e:
        return _err(f"build-bronze failed: {e}", 500)

@app.post("/ops/delete-session")
def ops_delete_session():
    if (resp := _require_ops()) is not None:
        return resp
    session_uid = request.args.get("session_uid", "")
    if not session_uid:
        return _err("session_uid required")
    if not DATABASE_URL:
        return _err("DATABASE_URL not set", 500)
    try:
        _run_sql("DELETE FROM dim_session WHERE session_uid=:u", {"u": session_uid}, autocommit=True)
        return _json({"ok": True, "deleted": session_uid})
    except Exception as e:
        return _err(f"delete failed: {e}", 500)

# -----------------------------------------------------------------------------
# Mount optional UI blueprint (your admin pages)
# -----------------------------------------------------------------------------
try:
    from ui_app import ui_bp  # noqa: E402
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print(f"ui_bp not mounted: {e}")

# -----------------------------------------------------------------------------
# Final: log routes on boot (one-time)
# -----------------------------------------------------------------------------
print("=== ROUTES (final) ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    methods = ",".join(sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}))
    print(f"{r.rule:30s} -> {r.endpoint:24s} [{methods}]")
print("=== END ROUTES ===")
