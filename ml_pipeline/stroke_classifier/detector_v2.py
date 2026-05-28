"""ADR-02 v2 swing-type detector — runs at ingest time after stroke_detector.

Pipeline per ADR-02 §"Q4 inference placement = A: Render-side":
  1. Query ml_analysis.stroke_events for this task -> list of (hit_frame, player_id)
  2. For each hit:
       a. Find role-matching player bbox in ml_analysis.player_detections (with
          ±5-frame fallback for far-coverage gap; same logic as the dataset builder).
       b. Download trimmed/<t5>/practice.mp4 from S3 if not cached locally.
       c. Read the 16-frame window around hit_frame; crop to bbox*1.5 square;
          resize to 112x112; compute Farneback optical flow.
  3. Batch-run model.predict_batch(flows, handedness)
  4. delete + reinsert ml_analysis.swing_type_events rows for this task

STOPGAP semantics:
  - If MODEL_WEIGHTS_V2 doesn't exist, the classifier returns no predictions,
    AND we skip the (expensive) video download entirely. Pure no-op.
  - Schema init still runs on boot so the table exists when weights ship.
  - Wired into upload_app.py::_do_ingest_t5 as a try/except so any error
    here cannot break the ingest flow.

Future split-out (only if inference budget bites):
  Decouple from the ingest critical path by moving the body into a periodic
  Render Cron job that scans for stroke_event-bearing tasks lacking
  swing_type_events. The detect_swing_types_for_task() signature stays the
  same; only the call site changes.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import torch
from sqlalchemy import text as sql_text

from ml_pipeline.stroke_classifier.db import (
    delete_swing_types_for_task,
    init_swing_type_schema,
)
from ml_pipeline.stroke_classifier.model_v2 import (
    CLASSES, MODEL_WEIGHTS_V2, SwingTypeClassifierV2,
)
from ml_pipeline.training.build_swing_type_dataset import (
    BBOX_FALLBACK_RADIUS,
    HALF_Y_METRES,
    ROI_SIZE,
    S3_BUCKET,
    WINDOW_PRE,
    WINDOW_TOTAL,
    _bbox_to_roi,
    _compute_flow_window,
    _fetch_bboxes_for_frames,
    _pick_player_with_fallback,
    _read_window_frames,
)

logger = logging.getLogger(__name__)

INFERENCE_BATCH_SIZE = 8


def _resolve_video_s3_key(engine, t5_task_id: str) -> Optional[str]:
    """Look up the original wix-uploads/<file> s3_key for this task.

    At _do_ingest_t5 time the ORIGINAL 1080p video is still present in S3
    (deleted only after trim completes). The trimmed 720p copy
    (`trimmed/<task>/practice.mp4`) doesn't exist yet at this point in the
    flow. Use the original so bbox coords from ml_analysis.player_detections
    (which are 1080p-native) apply directly without rescaling.
    """
    with engine.connect() as conn:
        row = conn.execute(sql_text("""
            SELECT s3_key FROM bronze.submission_context
             WHERE task_id = :tid AND deleted_at IS NULL
             LIMIT 1
        """), {"tid": t5_task_id}).mappings().first()
    return row["s3_key"] if row else None


def _ensure_video_local(s3_client, t5_task_id: str,
                        s3_key: str, cache_root: Path) -> Optional[Path]:
    """Download s3://<S3_BUCKET>/<s3_key> to cache_root if not already there."""
    cache_root.mkdir(parents=True, exist_ok=True)
    local_path = cache_root / f"{t5_task_id}_{Path(s3_key).name}"
    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path
    try:
        s3_client.download_file(S3_BUCKET, s3_key, str(local_path))
        return local_path
    except Exception as e:
        logger.warning("swing_type detect: video not in S3 (s3://%s/%s) — %s",
                       S3_BUCKET, s3_key, e)
        return None


