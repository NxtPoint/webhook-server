"""Training dataset for the bounce_detector CNN.

Two responsibilities:

  1) build_manifest(engine, ...)
       Walks ml_analysis.training_corpus rows (label_kind='ball_position'),
       fetches the per-task labels JSON from S3, joins to the live T5
       bronze (ml_analysis.ball_detections.is_bounce=TRUE candidates), and
       emits a list of (t5_task_id, frame_idx, label, court_x, court_y,
       timestamp) tuples.

       Positives are is_bounce=TRUE candidates within ±5 frames AND
       ≤50 px (image coords) of a SportAI floor label — i.e. the
       "strong-positive" definition from
       `.claude/adr01_label_audit_2026-05-28.md`. Negatives are randomly
       sampled is_bounce=TRUE candidates strictly OUTSIDE ±6 frames of
       any positive (the ±0.2 s exclusion zone the audit recipe asks
       for). Negatives_per_positive defaults to 5 per ADR-01 §"Training
       data assessment".

  2) BounceDataset(torch.utils.data.Dataset)
       Lazy-loads the bronze for each task on first touch (one DB hit
       per task across the dataset's lifetime), then builds the
       (14, 41) feature window per sample via feature_extractor.build_window.
       Returns (features_tensor, label_tensor) — directly consumable
       by the trainer.

Train/val split is stratified by LABEL (not by task) so val isn't
all-negative or all-positive at small sample counts. With Match 1 alone
(67 positives + ~335 negatives) and val_frac=0.2 → val has ~13 pos / ~67 neg.

# STOPGAP-untrained-stage1 caller note: this module produces the
# ground-truth windows the CNN will be trained on. It is NOT exercised
# by the v0 inference path (the detector currently runs in STOPGAP
# mode with threshold=1.1). Once weights ship, the detector's runtime
# path uses the SAME feature_extractor.build_window — guaranteeing
# train/inference parity.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from typing import Optional

import boto3
import numpy as np
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text as sql_text

from ml_pipeline.bounce_detector.feature_extractor import (
    N_CHANNELS,
    WINDOW_FRAMES,
    build_window,
)

logger = logging.getLogger(__name__)


# Defaults — sourced from `.claude/adr01_label_audit_2026-05-28.md`.
POSITIVE_FRAME_TOL = 5            # ±5 frames between SA label and is_bounce candidate
POSITIVE_PIXEL_TOL_PX = 50.0      # ≤50 px image-coord agreement
NEGATIVE_EXCLUSION_FRAMES = 6     # ±0.2 s @ 30 fps; conservative at 25 fps too
DEFAULT_NEG_PER_POS = 5
DEFAULT_FRAME_W = 1920
DEFAULT_FRAME_H = 1080


@dataclass
class _Sample:
    """One row of the manifest. Pickle-friendly (no DB handles)."""
    t5_task_id: str
    frame_idx: int
    label: int                     # 0 (negative) or 1 (positive)
    court_x: Optional[float]
    court_y: Optional[float]
    timestamp: Optional[float]     # seconds; mirrors SA label.timestamp
    fps: float

    def to_dict(self) -> dict:
        return {
            "t5_task_id": self.t5_task_id,
            "frame_idx": int(self.frame_idx),
            "label": int(self.label),
            "court_x": self.court_x,
            "court_y": self.court_y,
            "timestamp": self.timestamp,
            "fps": float(self.fps),
        }


# ---------------------------------------------------------------------------
# S3 + bronze fetchers
# ---------------------------------------------------------------------------

def _fetch_labels(s3_uri: str) -> dict:
    """Pull a label JSON from s3:// and return the parsed dict."""
    assert s3_uri.startswith("s3://"), s3_uri
    bucket, key = s3_uri[5:].split("/", 1)
    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"failed to fetch {s3_uri}: {exc}") from exc
    return json.loads(obj["Body"].read())


def _floor_labels(labels_doc: dict) -> list[dict]:
    """Filter to floor-only labels with non-null court coords + timestamp."""
    out = []
    for l in labels_doc.get("labels", []):
        if l.get("type") != "floor":
            continue
        if l.get("bounce_frame_est") is None:
            continue
        out.append(l)
    return out


