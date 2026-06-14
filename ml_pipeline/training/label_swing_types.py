"""Export SA-GT swing-type labels (fh/bh/overhead/other) for ADR-02 classifier training.

Sibling of label_ball_positions.py — same overall shape, different source
table and different downstream consumer.

  - label_ball_positions.py     -> bronze.ball_bounce   -> TrackNet ball-position trainer
  - label_swing_types.py (this) -> bronze.player_swing  -> ADR-02 R(2+1)D-18 swing-type classifier

Why this exists:
  ADR-02 (docs/_investigation/adr_02_swing_type_classifier_plan.md) trains
  a 3-class classifier {forehand, backhand, overhead} on 16-frame optical-
  flow ROIs centred on the player at contact. SportAI's `bronze.player_swing`
  already carries the label we need (`swing_type`) + the frame index of
  contact (`ball_hit_frame`) + the player who hit (`player_id`). One match
  yields ~100-400 swings -> a few matches gets us into the volume range
  ADR-02's v1 needs (~2-3k labelled hit-events).

  The downstream dataset builder (future `build_swing_type_dataset.py`) will
  look up the player bbox at `hit_frame` from `ml_analysis.player_detections`
  / `bronze.player_position`, crop the ROI, and compute optical flow over
  a +/- N frame window. That's why this extractor doesn't bother projecting
  ball coords to pixels -- the classifier's input is the player ROI, not
  the ball position. Court coords are kept for sanity / role classification.

Canonical class mapping (ADR-02 REVISION 2026-06-14 = {forehand, backhand, overhead, other}):
  fh           -> forehand
  1h_bh        -> backhand
  2h_bh        -> backhand
  fh_overhead  -> overhead   (includes serves -- serve is mechanically a smash)
  other        -> other      (4th class — lets the classifier reject non-groundstroke /
                              junk hits instead of mislabelling them; was previously dropped)

`swing_type_raw` is preserved per-label so a future analysis can separate
1h_bh vs 2h_bh, or filter out serves via the `is_serve` flag.

Usage (backfill the SA reference for one dual-submit pair):
    python -m ml_pipeline.training.label_swing_types \\
        --task 78c32f53-... \\
        --sportai 0d0514df-... \\
        --output ml_pipeline/training/labels/78c32f53_swing_types.json

Also exposed as a callable: `export_sa_swing_types(t5_task_id, sa_task_id,
engine=None, ...) -> dict`. The dual-submit pair-completion hook in
upload_app.py calls this in-process and uploads the result to S3 alongside
the ball-position labels, recording a second `ml_analysis.training_corpus`
row with `label_kind='stroke_classifier'`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text as sql_text


logger = logging.getLogger("label_swing_types")

DEFAULT_FRAME_W = 1920
DEFAULT_FRAME_H = 1080
DEFAULT_FPS = 30
DEFAULT_INCLUDE_TYPES = ("forehand", "backhand", "overhead", "other")
HALF_Y = 11.885  # court midline (net) in metres -- matches serve_detector/bounce_validity.py

# SA raw -> canonical class mapping.
# ADR-02 REVISION 2026-06-14: 4-class {fh, bh, overhead, other}. SA's raw
# `other` (291 of 2,592 corpus swings) was previously DROPPED; it is now the
# 4th class so the classifier can reject non-groundstroke / junk hits instead
# of mislabelling them as fh/bh/overhead. Volley is NOT a swing_type — it is a
# separate boolean fact (bronze.player_swing.volley), handled elsewhere.
SA_TO_CANONICAL = {
    "fh": "forehand",
    "1h_bh": "backhand",
    "2h_bh": "backhand",
    "fh_overhead": "overhead",
    "other": "other",
}

# Canonical class set (the valid output vocabulary). Single source for the
# include_types validation below + the model's CLASSES (kept in sync manually;
# model_v2.CLASSES is the model-side authority).
CANONICAL_CLASSES = frozenset({"forehand", "backhand", "overhead", "other"})


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _get_engine():
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
           or os.environ.get("DB_URL"))
    if not url:
        raise RuntimeError("DATABASE_URL required")
    return create_engine(_normalize_db_url(url))


def export_sa_swing_types(
    t5_task_id: str,
    sa_task_id: str,
    engine=None,
    frame_width: int = DEFAULT_FRAME_W,
    frame_height: int = DEFAULT_FRAME_H,
    fps: int = DEFAULT_FPS,
    include_types=DEFAULT_INCLUDE_TYPES,
) -> dict:
    """Build the swing-type label JSON for one (T5, SA) pair and return it as a dict.

    Raises RuntimeError if the SA task has no usable `bronze.player_swing`
    rows after mapping to canonical classes (i.e. the SA pipeline didn't
    emit any classifiable swings, or every row was `other` / missing
    contact frame).

    Does NOT write to disk or to S3 -- the caller decides where the output
    lives. The CLI wrapper (main()) writes to args.output; the upload_app
    pair-completion hook uploads to s3://.
    """
    include = tuple(t.strip().lower() for t in include_types if t and t.strip())
    if not include:
        raise ValueError("include_types must not be empty")
    invalid = [t for t in include if t not in CANONICAL_CLASSES]
    if invalid:
        raise ValueError(
            f"include_types must be subset of {sorted(CANONICAL_CLASSES)}; "
            f"got invalid: {invalid}"
        )

    if engine is None:
        engine = _get_engine()

    W = int(frame_width)
    H = int(frame_height)

    # Only pull rows whose raw swing_type maps to a class the caller wants.
    raw_filter = [raw for raw, canon in SA_TO_CANONICAL.items() if canon in include]

    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT ball_hit_frame, ball_hit_s, ball_hit_location_x, ball_hit_location_y,
                   swing_type, serve, player_id, confidence_swing_type
            FROM bronze.player_swing
            WHERE task_id = :tid
              AND swing_type = ANY(:raw_types)
              AND ball_hit_frame IS NOT NULL
              AND ball_hit_location_x IS NOT NULL
              AND ball_hit_location_y IS NOT NULL
            ORDER BY ball_hit_frame
        """), {"tid": sa_task_id, "raw_types": raw_filter}).mappings().all()

    if not rows:
        raise RuntimeError(
            f"no bronze.player_swing rows for SA task {sa_task_id} with "
            f"swing_type in {raw_filter}"
        )

    labels = []
    n_by_canonical: dict[str, int] = {}
    n_by_raw: dict[str, int] = {}
    n_by_role = {"NEAR": 0, "FAR": 0}
    n_serve = 0
    for r in rows:
        raw = r["swing_type"]
        canonical = SA_TO_CANONICAL.get(raw)
        if canonical is None or canonical not in include:
            continue

        cx = float(r["ball_hit_location_x"])
        cy = float(r["ball_hit_location_y"])
        # Role from court half (SA court coords: y=0 far baseline, y=23.77 near baseline)
        role = "NEAR" if cy > HALF_Y else "FAR"

        is_serve = bool(r["serve"])
        if is_serve:
            n_serve += 1
        n_by_canonical[canonical] = n_by_canonical.get(canonical, 0) + 1
        n_by_raw[raw] = n_by_raw.get(raw, 0) + 1
        n_by_role[role] += 1

        labels.append({
            "hit_frame": int(r["ball_hit_frame"]),
            "hit_ts": float(r["ball_hit_s"]) if r["ball_hit_s"] is not None else None,
            "player_id": int(r["player_id"]) if r["player_id"] is not None else None,
            "swing_type": canonical,
            "swing_type_raw": raw,
            "is_serve": is_serve,
            "court_x": round(cx, 3),
            "court_y": round(cy, 3),
            "role": role,
            "confidence": (
                round(float(r["confidence_swing_type"]), 4)
                if r["confidence_swing_type"] is not None else None
            ),
            "source": "sportai_player_swing",
        })

    return {
        "task_id": t5_task_id,
        "sportai_task_id": sa_task_id,
        "frame_width": W,
        "frame_height": H,
        "fps": int(fps),
        "label_count": len(labels),
        "by_swing_type": n_by_canonical,
        "by_swing_type_raw": n_by_raw,
        "role_breakdown": n_by_role,
        "serve_count": n_serve,
        "labels": labels,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True,
                    help="T5 task_id the labels belong to (for metadata only)")
    ap.add_argument("--sportai", required=True,
                    help="SA task_id to pull bronze.player_swing rows from")
    ap.add_argument("--output", required=True, help="Output JSON path")
    ap.add_argument("--frame-width", type=int, default=DEFAULT_FRAME_W)
    ap.add_argument("--frame-height", type=int, default=DEFAULT_FRAME_H)
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    ap.add_argument("--include-types", default=",".join(DEFAULT_INCLUDE_TYPES),
                    help="Comma-sep list of CANONICAL swing_type values to include. "
                         "Subset of {forehand, backhand, overhead}. Defaults to all "
                         "three. SA's 'other' bucket is always excluded.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    include_types = tuple(t.strip() for t in args.include_types.split(",") if t.strip())

    out = export_sa_swing_types(
        t5_task_id=args.task,
        sa_task_id=args.sportai,
        frame_width=args.frame_width,
        frame_height=args.frame_height,
        fps=args.fps,
        include_types=include_types,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))

    logger.info(
        "pulled %d swing labels (by_type=%s, NEAR=%d FAR=%d, serves=%d)",
        out["label_count"], out["by_swing_type"],
        out["role_breakdown"]["NEAR"], out["role_breakdown"]["FAR"],
        out["serve_count"],
    )
    logger.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
