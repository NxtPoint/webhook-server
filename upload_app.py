# upload_app.py
import os
import io
import json
import time
import uuid
import queue
import hashlib
import logging
import threading
import tempfile
import subprocess
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

import boto3
import requests

from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, Text, DateTime, JSON, Integer
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

# ----------------------------
# Config & Logging
# ----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("upload_app")

PORT = int(os.getenv("PORT", "8080"))
POSTGRES_DSN = os.getenv("POSTGRES_DSN")

AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET = os.getenv("S3_BUCKET")
S3_PREFIX = os.getenv("S3_PREFIX", "uploads/").strip("/")

SPORTAI_API_BASE = os.getenv("SPORTAI_API_BASE")
SPORTAI_API_KEY = os.getenv("SPORTAI_API_KEY")

QUALITY_MIN_WIDTH = int(os.getenv("QUALITY_MIN_WIDTH", "960"))
QUALITY_MIN_HEIGHT = int(os.getenv("QUALITY_MIN_HEIGHT", "540"))
QUALITY_MIN_FPS = int(os.getenv("QUALITY_MIN_FPS", "20"))
QUALITY_MIN_DURATION_SEC = int(os.getenv("QUALITY_MIN_DURATION_SEC", "6"))

RESULT_POLL_INTERVAL_SEC = int(os.getenv("RESULT_POLL_INTERVAL_SEC", "10"))
RESULT_POLL_TIMEOUT_SEC = int(os.getenv("RESULT_POLL_TIMEOUT_SEC", "900"))  # 15 min

REFRESH_SQL_FUNCTION = os.getenv("REFRESH_SQL_FUNCTION")  # e.g. "select refresh_nextpoint_views();"

ALLOWED_EXTENSIONS = {"mp4", "mov", "m4v"}

# ----------------------------
# Flask
# ----------------------------
app = Flask(__name__)

# ----------------------------
# DB setup (SQLAlchemy Core)
# ----------------------------
engine = create_engine(POSTGRES_DSN, pool_pre_ping=True, future=True)
Session = sessionmaker(bind=engine)

meta = MetaData()

# Job tracking table (api_jobs)
api_jobs = Table(
    "api_jobs", meta,
    Column("job_id", String(36), primary_key=True),
    Column("status", String(32), nullable=False, index=True),  # queued, processing, failed, done
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("input_sha256", String(64), nullable=False, index=True),
    Column("original_filename", String(255), nullable=False),
    Column("s3_key", String(512), nullable=True),
    Column("error", Text, nullable=True),
    Column("sportai_session_uid", String(64), nullable=True)
)