def _infer_handedness_for_match(engine, t5_task_id: str) -> dict[int, str]:
    """Per-player handedness inference. STOPGAP v0 default: everyone right-handed.

    Future revision: query ml_analysis.stroke_events + bronze.player_swing
    forehand events for each player, look at court-x preference, decide left vs
    right. For STOPGAP (no inference happens anyway) this returns nothing.
    """
    return {}


def detect_swing_types_for_task(task_id: str, engine=None, s3_client=None) -> dict:
    """Entry point invoked from upload_app.py::_do_ingest_t5 after stroke_detector.

    Returns a status dict {status, n_hits, n_written, reason?}. Never raises;
    upload_app wraps in try/except as belt-and-braces, but this function is
    written so it cannot leak exceptions to the ingest flow.
    """
    try:
        clf = SwingTypeClassifierV2()
        if not clf.available:
            return {"status": "stopgap",
                    "reason": f"no weights at {MODEL_WEIGHTS_V2}",
                    "n_hits": 0, "n_written": 0}

        if engine is None:
            from db_init import engine as default_engine
            engine = default_engine

        # 1. Initialise schema (idempotent) + clear prior predictions for this task
        with engine.begin() as conn:
            init_swing_type_schema(conn)
            delete_swing_types_for_task(conn, task_id)

        # 2. Pull stroke events for this task
        with engine.connect() as conn:
            stroke_rows = conn.execute(sql_text("""
                SELECT hit_frame, hit_ts, player_id
                  FROM ml_analysis.stroke_events
                 WHERE job_id::text = :tid
                 ORDER BY hit_frame
            """), {"tid": task_id}).mappings().all()
        if not stroke_rows:
            return {"status": "no_strokes", "n_hits": 0, "n_written": 0}

        # 3. Pre-fetch bboxes (with fallback radius) in one query
        needed = sorted({int(r["hit_frame"]) for r in stroke_rows})
        bbox_by_frame = _fetch_bboxes_for_frames(
            engine, task_id, needed, fallback_radius=BBOX_FALLBACK_RADIUS,
        )

        # 4. Resolve + download the ORIGINAL 1080p video (still present at
        #    ingest time; trimmed copy doesn't exist yet at this point in flow)
        video_s3_key = _resolve_video_s3_key(engine, task_id)
        if not video_s3_key:
            return {"status": "no_video_key", "n_hits": len(stroke_rows), "n_written": 0}
        if s3_client is None:
            import boto3
            s3_client = boto3.client("s3")
        cache_root = Path(tempfile.gettempdir()) / "t5_swing_classifier_cache"
        video_path = _ensure_video_local(s3_client, task_id, video_s3_key, cache_root)
        if video_path is None:
            return {"status": "no_video", "n_hits": len(stroke_rows), "n_written": 0}

        # Probe video dims once (cheap)
        import cv2
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return {"status": "video_open_failed", "n_hits": len(stroke_rows), "n_written": 0}
        n_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        video_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        video_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        # 5. Per-stroke extraction + inference (batched at INFERENCE_BATCH_SIZE)
        handedness_map = _infer_handedness_for_match(engine, task_id)
        pending_flows: list[np.ndarray] = []
        pending_hands: list[float] = []
        pending_meta: list[dict] = []
        n_skipped = 0
        n_written = 0

        def _flush():
            nonlocal n_written
            if not pending_flows:
                return
            flow_t = torch.from_numpy(np.stack(pending_flows, axis=0))
            # (B, T, H, W, C) -> (B, C, T, H, W)
            flow_t = flow_t.permute(0, 4, 1, 2, 3).contiguous()
            hand_t = torch.tensor([[h] for h in pending_hands], dtype=torch.float32)
            preds = clf.predict_batch(flow_t, hand_t)
            with engine.begin() as conn:
                for (cls_name, conf), meta in zip(preds, pending_meta):
                    conn.execute(sql_text("""
                        INSERT INTO ml_analysis.swing_type_events
                            (job_id, hit_frame, hit_ts, player_id, role,
                             swing_type, confidence, handedness, source)
                        VALUES
                            (:job, :hf, :hts, :pid, :role,
                             :st, :conf, :hand, 'swing_classifier_v2')
                    """), {
                        "job": task_id, "hf": meta["hit_frame"], "hts": meta["hit_ts"],
                        "pid": meta["player_id"], "role": meta["role"],
                        "st": cls_name, "conf": conf,
                        "hand": "right" if meta["handedness_bit"] > 0.5 else "left",
                    })
                    n_written += 1
            pending_flows.clear()
            pending_hands.clear()
            pending_meta.clear()

        for r in stroke_rows:
            hit_frame = int(r["hit_frame"])
            hit_ts = float(r["hit_ts"]) if r["hit_ts"] is not None else None
            sa_player_id = r["player_id"]

            # Need court_x/y at this frame to do role classification. The
            # stroke_event already encodes hitter via player_id but the role
            # depends on court coords. Query the player_detections row directly.
            with engine.connect() as conn:
                pos = conn.execute(sql_text("""
                    SELECT court_y FROM ml_analysis.player_detections
                     WHERE job_id = :tid AND frame_idx = :f AND player_id = :pid
                     LIMIT 1
                """), {"tid": task_id, "f": hit_frame, "pid": sa_player_id}).mappings().first()
            if not pos or pos["court_y"] is None:
                n_skipped += 1
                continue
            cy = float(pos["court_y"])
            role = "NEAR" if cy > HALF_Y_METRES else "FAR"

            player, frame_delta = _pick_player_with_fallback(
                bbox_by_frame, hit_frame, role,
                label_court_x=0.0, label_court_y=cy,
                fallback_radius=BBOX_FALLBACK_RADIUS,
            )
            if player is None:
                n_skipped += 1
                continue

            # Using the ORIGINAL 1080p video so bboxes apply 1:1 (no rescale)
            x1 = float(player["bbox_x1"])
            y1 = float(player["bbox_y1"])
            x2 = float(player["bbox_x2"])
            y2 = float(player["bbox_y2"])
            roi_x, roi_y, roi_w, roi_h = _bbox_to_roi(x1, y1, x2, y2, video_w, video_h)
            if roi_w < 4 or roi_h < 4:
                n_skipped += 1
                continue

            start_frame = hit_frame - WINDOW_PRE
            if start_frame < 0 or start_frame + WINDOW_TOTAL > n_video_frames:
                n_skipped += 1
                continue
            frames = _read_window_frames(video_path, start_frame, WINDOW_TOTAL)
            if len(frames) < WINDOW_TOTAL:
                n_skipped += 1
                continue

            crops = []
            for fr in frames:
                crop = fr[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
                if crop.size == 0:
                    crop = np.zeros((roi_h, roi_w, 3), dtype=np.uint8)
                crops.append(cv2.resize(crop, (ROI_SIZE, ROI_SIZE), interpolation=cv2.INTER_AREA))
            flow = _compute_flow_window(crops)

            hand_str = handedness_map.get(int(sa_player_id) if sa_player_id is not None else -1, "right")
            hand_bit = 1.0 if hand_str == "right" else 0.0

            pending_flows.append(flow)
            pending_hands.append(hand_bit)
            pending_meta.append({
                "hit_frame": hit_frame, "hit_ts": hit_ts,
                "player_id": int(player["player_id"]),
                "role": role, "handedness_bit": hand_bit,
            })

            if len(pending_flows) >= INFERENCE_BATCH_SIZE:
                _flush()

        _flush()

        logger.info(
            "swing_type detect t5=%s — strokes=%d written=%d skipped=%d",
            task_id, len(stroke_rows), n_written, n_skipped,
        )
        return {"status": "ok", "n_hits": len(stroke_rows),
                "n_written": n_written, "n_skipped": n_skipped}

    except Exception as e:
        logger.exception("swing_type detect t5=%s — fatal error swallowed: %s", task_id, e)
        return {"status": "error", "reason": f"{e.__class__.__name__}: {e}",
                "n_hits": 0, "n_written": 0}
