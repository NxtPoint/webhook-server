"""Build the ADR-02 swing-type training dataset from corpus JSONs.

Universal preparation step between the swing-type corpus (label_kind=
'stroke_classifier' rows in ml_analysis.training_corpus, produced by
label_swing_types.py) and any future classifier model. Architecture-
agnostic on purpose: outputs raw optical-flow tensors that can be
reshaped into either:
  - 3D-CNN input shape (B, 2, T=16, H=112, W=112) — R(2+1)D-18, S3D, etc.
  - Early-fusion 2D-CNN shape (B, 32, H=112, W=112) — 16 frames * 2 channels

What this does per corpus row (one per dual-submit match):

  1. Pull (sa_task_id, t5_task_id, label_s3_key, video_s3_key) from
     ml_analysis.training_corpus WHERE label_kind='stroke_classifier'.
  2. Download the label JSON from S3 (small, KB-scale).
  3. Download the 720p trimmed video from S3 (10s-100s MB; cached locally
     under --cache-dir so re-runs are free).
  4. For each labeled hit:
       a. Find the T5 player at the hit_frame whose role matches the label
          (court_y > HALF_Y -> NEAR; court_y < HALF_Y -> FAR). 100% exact-
          frame coverage was empirically measured on Match 1; if a frame
          ever lacks a matching detection, the hit is logged + dropped.
       b. Pull the player bbox; rescale 1080->720 (the pipeline ran at
          1080 but the surviving video is the 720p trimmed copy — see
          memory `reference_t5_video_retention`).
       c. Centre-pad the bbox to a square, expand by ROI_SCALE (=1.5
          per ADR-02), clip to frame bounds.
       d. Seek the video to (hit_frame - WINDOW_PRE), read WINDOW_TOTAL
          consecutive frames (16 = 10 pre + 6 post per ADR-02).
       e. Crop each frame to the ROI, resize to (112, 112).
       f. Compute dense Farneback optical flow between consecutive frames
          (15 flow fields for 16 input frames). Pad the first frame's
          flow with zeros so output has fixed 16-frame temporal axis.
  5. Aggregate per-match into a single .pt file containing:
       flows: torch.float32 tensor of shape (N_hits, 16, 112, 112, 2)
       labels: dict of parallel arrays (swing_type, swing_type_raw,
               role, is_serve, player_id, hit_frame, hit_ts, court_x,
               court_y, confidence) -- everything from the corpus JSON
               for downstream stratification + filtering.
       meta: source pair, source label_s3_key, builder version, settings
  6. Write a top-level manifest.json: list of per-match outputs + per-
     class + per-role totals + train/val splits keyed by match (so we
     never leak the same match across the split).

Why Farneback over TV-L1/RAFT (ADR-02 lists those):
  - cv2.calcOpticalFlowFarneback ships with stock OpenCV; TV-L1 needs
    opencv-contrib (not in our requirements). RAFT needs a model weight
    file + GPU. Farneback is the same algorithm the pre-ADR-02
    stroke_classifier/flow_extractor.py uses, so the data shape is
    consistent with the existing scaffold. If a future training run
    measures Farneback as the accuracy bottleneck, swap is one function.

CLI:
  # All corpus rows (default):
  python -m ml_pipeline.training.build_swing_type_dataset \\
      --output-dir ml_pipeline/training/datasets/swing_type_v1

  # One specific match (smoke test):
  python -m ml_pipeline.training.build_swing_type_dataset \\
      --t5 78c32f53-5580-4a88-a4e7-7506e59b2b52 \\
      --output-dir ml_pipeline/training/datasets/swing_type_v1_match1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np
import torch

from sqlalchemy import text as sql_text

from ml_pipeline.config import FRAME_SAMPLE_FPS

logger = logging.getLogger("build_swing_type_dataset")


def _bbox_lookup_frame(label: dict) -> int:
    """T5 25fps frame index for the player_detections bbox lookup.

    FRAME-SPACE FIX (2026-06-04): SportAI's hit_frame is stored verbatim in the
    SOURCE video's fps (25/30/60, per match) — but ml_analysis.player_detections
    is indexed in the fixed FRAME_SAMPLE_FPS (25fps) sampled space. Matching the
    bbox by the raw SA hit_frame silently misaligns by (source_fps/25 - 1) and
    dropped ~62% of hits (bimodal by fps: ~28% on 25fps matches, 77-81% on 30/60fps).
    Convert via the fps-independent hit_ts (seconds). NOTE: the VIDEO seek for the
    optical-flow crop stays on the raw SA hit_frame — the trimmed video is SOURCE-fps,
    so the SA frame is already correct there. See feedback_t5_two_frame_spaces.
    """
    ts = label.get("hit_ts")
    if ts is not None:
        return int(round(float(ts) * FRAME_SAMPLE_FPS))
    return int(label["hit_frame"])  # fallback: assume already 25fps-aligned

# ADR-02 spec constants (Build spec v1 table)
WINDOW_PRE = 10        # frames before predicted_hit_frame
WINDOW_POST = 6        # frames after; spec asks for 6 → total 16 incl. centre? See note.
WINDOW_TOTAL = 16      # NOTE: spec is "10 before -> 6 after" centred on hit; 10 + 1 + 5 = 16
                       # (centre frame counts in pre). We treat hit_frame as frame index 10
                       # within the [0..15] window.
ROI_SCALE = 1.5
ROI_SIZE = 112

# The bronze pipeline ran at 1080p; the surviving video is the trimmed 720p copy.
# See memory `reference_t5_video_retention`. Bboxes are 1080p-native -> scale to 720p.
PIPELINE_RES_HEIGHT = 1080
VIDEO_RES_HEIGHT = 720
BBOX_SCALE = VIDEO_RES_HEIGHT / PIPELINE_RES_HEIGHT  # = 0.6667

HALF_Y_METRES = 11.885  # net midline, matches serve_detector/bounce_validity.py
BBOX_FALLBACK_RADIUS = 5  # frames; search +/-N if no role-matching det at hit_frame

S3_BUCKET = "nextpoint-prod-uploads"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _get_engine():
    from sqlalchemy import create_engine
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
           or os.environ.get("DB_URL"))
    if not url:
        raise RuntimeError("DATABASE_URL required")
    return create_engine(_normalize_db_url(url))


def _fetch_corpus_rows(engine, t5_filter: Optional[str]) -> list[dict]:
    """Return stroke_classifier corpus rows, optionally filtered to one t5_task_id."""
    where = "tc.label_kind = 'stroke_classifier'"
    params: dict = {}
    if t5_filter:
        where += " AND tc.t5_task_id = :t5"
        params["t5"] = t5_filter
    with engine.connect() as conn:
        rows = conn.execute(sql_text(f"""
            SELECT id, sa_task_id, t5_task_id, label_s3_key, video_s3_key,
                   label_count, role_breakdown, created_at
              FROM ml_analysis.training_corpus tc
             WHERE {where}
             ORDER BY created_at
        """), params).mappings().all()
    return [dict(r) for r in rows]


def _fetch_bboxes_for_frames(engine, t5_task_id: str,
                             frames: list[int],
                             fallback_radius: int = 0) -> dict[int, list[dict]]:
    """Return {frame_idx: [{player_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
    court_x, court_y}, ...]} for each frame in `frames` ± fallback_radius.

    Bboxes are 1080p-native (caller pre-scales to 720p when applying to
    the trimmed video). fallback_radius widens the fetched frame set so a
    per-hit fallback picker can search neighbouring frames when the exact
    hit_frame lacks a role-matching detection (the far-player coverage gap
    is well documented in north_star line 56; recovers ~9-14 of 37 missing
    FAR labels on Match 1 at radius=5).
    """
    if not frames:
        return {}
    if fallback_radius > 0:
        expanded = set()
        for f in frames:
            for d in range(-fallback_radius, fallback_radius + 1):
                expanded.add(int(f) + d)
        frames_to_fetch = sorted(expanded)
    else:
        frames_to_fetch = list(frames)
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT frame_idx, player_id,
                   bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                   court_x, court_y
              FROM ml_analysis.player_detections
             WHERE job_id = :tid
               AND frame_idx = ANY(:frames)
        """), {"tid": t5_task_id, "frames": frames_to_fetch}).mappings().all()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(int(r["frame_idx"]), []).append(dict(r))
    return out


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _s3_uri_to_key(uri: str) -> str:
    if uri.startswith(f"s3://{S3_BUCKET}/"):
        return uri[len(f"s3://{S3_BUCKET}/"):]
    if uri.startswith("s3://"):
        # different bucket; not handled here
        raise RuntimeError(f"unexpected S3 URI bucket: {uri}")
    return uri