# Bronze/raw table for SportAI JSON
sportai_raw = Table(
    "sportai_raw", meta,
    Column("id", String(36), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("session_uid", String(64), nullable=False, index=True),
    Column("s3_video_key", String(512), nullable=False),
    Column("sportai_request", JSONB, nullable=True),
    Column("sportai_result", JSONB, nullable=True),
)

def ensure_tables():
    with engine.begin() as conn:
        meta.create_all(conn)
        log.info("Ensured tables exist.")

ensure_tables()

# ----------------------------
# S3
# ----------------------------
s3 = boto3.client("s3", region_name=AWS_REGION)

def put_s3(file_path: str, key: str):
    s3.upload_file(file_path, S3_BUCKET, key)
    return f"s3://{S3_BUCKET}/{key}"

# ----------------------------
# Helpers: video quality, conversion, hashing
# ----------------------------
def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def run_ffprobe(path: str) -> dict:
    """Return basic stream metadata using ffprobe (must be installed)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        path
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr}")
    return json.loads(proc.stdout)

def parse_fps(avg_frame_rate: str) -> float:
    if not avg_frame_rate or avg_frame_rate == "0/0":
        return 0.0
    if "/" in avg_frame_rate:
        num, den = avg_frame_rate.split("/")
        den = float(den) if float(den) != 0 else 1.0
        return float(num) / den
    return float(avg_frame_rate)

def check_quality(path: str):
    meta = run_ffprobe(path)
    # Format
    duration = float(meta.get("format", {}).get("duration", 0.0))
    # Stream
    streams = meta.get("streams", [])
    if not streams:
        raise ValueError("No video stream found.")
    st0 = streams[0]
    width = int(st0.get("width", 0))
    height = int(st0.get("height", 0))
    fps = parse_fps(st0.get("avg_frame_rate", "0/1"))

    problems = []
    if duration < QUALITY_MIN_DURATION_SEC:
        problems.append(f"duration {duration:.2f}s < {QUALITY_MIN_DURATION_SEC}s")
    if width < QUALITY_MIN_WIDTH or height < QUALITY_MIN_HEIGHT:
        problems.append(f"resolution {width}x{height} < {QUALITY_MIN_WIDTH}x{QUALITY_MIN_HEIGHT}")
    if fps < QUALITY_MIN_FPS:
        problems.append(f"fps {fps:.1f} < {QUALITY_MIN_FPS}")

    ok = len(problems) == 0
    return ok, {
        "duration_sec": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "problems": problems
    }

def convert_to_mp4_if_needed(path: str, original_name: str) -> str:
    ext = os.path.splitext(original_name.lower())[1].lstrip(".")
    if ext in ("mp4",):
        return path  # already mp4
    # Convert with ffmpeg (H.264/AAC MP4)
    out_path = path + ".mp4"
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-movflags", "+faststart",
        out_path
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg convert failed: {proc.stderr}")
    return out_path

# ----------------------------
# SportAI client (polling model)
# ----------------------------
def sportai_headers():
    return {
        "Authorization": f"Bearer {SPORTAI_API_KEY}",
        "Content-Type": "application/json"
    }

def sportai_submit(video_url: str) -> dict:
    """Submit video for processing; returns JSON containing session_uid or job_id."""
    url = f"{SPORTAI_API_BASE}/submit"
    payload = {"video_url": video_url}
    r = requests.post(url, headers=sportai_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def sportai_get_result(session_uid: str) -> dict:
    """Poll results by session UID."""
    url = f"{SPORTAI_API_BASE}/result/{session_uid}"
    r = requests.get(url, headers=sportai_headers(), timeout=30)
    if r.status_code == 404:
        return {"status": "pending"}
    r.raise_for_status()
    return r.json()

# ----------------------------
# Background worker
# ----------------------------
job_queue: "queue.Queue[str]" = queue.Queue()

def set_job_status(db, job_id: str, status: str, **fields):
    now = datetime.now(timezone.utc)
    update = {
        "status": status,
        "updated_at": now,
        **fields
    }
    db.execute(
        api_jobs.update().where(api_jobs.c.job_id == job_id).values(**update)
    )

def process_job(job_id: str):
    """Long-running processing: quality -> convert -> s3 -> sportai -> poll -> store -> refresh."""
    log.info(f"[worker] Start job {job_id}")
    db = engine.begin()
    try:
        # Fetch job row
        row = db.execute(
            api_jobs.select().where(api_jobs.c.job_id == job_id)
        ).mappings().first()
        if not row:
            log.error(f"Job {job_id} not found.")
            return

        # The upload temp file path is based on a deterministic staging path (by input hash)
        input_hash = row["input_sha256"]
        original_name = row["original_filename"]
        staging_dir = os.path.join(tempfile.gettempdir(), "nextpoint_staging")
        os.makedirs(staging_dir, exist_ok=True)
        local_path = os.path.join(staging_dir, f"{input_hash}_{secure_filename(original_name)}")

        if not os.path.exists(local_path):
            # This can happen if tmp was cleaned; fail gracefully
            msg = "Staged file missing; please re-upload."
            log.error(msg)
            set_job_status(db, job_id, "failed", error=msg)
            return

        set_job_status(db, job_id, "processing")

        # 1) Quality check (pre-conversion for early fail)
        ok, qmeta = check_quality(local_path)
        if not ok:
            msg = f"Quality check failed: {', '.join(qmeta['problems'])}"
            set_job_status(db, job_id, "failed", error=msg)
            log.warning(f"[{job_id}] {msg}")
            return

        # 2) Convert to MP4 if needed
        mp4_path = convert_to_mp4_if_needed(local_path, original_name)

        # 3) S3 upload
        s3_key = f"{S3_PREFIX}/{input_hash}/{os.path.basename(mp4_path)}"
        put_s3(mp4_path, s3_key)
        set_job_status(db, job_id, "processing", s3_key=s3_key)

        # 4) Submit to SportAI
        s3_https = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
        submit_resp = sportai_submit(s3_https)
        session_uid = submit_resp.get("session_uid") or submit_resp.get("job_id") or str(uuid.uuid4())
        set_job_status(db, job_id, "processing", sportai_session_uid=session_uid)

        # 5) Poll for result
        start = time.time()
        last_payload = None
        while True:
            if time.time() - start > RESULT_POLL_TIMEOUT_SEC:
                raise TimeoutError("Timed out waiting for SportAI result.")

            payload = sportai_get_result(session_uid)
            last_payload = payload
            status = (payload.get("status") or "").lower()
            if status in ("done", "completed", "complete", "success"):
                break
            if status in ("failed", "error"):
                raise RuntimeError(f"SportAI failed: {payload}")

            time.sleep(RESULT_POLL_INTERVAL_SEC)

        # 6) Persist raw JSON to bronze table
        raw_id = str(uuid.uuid4())
        db.execute(
            sportai_raw.insert().values(
                id=raw_id,
                created_at=datetime.now(timezone.utc),
                session_uid=session_uid,
                s3_video_key=s3_key,
                sportai_request=submit_resp,
                sportai_result=last_payload
            )
        )

        # 7) Optionally refresh downstream objects (views/materializations)
        if REFRESH_SQL_FUNCTION:
            try:
                db.exec_driver_sql(REFRESH_SQL_FUNCTION)
                log.info("Ran refresh function.")
            except Exception as ex:
                # Non-fatal: we still mark job done, but include a soft warning in error column
                warn = f"Refresh function failed: {ex}"
                set_job_status(db, job_id, "done", error=warn)
                log.warning(f"[{job_id}] {warn}")
                return

        set_job_status(db, job_id, "done")
        log.info(f"[worker] Done job {job_id}")

    except Exception as e:
        log.exception(f"[worker] Job {job_id} failed:")
        set_job_status(db, job_id, "failed", error=str(e))
    finally:
        db.close()

def worker_loop():
    while True:
        job_id = job_queue.get()
        try:
            process_job(job_id)
        finally:
            job_queue.task_done()

# Start a single background worker thread
threading.Thread(target=worker_loop, daemon=True).start()

# ----------------------------
# API routes
# ----------------------------
@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "s3_bucket": S3_BUCKET,
        "region": AWS_REGION
    })

def allowed_file(fname: str) -> bool:
    return "." in fname and fname.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.post("/upload")
def upload():
    """
    Multipart form-data:
      - file: the video file (.mp4/.mov)
    Returns: { job_id }
    Frontend should poll /status/<job_id> until status is 'done' or 'failed'.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No selected file"}), 400

    if not allowed_file(f.filename):
        return jsonify({"error": f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    # Save to a deterministic temp path (by hash) so duplicate uploads are idempotent
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    # Compute hash
    h = file_sha256(tmp_path)
    safe_name = secure_filename(f.filename)

    staging_dir = os.path.join(tempfile.gettempdir(), "nextpoint_staging")
    os.makedirs(staging_dir, exist_ok=True)
    staged_path = os.path.join(staging_dir, f"{h}_{safe_name}")
    os.replace(tmp_path, staged_path)

    # Create job
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        db.execute(
            api_jobs.insert().values(
                job_id=job_id,
                status="queued",
                created_at=now,
                updated_at=now,
                input_sha256=h,
                original_filename=f.filename,
                s3_key=None,
                error=None,
                sportai_session_uid=None
            )
        )

    # Enqueue
    job_queue.put(job_id)

    return jsonify({"job_id": job_id})

@app.get("/status/<job_id>")
def status(job_id):
    with engine.begin() as db:
        row = db.execute(
            api_jobs.select().where(api_jobs.c.job_id == job_id)
        ).mappings().first()
        if not row:
            return jsonify({"error": "job not found"}), 404

        data = {
            "job_id": row["job_id"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "s3_key": row["s3_key"],
            "sportai_session_uid": row["sportai_session_uid"],
            "error": row["error"]
        }
        return jsonify(data)

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    log.info("Starting upload_app...")
    app.run(host="0.0.0.0", port=PORT)
