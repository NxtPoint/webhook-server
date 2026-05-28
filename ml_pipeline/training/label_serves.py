"""Export SA-GT serve labels for Stream 3 (serve_detector v2 training).

Third corpus extractor in the dual-submit pipeline. Pattern is identical
to label_ball_positions.py + label_swing_types.py -- same DB engine helper,
same idempotent UPSERT shape via _label_pair_now in upload_app.py.

  - label_ball_positions.py  -> bronze.ball_bounce          -> TrackNet ball-position trainer
  - label_swing_types.py     -> bronze.player_swing         -> ADR-02 R(2+1)D-18 swing-type classifier
  - label_serves.py (this)   -> bronze.player_swing serves  -> serve_detector v2 (lift past dev ceiling)

Why this exists:
  serve_detector is at dev ceiling (20/24 + 23/24 bench, locked). The
  dominant residual FP is the receiver standing at their baseline --
  geometrically identical to a server, undiscriminable by dev heuristics
  (proven twice via bench regressions on suppressor attempts -- see
  north_star.md §"Serve precision DEV CEILING"). Only training on a
  positive/negative serve corpus can move the needle further.

  This extractor produces the POSITIVE serve set from SA's
  bronze.player_swing.serve=TRUE rows. Negative-mining (the "receiver
  standing at baseline at moment X" half) is a training-time concern,
  done by the consumer against ml_analysis.player_detections at the
  same frames.

What's in a serve row:
  - hit_frame, hit_ts, player_id            -- frame anchor + which player
  - court_x, court_y, role                  -- which baseline (NEAR/FAR via HALF_Y)
  - swing_type_raw                          -- usually 'fh_overhead', occasionally 'other'
  - ball_speed                              -- 50% coverage; useful feature for the v2 model
  - confidence                              -- row-level SA confidence (always populated)
  - bounce_court_x, bounce_court_y          -- where the served ball landed (~75% coverage);
                                              feeds fault detection + placement analytics

Usage:
    python -m ml_pipeline.training.label_serves \\
        --task 78c32f53-... \\
        --sportai 0d0514df-... \\
        --output ml_pipeline/training/labels/78c32f53_serves.json

Also exposed as a callable: `export_sa_serves(t5_task_id, sa_task_id,
engine=None, ...) -> dict`. The dual-submit pair-completion hook in
upload_app.py calls this in-process as the third _label_one_kind invocation
(after ball_position + stroke_classifier), recording a third
ml_analysis.training_corpus row with label_kind='serve'.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text as sql_text


logger = logging.getLogger("label_serves")

DEFAULT_FRAME_W = 1920
DEFAULT_FRAME_H = 1080
DEFAULT_FPS = 30
HALF_Y = 11.885  # court midline (net) in metres -- matches serve_detector/bounce_validity.py


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


def export_sa_serves(
    t5_task_id: str,
    sa_task_id: str,
    engine=None,
    frame_width: int = DEFAULT_FRAME_W,
    frame_height: int = DEFAULT_FRAME_H,
    fps: int = DEFAULT_FPS,
) -> dict:
    """Build the serve label JSON for one (T5, SA) pair and return it as a dict.

    Filters bronze.player_swing on `serve = TRUE`. Requires a
    `ball_hit_frame` + `ball_hit_location_x/y`; rows missing those are
    dropped silently (counted under `dropped_incomplete`).

    Raises RuntimeError if the SA task has no usable serve rows.

    Does NOT write to disk or to S3 -- the caller decides where the output
    lives. The CLI wrapper (main()) writes to args.output; the upload_app
    pair-completion hook uploads to s3://.
    """
    if engine is None:
        engine = _get_engine()

    W = int(frame_width)
    H = int(frame_height)

    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT ball_hit_frame, ball_hit_s, player_id, swing_type,
                   ball_hit_location_x, ball_hit_location_y,
                   ball_impact_location_x, ball_impact_location_y,
                   ball_speed, confidence, confidence_swing_type
            FROM bronze.player_swing
            WHERE task_id = :tid
              AND serve = TRUE
            ORDER BY ball_hit_frame NULLS LAST
        """), {"tid": sa_task_id}).mappings().all()

    if not rows:
        raise RuntimeError(f"no bronze.player_swing serve rows for SA task {sa_task_id}")

    labels = []
    n_dropped = 0
    n_by_role = {"NEAR": 0, "FAR": 0}
    n_by_swing_type_raw: dict[str, int] = {}
    n_have_speed = 0
    n_have_bounce = 0
    for r in rows:
        if (r["ball_hit_frame"] is None
                or r["ball_hit_location_x"] is None
                or r["ball_hit_location_y"] is None):
            n_dropped += 1
            continue

        cx = float(r["ball_hit_location_x"])
        cy = float(r["ball_hit_location_y"])
        role = "NEAR" if cy > HALF_Y else "FAR"
        raw = r["swing_type"] or "unknown"

        n_by_role[role] += 1
        n_by_swing_type_raw[raw] = n_by_swing_type_raw.get(raw, 0) + 1
        if r["ball_speed"] is not None:
            n_have_speed += 1
        bounce_x = r["ball_impact_location_x"]
        bounce_y = r["ball_impact_location_y"]
        if bounce_x is not None and bounce_y is not None:
            n_have_bounce += 1

        labels.append({
            "hit_frame": int(r["ball_hit_frame"]),
            "hit_ts": float(r["ball_hit_s"]) if r["ball_hit_s"] is not None else None,
            "player_id": int(r["player_id"]) if r["player_id"] is not None else None,
            "swing_type_raw": raw,
            "court_x": round(cx, 3),
            "court_y": round(cy, 3),
            "role": role,
            "ball_speed": (
                round(float(r["ball_speed"]), 2) if r["ball_speed"] is not None else None
            ),
            "bounce_court_x": (
                round(float(bounce_x), 3) if bounce_x is not None else None
            ),
            "bounce_court_y": (
                round(float(bounce_y), 3) if bounce_y is not None else None
            ),
            "confidence": (
                round(float(r["confidence"]), 4)
                if r["confidence"] is not None else None
            ),
            "confidence_swing_type": (
                round(float(r["confidence_swing_type"]), 4)
                if r["confidence_swing_type"] is not None else None
            ),
            "source": "sportai_player_swing_serve",
        })

    return {
        "task_id": t5_task_id,
        "sportai_task_id": sa_task_id,
        "frame_width": W,
        "frame_height": H,
        "fps": int(fps),
        "label_count": len(labels),
        "dropped_incomplete": n_dropped,
        "role_breakdown": n_by_role,
        "by_swing_type_raw": n_by_swing_type_raw,
        "ball_speed_coverage": n_have_speed,
        "bounce_location_coverage": n_have_bounce,
        "labels": labels,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True,
                    help="T5 task_id the labels belong to (for metadata only)")
    ap.add_argument("--sportai", required=True,
                    help="SA task_id to pull bronze.player_swing serve rows from")
    ap.add_argument("--output", required=True, help="Output JSON path")
    ap.add_argument("--frame-width", type=int, default=DEFAULT_FRAME_W)
    ap.add_argument("--frame-height", type=int, default=DEFAULT_FRAME_H)
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    out = export_sa_serves(
        t5_task_id=args.task,
        sa_task_id=args.sportai,
        frame_width=args.frame_width,
        frame_height=args.frame_height,
        fps=args.fps,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))

    logger.info(
        "pulled %d serve labels (dropped=%d, NEAR=%d FAR=%d, by_raw=%s, "
        "ball_speed_coverage=%d, bounce_location_coverage=%d)",
        out["label_count"], out["dropped_incomplete"],
        out["role_breakdown"]["NEAR"], out["role_breakdown"]["FAR"],
        out["by_swing_type_raw"],
        out["ball_speed_coverage"], out["bounce_location_coverage"],
    )
    logger.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