def _download_to_cache(s3_client, s3_key: str, cache_dir: Path) -> Path:
    """Download S3 object to local cache; return local path. Skip if cached."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_name = s3_key.replace("/", "__")
    local_path = cache_dir / local_name
    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path
    logger.info("downloading s3://%s/%s -> %s", S3_BUCKET, s3_key, local_path)
    s3_client.download_file(S3_BUCKET, s3_key, str(local_path))
    return local_path


# ---------------------------------------------------------------------------
# Bbox / ROI math
# ---------------------------------------------------------------------------

def _pick_player_for_label(detections_at_frame: list[dict],
                           label_role: str,
                           label_court_x: float,
                           label_court_y: float) -> Optional[dict]:
    """Pick the T5 player whose role matches the SA label at one frame.

    Returns None if no detection at the frame OR no detection on the
    label's court half.
    """
    if not detections_at_frame:
        return None
    if label_role == "NEAR":
        candidates = [d for d in detections_at_frame
                      if d["court_y"] is not None and d["court_y"] > HALF_Y_METRES]
    else:
        candidates = [d for d in detections_at_frame
                      if d["court_y"] is not None and d["court_y"] <= HALF_Y_METRES]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Tie-break by court-coord proximity to the labeled hit location
    def dist(d):
        cx = d["court_x"] if d["court_x"] is not None else 0.0
        cy = d["court_y"] if d["court_y"] is not None else 0.0
        return (cx - label_court_x) ** 2 + (cy - label_court_y) ** 2
    return min(candidates, key=dist)


def _pick_player_with_fallback(
    bbox_by_frame: dict[int, list[dict]],
    hit_frame: int,
    label_role: str,
    label_court_x: float,
    label_court_y: float,
    fallback_radius: int,
) -> tuple[Optional[dict], int]:
    """Search the exact hit_frame first, then expanding ±N frames in priority
    order, for a role-matching player detection.

    Returns (player_row, frame_delta) where frame_delta is signed distance
    from the hit_frame (0 if exact). frame_delta is preserved in the
    output metadata so downstream training code can weight or filter by
    bbox staleness.
    """
    # Frame 0 first, then ±1, ±2, ... in alternating order
    candidates_order: list[int] = [0]
    for r in range(1, fallback_radius + 1):
        candidates_order.append(-r)
        candidates_order.append(r)
    for delta in candidates_order:
        f = hit_frame + delta
        dets = bbox_by_frame.get(f, [])
        picked = _pick_player_for_label(dets, label_role, label_court_x, label_court_y)
        if picked is not None:
            return picked, delta
    return None, 0


def _bbox_to_roi(bbox_x1: float, bbox_y1: float, bbox_x2: float, bbox_y2: float,
                 frame_w: int, frame_h: int,
                 scale: float = ROI_SCALE) -> tuple[int, int, int, int]:
    """Convert (x1,y1,x2,y2) bbox into a square ROI expanded by `scale`,
    clipped to frame bounds. Returns (x, y, w, h) suitable for cropping.

    Bbox is expected in the video's coord system (caller pre-scales 1080->720).
    """
    cx = (bbox_x1 + bbox_x2) / 2.0
    cy = (bbox_y1 + bbox_y2) / 2.0
    half = max(bbox_x2 - bbox_x1, bbox_y2 - bbox_y1) * scale / 2.0
    x = int(round(cx - half))
    y = int(round(cy - half))
    side = int(round(half * 2))
    # Clip to frame; if ROI goes off the edge, shift inward (preserves shape)
    if x < 0:
        x = 0
    if y < 0:
        y = 0
    if x + side > frame_w:
        x = max(0, frame_w - side)
    if y + side > frame_h:
        y = max(0, frame_h - side)
    side = min(side, frame_w - x, frame_h - y)
    return x, y, side, side


# ---------------------------------------------------------------------------
# Video / flow
# ---------------------------------------------------------------------------

def _read_window_frames(video_path: Path, start_frame: int,
                        n_frames: int) -> list[np.ndarray]:
    """Read `n_frames` consecutive BGR frames starting at start_frame.
    Returns a list of length n_frames (may be shorter if EOF)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        frames = []
        for _ in range(n_frames):
            ok, fr = cap.read()
            if not ok:
                break
            frames.append(fr)
        return frames
    finally:
        cap.release()


