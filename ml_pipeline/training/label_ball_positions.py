"""Export ALL SA-GT ball positions (hits + bounces) as TrackNet labels.

Why this exists alongside label_serve_bounces.py:
  The original labeler only emits SERVE bounces (~23 labels per match)
  because it filters `silver.point_detail.serve_d = TRUE` and needs the
  court-homography projection to translate SA's normalised court coords
  to pixels. That's 23 / match — well below the 100+ figure needed for
  a meaningful TrackNet fine-tune.

  `bronze.ball_bounce` contains EVERY SA ball event for a match —
  serves, returns, rally shots, final-point bounces — with exact
  `frame_nr` and already-pixel-normalised `image_x / image_y`. A typical
  10-minute match has ~160 rows. No court calibration needed, no
  serve-only filter. So one match here = ~160 labels vs ~23 previously
  (~7× uplift per match with the data we already have).

  Type field splits `swing` (ball at contact / racket level) from
  `floor` (ball on ground). Both are valid TrackNet training targets
  — the model just needs to know where the ball is in the frame at
  a known frame index.

Output matches the label_serve_bounces.py schema so the downstream
`build_serve_bounce_dataset.py` can consume either interchangeably
(uses `bounce_frame_est`, `pixel_x/y`, `frame_width/height`, and
reports `role`).

Usage (labelling the SA reference for the 8a5e0b5e dual-submit):
    python -m ml_pipeline.training.label_ball_positions \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088 \\
        --sportai 2c1ad953-b65b-41b4-9999-975964ff92e1 \\
        --output ml_pipeline/training/labels/8a5e0b5e_ball_positions.json

Also exposed as a callable: `export_sa_ball_positions(t5_task_id,
sa_task_id, engine=None, ...) -> dict`. The Phase 5c.2 pair-completion
hook in upload_app.py calls this in-process and uploads the result to
S3, avoiding a subprocess hop.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text as sql_text


logger = logging.getLogger("label_ball_positions")

DEFAULT_FRAME_W = 1920
DEFAULT_FRAME_H = 1080
DEFAULT_INCLUDE_TYPES = ("swing", "floor")


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


def export_sa_ball_positions(
    t5_task_id: str,
    sa_task_id: str,
    engine=None,
    frame_width: int = DEFAULT_FRAME_W,
    frame_height: int = DEFAULT_FRAME_H,
    include_types=DEFAULT_INCLUDE_TYPES,
) -> dict:
    """Build the label JSON for one (T5, SA) pair and return it as a dict.

    Raises RuntimeError if the SA task has no bronze.ball_bounce rows
    (i.e. the SA pipeline didn't emit any ball events — either it
    failed silently or the match has no usable rallies).

    Does NOT write to disk or to S3 — the caller decides where the
    output lives. The CLI wrapper (main()) writes to args.output;
    the upload_app pair-completion hook uploads to s3://.
    """
    type_filter = tuple(t.strip() for t in include_types if t and t.strip())
    if not type_filter:
        raise ValueError("include_types must not be empty")

    if engine is None:
        engine = _get_engine()

    W = int(frame_width)
    H = int(frame_height)

    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT frame_nr, image_x, image_y, court_x, court_y,
                   timestamp, type
            FROM bronze.ball_bounce
            WHERE task_id = :tid
              AND type = ANY(:types)
              AND frame_nr IS NOT NULL
              AND image_x IS NOT NULL
              AND image_y IS NOT NULL
            ORDER BY frame_nr
        """), {"tid": sa_task_id, "types": list(type_filter)}).mappings().all()

    if not rows:
        raise RuntimeError(f"no bronze.ball_bounce rows for SA task {sa_task_id}")

    labels = []
    n_oob = 0
    n_by_type: dict[str, int] = {}
    n_by_role = {"NEAR": 0, "FAR": 0, "other": 0}
    for r in rows:
        px = float(r["image_x"]) * W
        py = float(r["image_y"]) * H
        if not (0 <= px < W and 0 <= py < H):
            n_oob += 1
            continue
        cy = r["court_y"]
        role = None
        if cy is not None:
            cy_f = float(cy)
            if cy_f > 22:
                role = "NEAR"
            elif cy_f < 2:
                role = "FAR"
        n_by_type[r["type"]] = n_by_type.get(r["type"], 0) + 1
        if role in ("NEAR", "FAR"):
            n_by_role[role] += 1
        else:
            n_by_role["other"] += 1
        labels.append({
            # Same schema key name the downstream builder looks for:
            "bounce_frame_est": int(r["frame_nr"]),
            "bounce_frame_search": [int(r["frame_nr"]), int(r["frame_nr"])],
            "pixel_x": round(px, 2),
            "pixel_y": round(py, 2),
            "court_x": round(float(r["court_x"]), 3) if r["court_x"] is not None else None,
            "court_y": round(float(r["court_y"]), 3) if r["court_y"] is not None else None,
            "timestamp": float(r["timestamp"]) if r["timestamp"] is not None else None,
            "role": role,
            "type": r["type"],
            "source": "sportai_ball_bounce",
        })

    return {
        "task_id": t5_task_id,
        "sportai_task_id": sa_task_id,
        "frame_height": H,
        "frame_width": W,
        "label_count": len(labels),
        "out_of_bounds": n_oob,
        "by_type": n_by_type,
        "role_breakdown": {"NEAR": n_by_role["NEAR"], "FAR": n_by_role["FAR"]},
        "labels": labels,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True,
                    help="T5 task_id the labels belong to (for metadata only)")
    ap.add_argument("--sportai", required=True,
                    help="SA task_id to pull bronze.ball_bounce rows from")
    ap.add_argument("--output", required=True, help="Output JSON path")
    ap.add_argument("--frame-width", type=int, default=DEFAULT_FRAME_W)
    ap.add_argument("--frame-height", type=int, default=DEFAULT_FRAME_H)
    ap.add_argument("--include-types", default=",".join(DEFAULT_INCLUDE_TYPES),
                    help="Comma-sep list of bronze.ball_bounce.type values "
                         "to include. 'swing'=hit, 'floor'=bounce. Default "
                         "includes both — every SA ball sighting is training "
                         "signal for TrackNet.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    include_types = tuple(t.strip() for t in args.include_types.split(",") if t.strip())

    out = export_sa_ball_positions(
        t5_task_id=args.task,
        sa_task_id=args.sportai,
        frame_width=args.frame_width,
        frame_height=args.frame_height,
        include_types=include_types,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))

    logger.info("pulled %d labels (out_of_bounds=%d, by_type=%s, NEAR=%d FAR=%d other=%d)",
                out["label_count"], out["out_of_bounds"], out["by_type"],
                out["role_breakdown"]["NEAR"], out["role_breakdown"]["FAR"],
                out["label_count"] - out["role_breakdown"]["NEAR"] - out["role_breakdown"]["FAR"])
    logger.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