def _load_candidates(conn, t5_task_id: str) -> list[dict]:
    """All ball_detections rows for the task, including is_bounce flag +
    pixel coords for the positive-matching gate."""
    # Schema: id, job_id, frame_idx, x, y, court_x, court_y, speed_kmh,
    # is_bounce, is_in, created_at, source. No `confidence` column —
    # feature_extractor.build_window falls back to 1.0 when missing.
    rows = conn.execute(sql_text("""
        SELECT frame_idx, x, y, court_x, court_y, is_bounce
        FROM ml_analysis.ball_detections
        WHERE job_id = :tid
        ORDER BY frame_idx
    """), {"tid": t5_task_id}).mappings().all()
    return [dict(r) for r in rows]


def _load_fps(conn, t5_task_id: str) -> float:
    fps = conn.execute(sql_text(
        "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
        "WHERE job_id = :t OR task_id = :t LIMIT 1"
    ), {"t": t5_task_id}).scalar()
    return float(fps) if fps else 25.0


# ---------------------------------------------------------------------------
# Manifest building
# ---------------------------------------------------------------------------

def _match_label_to_candidate(
    label: dict,
    candidates_by_frame: dict[int, dict],
    frame_tol: int,
    pixel_tol_px: float,
) -> tuple[int, bool]:
    """For one SA floor label, return (frame_idx_to_use, is_strict_match).

    Strict path: nearest is_bounce=TRUE candidate within ±frame_tol AND
    ≤pixel_tol_px image-pixel distance — the audit's "strong positive"
    definition (53% / 36-of-67 on Match 1).

    Fallback path: when no strict candidate exists, anchor at the SA
    `bounce_frame_est` directly. The window builder pulls ball context
    from ±20 frames around it; the model trains on whatever bronze
    coverage there is (some SA labels fall in TrackNet coverage gaps
    and contribute mostly-NaN windows — the BatchNorm in the CNN
    handles this gracefully via nan_to_num in feature_extractor).

    Why fallback: ADR-01's "model scores pre-gated candidates" framing
    assumes a working candidate generator. TrackNet's is_bounce flag has
    ~9% recall vs SA (audit), so strict-mode alone yields 6/67 positives
    — too small to train. Falling back gives us 67 positives at the cost
    of a mild train/inference distribution skew (which the audit notes
    is small: 95% of SA labels have a T5 ball detection within ±5
    frames, so windows are similar regardless of centre frame). Once
    candidate generation graduates to gravity-residual peak detection
    (ADR-01 v1+ follow-up), strict-mode and fallback converge.
    """
    bf = int(label["bounce_frame_est"])
    lbl_px = float(label.get("pixel_x", 0.0) or 0.0)
    lbl_py = float(label.get("pixel_y", 0.0) or 0.0)
    best_fi = None
    best_dist = float("inf")
    for fi in range(bf - frame_tol, bf + frame_tol + 1):
        c = candidates_by_frame.get(fi)
        if c is None or not c.get("is_bounce"):
            continue
        cx = c.get("x")
        cy = c.get("y")
        if cx is None or cy is None:
            continue
        dx = float(cx) - lbl_px
        dy = float(cy) - lbl_py
        d = (dx * dx + dy * dy) ** 0.5
        if d > pixel_tol_px:
            continue
        if abs(fi - bf) < abs((best_fi if best_fi is not None else bf + frame_tol + 1) - bf) or (
            best_fi is not None and abs(fi - bf) == abs(best_fi - bf) and d < best_dist
        ):
            best_fi = fi
            best_dist = d
    if best_fi is not None:
        return best_fi, True
    return bf, False


