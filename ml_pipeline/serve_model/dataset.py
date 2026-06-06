"""Corpus → (X, y) dataset for the serve model.

Pulls per-task arrays from ml_analysis, serve labels from the corpus S3
JSONs, generates candidate anchors, and labels each anchor positive when
it sits within POS_TOL_S of a SportAI FAR-serve label.

Split discipline: BY VIDEO, not by task — the reference video appears as
two corpus tasks (a35b37f6 + 17e2da3a, two SA runs of the same file), so a
task-level split would leak the held-out video into training.
"""
from __future__ import annotations

import json
import logging
from bisect import bisect_left, bisect_right
from typing import Dict, List, Tuple

import numpy as np
from sqlalchemy import text

from ml_pipeline.serve_model.candidates import (
    Anchor, bounce_anchors, pose_anchors, merge_anchors,
)
from ml_pipeline.serve_model.features import featurize

logger = logging.getLogger(__name__)

POS_TOL_S = 1.25      # anchor within ±1.25s of a label = positive
S3_BUCKET = "nextpoint-prod-uploads"

# Reference video = the held-out eval video (BOTH its corpus tasks).
HELDOUT_TASKS = {"a35b37f6", "17e2da3a"}

# Duplicate-video tasks dropped from TRAINING (each is a second SA run of
# a video already in the train set — keeping both double-counts correlated
# labels and overfits the threshold sweep).
TRAIN_DEDUP_DROP = {"9378f2dd", "105290c3"}


def _task_fps(conn, tid: str) -> float:
    r = conn.execute(text("""
        SELECT total_frames, video_duration_sec
        FROM ml_analysis.video_analysis_jobs WHERE job_id::text = :t
    """), {"t": tid}).fetchone()
    return (r[0] / r[1]) if r and r[0] and r[1] else 25.0


def load_task_arrays(conn, tid: str) -> dict:
    """All per-task arrays the candidate generator + featurizer need."""
    fps = _task_fps(conn, tid)
    ball = conn.execute(text("""
        SELECT frame_idx, x, y, is_bounce, court_x, court_y
        FROM ml_analysis.ball_detections WHERE job_id::text = :t
        ORDER BY frame_idx"""), {"t": tid}).mappings().all()
    ball_rows = [dict(r) for r in ball]
    roi_raw = conn.execute(text("""
        SELECT frame_idx, keypoints, bbox_y1, bbox_y2
        FROM ml_analysis.player_detections_roi
        WHERE job_id::text = :t ORDER BY frame_idx"""), {"t": tid}).fetchall()
    roi_ts = sorted(r[0] / fps for r in roi_raw)
    roi_rows = []
    for fi, kp, y1, y2 in roi_raw:
        if isinstance(kp, str):
            kp = json.loads(kp)
        bbox_h = (y2 - y1) if (y1 is not None and y2 is not None) else None
        roi_rows.append({"ts": fi / fps, "kp": kp, "bbox_h": bbox_h})
    far_ts = sorted(r[0] / fps for r in conn.execute(text("""
        SELECT frame_idx FROM ml_analysis.player_detections
        WHERE job_id::text = :t AND player_id = 1"""), {"t": tid}).fetchall())
    near_ts = sorted(r[0] / fps for r in conn.execute(text("""
        SELECT frame_idx FROM ml_analysis.player_detections
        WHERE job_id::text = :t AND player_id = 0"""), {"t": tid}).fetchall())

    with_y = [(b["frame_idx"] / fps, b["y"]) for b in ball_rows if b.get("y") is not None]
    ball_t = np.array([t for t, _ in with_y], dtype=np.float64)
    ball_y = np.array([y for _, y in with_y], dtype=np.float64)
    bounce_ts = sorted(b["frame_idx"] / fps for b in ball_rows if b.get("is_bounce"))

    return dict(fps=fps, ball_rows=ball_rows, roi_ts=roi_ts, roi_rows=roi_rows,
                far_ts=far_ts, near_ts=near_ts, ball_t=ball_t, ball_y=ball_y,
                bounce_ts=bounce_ts)


def task_anchors(arrays: dict) -> List[Anchor]:
    return merge_anchors(
        bounce_anchors(arrays["ball_rows"], arrays["fps"]),
        pose_anchors(arrays["roi_ts"]),
    )


def _load_labels(s3, key: str) -> List[dict]:
    key = key.replace(f"s3://{S3_BUCKET}/", "")
    body = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
    labels = json.loads(body)
    if isinstance(labels, dict):
        labels = labels.get("labels") or labels.get("serves") or []
    return labels


def build_dataset(engine) -> Dict[str, dict]:
    """Per-task {X, y, anchors, far_label_ts} for every corpus serve task."""
    import boto3
    s3 = boto3.client("s3", region_name="eu-north-1")

    out: Dict[str, dict] = {}
    with engine.connect() as conn:
        corpus = conn.execute(text("""
            SELECT t5_task_id::text, label_s3_key FROM ml_analysis.training_corpus
            WHERE label_kind = 'serve' ORDER BY created_at
        """)).fetchall()

        for tid, key in corpus:
            labels = _load_labels(s3, key)
            far_ts = sorted(float(l["hit_ts"]) for l in labels if l.get("role") == "FAR")
            arrays = load_task_arrays(conn, tid)
            anchors = task_anchors(arrays)

            X = np.stack([
                featurize(a, arrays["bounce_ts"], arrays["roi_ts"], arrays["far_ts"],
                          arrays["near_ts"], arrays["ball_t"], arrays["ball_y"],
                          roi_rows=arrays["roi_rows"])
                for a in anchors
            ]) if anchors else np.zeros((0, 26), dtype=np.float32)

            y = np.zeros(len(anchors), dtype=np.float32)
            for i, a in enumerate(anchors):
                j = bisect_left(far_ts, a.ts - POS_TOL_S)
                if j < len(far_ts) and far_ts[j] <= a.ts + POS_TOL_S:
                    y[i] = 1.0

            short = tid[:8]
            out[short] = dict(task_id=tid, X=X, y=y,
                              anchor_ts=[a.ts for a in anchors],
                              far_label_ts=far_ts,
                              heldout=short in HELDOUT_TASKS)
            logger.info("dataset %s: anchors=%d positives=%d far_labels=%d heldout=%s",
                        short, len(anchors), int(y.sum()), len(far_ts),
                        short in HELDOUT_TASKS)
    return out


def split(dataset: Dict[str, dict], dedup: bool = False
          ) -> Tuple[np.ndarray, np.ndarray, Dict[str, dict]]:
    """(X_train, y_train, heldout_tasks). Held-out = the reference video.

    dedup=True drops the duplicate-video tasks from training. Measured
    2026-06-06: with only ~4 distinct train videos, dedup halves the
    positives (214→103) and costs more heldout F1 than the correlation
    saves — keep duplicates until the corpus has real diversity.
    """
    Xs, ys = [], []
    heldout = {}
    for short, d in dataset.items():
        if d["heldout"]:
            heldout[short] = d
        elif dedup and short in TRAIN_DEDUP_DROP:
            logger.info("split: dropping duplicate-video task %s from training", short)
        elif len(d["X"]):
            Xs.append(d["X"]); ys.append(d["y"])
    return np.concatenate(Xs), np.concatenate(ys), heldout
