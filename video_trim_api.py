import os
import requests
import pandas as pd
from sqlalchemy import text

from db_init import engine
from build_video_timeline import build_video_timeline_from_silver, timeline_to_edl

VIDEO_WORKER_BASE_URL = os.getenv("VIDEO_WORKER_BASE_URL")  # new
VIDEO_WORKER_OPS_KEY = os.getenv("VIDEO_WORKER_OPS_KEY")    # same value as worker

if not VIDEO_WORKER_BASE_URL:
    raise RuntimeError("VIDEO_WORKER_BASE_URL env var is required")
if not VIDEO_WORKER_OPS_KEY:
    raise RuntimeError("VIDEO_WORKER_OPS_KEY env var is required")


def trigger_video_trim(task_id: str) -> dict:
    # 1) Load silver rows (I/O only)
    with engine.connect() as conn:
        df_silver = pd.read_sql(
            text("""
                SELECT task_id, point_number, ball_hit_s, exclude_d
                FROM silver.point_detail
                WHERE task_id = :task_id
                  AND ball_hit_s IS NOT NULL
                  AND point_number IS NOT NULL
            """),
            conn,
            params={"task_id": task_id},
        )

        # 2) Load s3 key/bucket from submission_context (I/O only)
        row = conn.execute(
            text("""
                SELECT data->>'s3_bucket' AS s3_bucket,
                       data->>'s3_key'    AS s3_key
                FROM bronze.submission_context
                WHERE task_id = :task_id
            """),
            {"task_id": task_id},
        ).mappings().first()

    if not row or not row.get("s3_key"):
        raise ValueError("submission_context missing s3_key (and/or s3_bucket) for task_id")

    s3_bucket = row.get("s3_bucket") or "nextpoint-prod-uploads"
    s3_key = row["s3_key"]

    # 3) Build timeline + EDL (business logic in Python)
    df_timeline = build_video_timeline_from_silver(df_silver, task_id=task_id)
    edl = timeline_to_edl(df_timeline)

    # 4) Call worker
    url = f"{VIDEO_WORKER_BASE_URL.rstrip('/')}/trim"
    headers = {"Authorization": f"Bearer {VIDEO_WORKER_OPS_KEY}"}

    resp = requests.post(
        url,
        json={
            "task_id": task_id,
            "s3_bucket": s3_bucket,
            "s3_key": s3_key,
            "edl": edl,
        },
        headers=headers,
        timeout=30,  # worker runs sync; API call just waits for ack
    )
    resp.raise_for_status()
    return resp.json()