def build_manifest(
    engine,
    *,
    task_filter: Optional[list[str]] = None,
    neg_per_pos: int = DEFAULT_NEG_PER_POS,
    seed: int = 42,
) -> list[dict]:
    """Walk training_corpus → emit (positives + negatives) manifest.

    engine: SQLAlchemy Engine pointed at the prod DB (the dev box's IP is
        allowlisted; see `reference_local_db_access.md` in memory).
    task_filter: optional list of t5_task_ids to include (rest are skipped).
    neg_per_pos: target ratio of negatives to positives per task. ~5 per ADR.
    seed: RNG seed for negative sampling.

    Returns: list of dicts (one per sample) — see _Sample.to_dict() for keys.

    Empty list when no corpus rows exist or no floor labels are accessible.
    Logs a per-task breakdown so the caller can see what's been mined.
    """
    rng = random.Random(seed)

    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT t5_task_id, sa_task_id, label_s3_key
            FROM ml_analysis.training_corpus
            WHERE label_kind = 'ball_position'
            ORDER BY created_at
        """)).mappings().all()
    corpus_rows = [dict(r) for r in rows]
    if task_filter:
        keep = set(task_filter)
        corpus_rows = [r for r in corpus_rows if r["t5_task_id"] in keep]

    manifest: list[dict] = []
    n_total_floor = 0
    n_total_matched = 0
    n_total_neg = 0

    for row in corpus_rows:
        t5 = row["t5_task_id"]
        try:
            labels_doc = _fetch_labels(row["label_s3_key"])
        except Exception as exc:
            logger.warning("skipping task %s: label fetch failed: %s", t5[:8], exc)
            continue
        floor_lbls = _floor_labels(labels_doc)
        if not floor_lbls:
            logger.info("task %s: 0 floor labels — skipping (expected for "
                        "non-Rivonia courts per the audit)", t5[:8])
            continue

        with engine.connect() as conn:
            fps = _load_fps(conn, t5)
            cands = _load_candidates(conn, t5)
        if not cands:
            logger.warning("task %s: 0 ball_detections rows — skipping", t5[:8])
            continue
        cands_by_frame: dict[int, dict] = {int(c["frame_idx"]): c for c in cands}
        all_bounce_frames = [
            int(c["frame_idx"]) for c in cands if c.get("is_bounce")
        ]

        # --- positives ---
        pos_frames: list[tuple[int, dict]] = []
        n_strict = 0
        for lbl in floor_lbls:
            fi, is_strict = _match_label_to_candidate(
                lbl, cands_by_frame,
                frame_tol=POSITIVE_FRAME_TOL,
                pixel_tol_px=POSITIVE_PIXEL_TOL_PX,
            )
            pos_frames.append((fi, lbl))
            if is_strict:
                n_strict += 1

        # --- negatives ---
        # Exclude any is_bounce frame within ±NEGATIVE_EXCLUSION_FRAMES of
        # any positive. The rest of the is_bounce pool are eligible negatives.
        excluded: set[int] = set()
        for fi, _lbl in pos_frames:
            for k in range(fi - NEGATIVE_EXCLUSION_FRAMES,
                           fi + NEGATIVE_EXCLUSION_FRAMES + 1):
                excluded.add(k)
        eligible_negs = [f for f in all_bounce_frames if f not in excluded]
        target_neg = neg_per_pos * len(pos_frames)
        # Drop duplicates while preserving frame ordering (a frame can only
        # appear once in is_bounce anyway, but defensive against future
        # bronze schema drift).
        eligible_negs = sorted(set(eligible_negs))
        if eligible_negs and target_neg > 0:
            n_to_sample = min(target_neg, len(eligible_negs))
            neg_frames = rng.sample(eligible_negs, n_to_sample)
        else:
            neg_frames = []

        # --- emit ---
        for fi, lbl in pos_frames:
            c = cands_by_frame.get(fi)        # may be None when SA-anchor falls in ball-coverage gap
            cx = float(c["court_x"]) if c and c.get("court_x") is not None else None
            cy = float(c["court_y"]) if c and c.get("court_y") is not None else None
            manifest.append(_Sample(
                t5_task_id=t5, frame_idx=fi, label=1,
                court_x=cx, court_y=cy,
                timestamp=(float(lbl["timestamp"]) if lbl.get("timestamp") is not None else None),
                fps=fps,
            ).to_dict())
        for fi in neg_frames:
            c = cands_by_frame[fi]
            manifest.append(_Sample(
                t5_task_id=t5, frame_idx=fi, label=0,
                court_x=(float(c["court_x"]) if c.get("court_x") is not None else None),
                court_y=(float(c["court_y"]) if c.get("court_y") is not None else None),
                timestamp=fi / fps if fps else None,
                fps=fps,
            ).to_dict())

        n_total_floor += len(floor_lbls)
        n_total_matched += len(pos_frames)
        n_total_neg += len(neg_frames)
        logger.info(
            "task %s: floor_labels=%d  positives=%d (strict=%d/%.0f%%, "
            "anchor_fallback=%d)  eligible_negs=%d  sampled_negs=%d  fps=%.1f",
            t5[:8], len(floor_lbls), len(pos_frames),
            n_strict, 100.0 * n_strict / max(1, len(floor_lbls)),
            len(pos_frames) - n_strict, len(eligible_negs), len(neg_frames), fps,
        )

    logger.info(
        "manifest built: tasks=%d  total_floor_labels=%d  positives_emitted=%d  "
        "negatives=%d  total_samples=%d",
        len(corpus_rows), n_total_floor, n_total_matched, n_total_neg,
        len(manifest),
    )
    return manifest


# ---------------------------------------------------------------------------
# Train/val split
# ---------------------------------------------------------------------------

def train_val_split(
    manifest: list[dict],
    val_frac: float = 0.2,
    seed: int = 42,
) -> tuple[list[dict], list[dict]]:
    """Stratified split by LABEL — guarantees each split has both classes.

    At small sample counts (67 positives) a random split would frequently
    leave val with 0 positives. Stratification keeps val pos/neg ratio
    close to the global ratio.
    """
    pos = [s for s in manifest if s["label"] == 1]
    neg = [s for s in manifest if s["label"] == 0]
    rng = random.Random(seed)
    rng.shuffle(pos)
    rng.shuffle(neg)
    n_val_pos = max(1, int(round(val_frac * len(pos)))) if pos else 0
    n_val_neg = max(1, int(round(val_frac * len(neg)))) if neg else 0
    val = pos[:n_val_pos] + neg[:n_val_neg]
    train = pos[n_val_pos:] + neg[n_val_neg:]
    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


# ---------------------------------------------------------------------------
# PyTorch Dataset
# ---------------------------------------------------------------------------

class BounceDataset:
    """Lazy PyTorch Dataset over a bounce manifest.

    The bronze for each task is loaded ONCE on first touch and cached
    in-process. For 1-5 tasks that's a few thousand rows each — fits in
    memory comfortably and avoids re-querying for every __getitem__.

    Returns per sample:
        {"features": Tensor(14, 41), "label": Tensor(scalar float)}

    We import torch lazily so this module stays cheap to import for the
    manifest-only paths (bench, dataset construction CLI).
    """

    def __init__(self, manifest: list[dict], engine):
        self.manifest = list(manifest)
        self._engine = engine
        self._bronze_cache: dict[str, dict] = {}    # t5_task_id -> {ball_by_frame, wrists_by_frame, rally_by_frame}

    def __len__(self) -> int:
        return len(self.manifest)

    def _ensure_task_bronze(self, t5_task_id: str) -> dict:
        cached = self._bronze_cache.get(t5_task_id)
        if cached is not None:
            return cached
        # Reuse the detector's loaders — keeps train/inference parity exact.
        from ml_pipeline.bounce_detector.detector import (
            _load_ball_rows,
            _load_rally_states_by_frame,
            _load_wrist_positions,
        )
        with self._engine.connect() as conn:
            fps = _load_fps(conn, t5_task_id)
            ball_rows = _load_ball_rows(conn, t5_task_id)
            wrists_by_frame = _load_wrist_positions(conn, t5_task_id)
            last_frame = max((int(r["frame_idx"]) for r in ball_rows), default=0)
            rally_by_frame = _load_rally_states_by_frame(
                conn, t5_task_id, fps, last_frame,
            )
        ball_by_frame = {int(r["frame_idx"]): r for r in ball_rows}
        cached = {
            "ball_by_frame": ball_by_frame,
            "wrists_by_frame": wrists_by_frame,
            "rally_by_frame": rally_by_frame,
            "fps": fps,
        }
        self._bronze_cache[t5_task_id] = cached
        return cached

    def __getitem__(self, idx: int) -> dict:
        import torch  # lazy
        s = self.manifest[idx]
        bronze = self._ensure_task_bronze(s["t5_task_id"])
        wrists = bronze["wrists_by_frame"].get(int(s["frame_idx"]), [])
        rally = bronze["rally_by_frame"].get(int(s["frame_idx"]))
        feats = build_window(
            candidate_frame_idx=int(s["frame_idx"]),
            ball_rows_by_frame=bronze["ball_by_frame"],
            wrist_positions_at_centre=wrists,
            rally_state_at_centre=rally,
        )
        # build_window guarantees shape (N_CHANNELS, WINDOW_FRAMES) — assert
        # so a future feature change can't silently corrupt training.
        assert feats.shape == (N_CHANNELS, WINDOW_FRAMES), (
            f"feature shape {feats.shape} != ({N_CHANNELS},{WINDOW_FRAMES})"
        )
        return {
            "features": torch.from_numpy(np.ascontiguousarray(feats, dtype=np.float32)),
            "label": torch.tensor(float(s["label"]), dtype=torch.float32),
            "t5_task_id": s["t5_task_id"],
            "frame_idx": int(s["frame_idx"]),
        }


def class_counts(manifest: list[dict]) -> dict[int, int]:
    """{0: n_neg, 1: n_pos} — convenience for WeightedRandomSampler."""
    out = {0: 0, 1: 0}
    for s in manifest:
        out[int(s["label"])] = out.get(int(s["label"]), 0) + 1
    return out