def _compute_flow_window(crops: list[np.ndarray]) -> np.ndarray:
    """Compute Farneback dense optical flow over a sequence of grayscale
    crops. Returns float32 array of shape (T, H, W, 2) where T == len(crops).
    The first frame's flow is zero-padded (no previous frame).
    """
    T = len(crops)
    if T == 0:
        return np.zeros((0, ROI_SIZE, ROI_SIZE, 2), dtype=np.float32)
    H, W = crops[0].shape[:2]
    flows = np.zeros((T, H, W, 2), dtype=np.float32)
    prev_gray = cv2.cvtColor(crops[0], cv2.COLOR_BGR2GRAY)
    for t in range(1, T):
        curr_gray = cv2.cvtColor(crops[t], cv2.COLOR_BGR2GRAY)
        flow = cv2.calcOpticalFlowFarneback(
            prev_gray, curr_gray, None,
            pyr_scale=0.5, levels=3, winsize=15,
            iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
        )
        flows[t] = flow.astype(np.float32)
        prev_gray = curr_gray
    return flows


# ---------------------------------------------------------------------------
# Main per-match builder
# ---------------------------------------------------------------------------

def build_one_match(
    corpus_row: dict,
    output_dir: Path,
    cache_dir: Path,
    engine,
    s3_client,
) -> dict:
    """Run the full builder pipeline for one corpus row. Returns stats."""
    t5_task_id = corpus_row["t5_task_id"]
    sa_task_id = corpus_row["sa_task_id"]
    label_s3_key = _s3_uri_to_key(corpus_row["label_s3_key"])
    video_s3_key = _s3_uri_to_key(corpus_row["video_s3_key"])

    # 1. Download label JSON
    label_local = _download_to_cache(s3_client, label_s3_key, cache_dir / "labels")
    labels_obj = json.loads(label_local.read_text())
    labels: list[dict] = labels_obj["labels"]
    logger.info("[%s] %d labels loaded", t5_task_id[:8], len(labels))

    # 2. Download trimmed 720p video (or use the cached one)
    trimmed_key = f"trimmed/{t5_task_id}/practice.mp4"
    video_local = _download_to_cache(s3_client, trimmed_key, cache_dir / "videos")

    # Probe video to get exact frame dims
    cap = cv2.VideoCapture(str(video_local))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open trimmed video {video_local}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    n_frames_in_video = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    video_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    video_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    logger.info("[%s] video %dx%d @ %.2f fps, %d frames",
                t5_task_id[:8], video_w, video_h, fps, n_frames_in_video)

    # 3. Pre-fetch all needed bboxes (incl. fallback radius) in one query
    # 25fps bbox-lookup frames (NOT the raw SA hit_frame — see _bbox_lookup_frame)
    needed_frames = sorted({_bbox_lookup_frame(l) for l in labels
                            if l.get("hit_frame") is not None})
    bbox_by_frame = _fetch_bboxes_for_frames(
        engine, t5_task_id, needed_frames,
        fallback_radius=BBOX_FALLBACK_RADIUS,
    )

    # 4. Per-hit extraction
    out_flows: list[np.ndarray] = []
    meta_lists: dict[str, list] = {
        "swing_type": [], "swing_type_raw": [], "role": [],
        "is_serve": [], "player_id_sa": [], "hit_frame": [], "hit_ts": [],
        "court_x": [], "court_y": [], "confidence": [],
        "t5_player_id": [], "bbox_xywh_720": [], "bbox_frame_delta": [],
    }
    dropped = {"no_role_match_within_radius": 0, "video_eof": 0, "bad_bbox": 0}

    for li, lbl in enumerate(labels):
        hit_frame = int(lbl["hit_frame"])        # SOURCE-fps index → video seek (below)
        t5_frame = _bbox_lookup_frame(lbl)       # 25fps index → bbox lookup
        role = lbl["role"]
        cx = float(lbl["court_x"]); cy = float(lbl["court_y"])

        player, frame_delta = _pick_player_with_fallback(
            bbox_by_frame, t5_frame, role, cx, cy,
            fallback_radius=BBOX_FALLBACK_RADIUS,
        )
        if player is None:
            dropped["no_role_match_within_radius"] += 1
            continue

        # Scale 1080p bbox -> 720p video coords
        x1 = player["bbox_x1"] * BBOX_SCALE
        y1 = player["bbox_y1"] * BBOX_SCALE
        x2 = player["bbox_x2"] * BBOX_SCALE
        y2 = player["bbox_y2"] * BBOX_SCALE
        if x2 - x1 < 4 or y2 - y1 < 4:
            dropped["bad_bbox"] += 1
            continue
        roi_x, roi_y, roi_w, roi_h = _bbox_to_roi(x1, y1, x2, y2, video_w, video_h)
        if roi_w < 4 or roi_h < 4:
            dropped["bad_bbox"] += 1
            continue

        # Read the 16-frame window
        start_frame = hit_frame - WINDOW_PRE
        if start_frame < 0 or start_frame + WINDOW_TOTAL > n_frames_in_video:
            dropped["video_eof"] += 1
            continue
        frames = _read_window_frames(video_local, start_frame, WINDOW_TOTAL)
        if len(frames) < WINDOW_TOTAL:
            dropped["video_eof"] += 1
            continue

        # Crop + resize per frame
        crops = []
        for fr in frames:
            crop = fr[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
            if crop.size == 0:
                crop = np.zeros((roi_h, roi_w, 3), dtype=np.uint8)
            crop = cv2.resize(crop, (ROI_SIZE, ROI_SIZE), interpolation=cv2.INTER_AREA)
            crops.append(crop)

        flow = _compute_flow_window(crops)  # (16, 112, 112, 2)
        out_flows.append(flow)

        meta_lists["swing_type"].append(lbl["swing_type"])
        meta_lists["swing_type_raw"].append(lbl["swing_type_raw"])
        meta_lists["role"].append(role)
        meta_lists["is_serve"].append(bool(lbl.get("is_serve", False)))
        meta_lists["player_id_sa"].append(lbl.get("player_id"))
        meta_lists["hit_frame"].append(hit_frame)
        meta_lists["hit_ts"].append(lbl.get("hit_ts"))
        meta_lists["court_x"].append(cx)
        meta_lists["court_y"].append(cy)
        meta_lists["confidence"].append(lbl.get("confidence"))
        meta_lists["t5_player_id"].append(int(player["player_id"]))
        meta_lists["bbox_xywh_720"].append([roi_x, roi_y, roi_w, roi_h])
        meta_lists["bbox_frame_delta"].append(int(frame_delta))

        if (li + 1) % 25 == 0:
            logger.info("[%s] processed %d/%d labels", t5_task_id[:8], li + 1, len(labels))

    if not out_flows:
        raise RuntimeError(f"no usable hits for {t5_task_id} (all dropped: {dropped})")

    flows_tensor = torch.from_numpy(np.stack(out_flows, axis=0))  # (N, 16, 112, 112, 2)

    output_dir.mkdir(parents=True, exist_ok=True)
    pt_path = output_dir / f"{t5_task_id}.pt"
    torch.save({
        "flows": flows_tensor,
        "labels": meta_lists,
        "meta": {
            "t5_task_id": t5_task_id,
            "sa_task_id": sa_task_id,
            "source_label_s3_key": label_s3_key,
            "source_video_s3_key": trimmed_key,
            "window_pre": WINDOW_PRE,
            "window_total": WINDOW_TOTAL,
            "roi_scale": ROI_SCALE,
            "roi_size": ROI_SIZE,
            "bbox_scale_1080_to_720": BBOX_SCALE,
            "bbox_fallback_radius": BBOX_FALLBACK_RADIUS,
            "flow_method": "cv2.calcOpticalFlowFarneback",
            "builder_version": "v1-2026-05-28",
        },
    }, pt_path)

    # Class + role tallies for the manifest
    by_class = {c: meta_lists["swing_type"].count(c)
                for c in ("forehand", "backhand", "overhead")}
    by_role = {"NEAR": meta_lists["role"].count("NEAR"),
               "FAR": meta_lists["role"].count("FAR")}
    return {
        "t5_task_id": t5_task_id,
        "sa_task_id": sa_task_id,
        "pt_path": pt_path.name,  # basename only — loader resolves as dataset_dir/name.
                                  # (str(pt_path) embedded the Windows full path w/ backslashes,
                                  #  which the Linux loader's Path().name couldn't split → broke
                                  #  cross-platform training on the GPU box. 2026-06-04.)
        "n_in": len(labels),
        "n_out": len(out_flows),
        "dropped": dropped,
        "by_class": by_class,
        "by_role": by_role,
        "video_kb": video_local.stat().st_size // 1024,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_dataset(
    output_dir: str,
    cache_dir: str,
    t5_filter: Optional[str] = None,
    engine=None,
    s3_client=None,
) -> dict:
    if engine is None:
        engine = _get_engine()
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3")

    rows = _fetch_corpus_rows(engine, t5_filter)
    if not rows:
        raise RuntimeError(
            f"no stroke_classifier corpus rows match filter (t5={t5_filter})"
        )

    output_path = Path(output_dir)
    cache_path = Path(cache_dir)
    per_match = []
    for r in rows:
        try:
            res = build_one_match(r, output_path, cache_path, engine, s3_client)
            per_match.append(res)
        except Exception as e:
            logger.exception("FAILED %s: %s", r["t5_task_id"], e)
            per_match.append({
                "t5_task_id": r["t5_task_id"], "error": f"{e.__class__.__name__}: {e}",
            })

    # Train/val split: every other match goes to val (deterministic).
    # Will need re-thinking once we have >4 matches; for now, simple stride keeps
    # both classes/roles represented across train + val.
    success = [m for m in per_match if "error" not in m]
    train_ids = [m["t5_task_id"] for i, m in enumerate(success) if i % 2 == 0]
    val_ids = [m["t5_task_id"] for i, m in enumerate(success) if i % 2 == 1]

    totals_by_class = {"forehand": 0, "backhand": 0, "overhead": 0}
    totals_by_role = {"NEAR": 0, "FAR": 0}
    total_hits = 0
    for m in success:
        for k in totals_by_class:
            totals_by_class[k] += m["by_class"].get(k, 0)
        for k in totals_by_role:
            totals_by_role[k] += m["by_role"].get(k, 0)
        total_hits += m["n_out"]

    manifest = {
        "builder_version": "v1-2026-05-28",
        "n_matches": len(success),
        "total_hits": total_hits,
        "totals_by_class": totals_by_class,
        "totals_by_role": totals_by_role,
        "train_match_ids": train_ids,
        "val_match_ids": val_ids,
        "matches": per_match,
    }
    (output_path / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True)
    ap.add_argument(
        "--cache-dir", default="ml_pipeline/training/_dataset_cache",
        help="Local cache for downloaded label JSONs + videos.",
    )
    ap.add_argument("--t5", default=None,
                    help="If set, build only this one t5_task_id (smoke testing).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    manifest = build_dataset(
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        t5_filter=args.t5,
    )
    logger.info("=== BUILD SUMMARY ===")
    logger.info("  n_matches=%d  total_hits=%d", manifest["n_matches"], manifest["total_hits"])
    logger.info("  by_class: %s", manifest["totals_by_class"])
    logger.info("  by_role:  %s", manifest["totals_by_role"])
    logger.info("  train_match_ids: %s", manifest["train_match_ids"])
    logger.info("  val_match_ids:   %s", manifest["val_match_ids"])
    logger.info("  manifest: %s/manifest.json", args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
