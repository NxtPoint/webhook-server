"""Per-frame source-preference dedup for ml_analysis.ball_detections.

Lives at the package root (NOT under roi_extractors/, whose __init__ imports
cv2/torch and fails on Render) so the Render-side readers — serve_detector and
build_silver_match_t5 — can import it. Pure SQL strings, zero dependencies.

Why this exists
---------------
far_ball.py (source='roi_far_ball') and bounces.py (source='roi_prod') write
EXTRA rows into ml_analysis.ball_detections that OVERLAP the global ball
(source NULL or 'main') at the same (job_id, frame_idx). Any reader that loads
a trajectory `ORDER BY frame_idx` WITHOUT dedup would then see 2+ rows per
re-detected frame and corrupt the velocity / gravity-residual signal the hit
model + bounce candidate generator depend on. (Merge strategy Option A,
docs/_investigation/far_ball_roi.md.)

The canonical pick, per frame, is the SHARPEST re-detection available:

    roi_far_ball  >  roi_prod  >  main  >  (legacy NULL)

`merged_ball_subquery()` returns a `SELECT DISTINCT ON (frame_idx) ...` that any
trajectory reader can substitute for `FROM ml_analysis.ball_detections WHERE
job_id = :tid`. It is a NO-OP until roi_* rows exist (with only NULL/main rows
present, every frame has one row and the dedup picks it unchanged).

NOT for the silver is_bounce reader. Silver Pass-1 is bounce-driven (one row per
bounce); letting roi-source rows add net-new bounce events 2.4x'd the active row
count and wrecked SA reconciliation (bronze_ingest_t5.py:272, 2026-06-04). Silver
keeps reading main/NULL ONLY — use `MAIN_ONLY_WHERE` there, not this dedup.
"""
from __future__ import annotations

# Per-frame source priority — LOWER wins. A single named constant so the
# preference is tunable in one place and identical across every reader.
BALL_SOURCE_RANK_SQL = (
    "CASE source "
    "WHEN 'roi_far_ball' THEN 0 "
    "WHEN 'roi_prod' THEN 1 "
    "WHEN 'main' THEN 2 "
    "ELSE 3 END"
)

# The silver / main-only ball set: exclude every ROI re-detection source so
# roi rows never add net-new shot events to the bounce-driven silver build.
MAIN_ONLY_WHERE = "(source IS NULL OR source = 'main')"


def merged_ball_subquery(columns: str, *, job_pred: str = "job_id = :tid",
                         extra_where: str = "") -> str:
    """Build a per-frame source-preference-deduped ball SELECT.

    Args:
        columns: comma-separated column list to project (must include
            frame_idx, e.g. "frame_idx, x, y, court_x, court_y, is_bounce").
        job_pred: the job predicate, verbatim, so each reader keeps its own
            cast/param (default "job_id = :tid"; hit_model uses
            "job_id::text = :t").
        extra_where: optional additional predicate ANDed in (no leading AND),
            e.g. "x IS NOT NULL". Applied BEFORE the per-frame pick.

    Returns a SQL string ordered by frame_idx, one row per frame_idx, that is a
    drop-in trajectory source. DISTINCT ON requires the leading ORDER BY key to
    be frame_idx; the source rank is the tiebreak that selects the sharp row.
    """
    where = job_pred
    if extra_where:
        where += f" AND ({extra_where})"
    return (
        f"SELECT DISTINCT ON (frame_idx) {columns}\n"
        f"FROM ml_analysis.ball_detections\n"
        f"WHERE {where}\n"
        f"ORDER BY frame_idx, {BALL_SOURCE_RANK_SQL}"
    )
