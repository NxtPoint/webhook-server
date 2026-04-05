# ============================================================
# build_video_timeline.py
#
# PURPOSE
#   Build a canonical, deterministic "video keep timeline" from Silver.
#   This timeline is the foundation for:
#     - MVP1: Trim dead time (FFmpeg worker will consume this EDL)
#     - MVP2: Clickable links to points/events (segment -> timestamp mapping)
#     - Future: ML labeling + eventual SportAI replacement
#
# GOLDEN RULES (ARCHITECTURE)
#   - Python owns all business logic and transformations.
#   - SQL is only for I/O (fetching rows), never for derived logic.
#
# BUSINESS RULES (CURRENT BASELINE)
#   - Use Silver point_detail as input (already “perfect” per your confirmation).
#   - Only include rows where exclude_d == False (NULL treated as False).
#   - Each point segment is defined by ball_hit_s:
#       point_start_s = min(ball_hit_s) per (task_id, point_number)
#       point_end_s   = max(ball_hit_s) per (task_id, point_number)
#   - Apply padding:
#       start_s = max(point_start_s - PAD_BEFORE_S, 0)
#       end_s   = point_end_s + PAD_AFTER_S
#   - Merge behavior:
#       MERGE_GAP_S = 0.0  (baseline): merge overlaps only; do NOT merge close gaps
#   - Drop segments shorter than MIN_SEGMENT_S after merging.
#
# OUTPUT
#   DataFrame with one row per "keep segment" (point_group):
#     task_id, entity_type, entity_id, start_s, end_s, source, confidence
#
# NOTE
#   This file does NOT:
#     - run FFmpeg
#     - write to DB
#     - upload to S3
# ============================================================

from __future__ import annotations

from typing import Dict, List, Tuple, Optional

import pandas as pd

# ============================================================
# CONFIG (MVP1 BASELINE)
# ============================================================

PAD_BEFORE_S = 2.0       # seconds kept before each point start
PAD_AFTER_S = 2.0        # seconds kept after each point end

MERGE_GAP_S = 0.0        # baseline: merge overlaps only (no "close gap" merging)
MIN_SEGMENT_S = 2.0      # discard merged segments shorter than this

# ============================================================
# SECTION 1: INPUT VALIDATION
# ============================================================

REQUIRED_COLUMNS = {"task_id", "point_number", "ball_hit_s", "exclude_d"}


def _validate_silver(df_silver: pd.DataFrame) -> None:
    """Fail fast if required columns are missing or dataframe is empty."""
    if df_silver is None or len(df_silver) == 0:
        raise ValueError("df_silver is empty or None")

    missing = REQUIRED_COLUMNS - set(df_silver.columns)
    if missing:
        raise ValueError(f"df_silver missing required columns: {sorted(missing)}")


# ============================================================
# SECTION 2: BUILD POINT-LEVEL SEGMENTS FROM SILVER
# ============================================================

def _build_point_segments(df_silver: pd.DataFrame, *, task_id: Optional[str] = None) -> pd.DataFrame:
    """
    Build one row per point with start/end seconds derived from Silver.

    Business logic:
      - exclude_d must be False (NULL treated as False)
      - point_start_s = min(ball_hit_s) within point
      - point_end_s   = max(ball_hit_s) within point
      - apply padding:
          start_s = max(point_start_s - PAD_BEFORE_S, 0)
          end_s   = point_end_s + PAD_AFTER_S
    """
    _validate_silver(df_silver)

    # Work on a private copy
    df = df_silver.copy()

    # --------------------------
    # Business rule: exclude_d
    # --------------------------
    df.loc[:, "exclude_d"] = df["exclude_d"].fillna(False).astype(bool)
    df = df.loc[df["exclude_d"] == False].copy()
    if df.empty:
        raise ValueError("After applying exclude_d=False filter, no rows remain.")

    # --------------------------
    # Optional task_id filter
    # --------------------------
    if task_id is not None:
        df.loc[:, "task_id"] = df["task_id"].astype(str).str.strip()
        task_id_norm = str(task_id).strip()

        df = df.loc[df["task_id"] == task_id_norm].copy()
        if df.empty:
            available = df_silver["task_id"].astype(str).str.strip().unique().tolist()
            raise ValueError(
                f"No rows found for task_id={task_id_norm!r}. Available task_id values: {available}"
            )

    # --------------------------
    # Ensure numeric seconds
    # --------------------------
    df.loc[:, "ball_hit_s"] = pd.to_numeric(df["ball_hit_s"], errors="coerce")
    df = df.dropna(subset=["ball_hit_s"]).copy()
    if df.empty:
        raise ValueError("After coercing ball_hit_s to numeric, no valid rows remain.")

    # --------------------------
    # Collapse to point boundaries
    # --------------------------
    points = (
        df.groupby(["task_id", "point_number"], as_index=False)
          .agg(
              point_start_s=("ball_hit_s", "min"),
              point_end_s=("ball_hit_s", "max"),
          )
          .sort_values(["task_id", "point_number"])
          .reset_index(drop=True)
    )

    # --------------------------
    # Apply padding
    # --------------------------
    points.loc[:, "start_s"] = (points["point_start_s"] - PAD_BEFORE_S).clip(lower=0)
    points.loc[:, "end_s"] = points["point_end_s"] + PAD_AFTER_S

    # Canonical point-level timeline shape
    timeline = points[["task_id", "point_number", "start_s", "end_s"]].rename(
        columns={"point_number": "entity_id"}
    )
    timeline.loc[:, "entity_type"] = "point"
    timeline.loc[:, "source"] = "silver"
    timeline.loc[:, "confidence"] = 1.0

    return timeline.reset_index(drop=True)


# ============================================================
# SECTION 3: MERGE + CLEAN SEGMENTS (NON-OVERLAPPING / MONOTONIC)
# ============================================================

def _merge_and_validate_segments(df_segments: pd.DataFrame) -> pd.DataFrame:
    """
    Input:
      DataFrame with columns: start_s, end_s (at minimum)
    Output:
      Clean merged segments:
        - sorted by start_s
        - overlaps merged
        - small gaps merged ONLY if MERGE_GAP_S > 0
        - segments shorter than MIN_SEGMENT_S dropped
        - strictly increasing start_s
    """
    if df_segments is None or df_segments.empty:
        raise ValueError("df_segments is empty; nothing to merge.")

    seg = df_segments[["start_s", "end_s"]].copy()
    seg.loc[:, "start_s"] = pd.to_numeric(seg["start_s"], errors="coerce")
    seg.loc[:, "end_s"] = pd.to_numeric(seg["end_s"], errors="coerce")
    seg = seg.dropna(subset=["start_s", "end_s"]).copy()
    seg = seg.loc[seg["end_s"] > seg["start_s"]].copy()

    if seg.empty:
        raise ValueError("No valid segments remain after cleaning start/end seconds.")

    seg = seg.sort_values("start_s").reset_index(drop=True)

    merged_rows: List[Tuple[float, float]] = []
    cur_start: Optional[float] = None
    cur_end: Optional[float] = None

    for row in seg.itertuples(index=False):
        s = float(row.start_s)
        e = float(row.end_s)

        if cur_start is None:
            cur_start, cur_end = s, e
            continue

        # cur_end is not None here
        gap = s - float(cur_end)

        # Merge if overlapping OR within configured gap (baseline MERGE_GAP_S=0 means overlaps only)
        if gap <= MERGE_GAP_S:
            cur_end = max(float(cur_end), e)
        else:
            if (float(cur_end) - float(cur_start)) >= MIN_SEGMENT_S:
                merged_rows.append((float(cur_start), float(cur_end)))
            cur_start, cur_end = s, e

    # Finalize last segment
    if cur_start is not None and cur_end is not None:
        if (float(cur_end) - float(cur_start)) >= MIN_SEGMENT_S:
            merged_rows.append((float(cur_start), float(cur_end)))

    out = pd.DataFrame(merged_rows, columns=["start_s", "end_s"])

    # --------------------------
    # Final validations
    # --------------------------
    if out.empty:
        raise ValueError("All segments were filtered out; check PAD/MERGE/MIN thresholds.")

    if not (out["end_s"] > out["start_s"]).all():
        raise AssertionError("Invalid segment found where end_s <= start_s")

    diffs = out["start_s"].diff().dropna()
    if not (diffs > 0).all():
        raise AssertionError("Merged segments are not strictly increasing by start_s")

    return out.reset_index(drop=True)


# ============================================================
# SECTION 4: PUBLIC API
# ============================================================

def build_video_timeline_from_silver(df_silver: pd.DataFrame, *, task_id: Optional[str] = None) -> pd.DataFrame:
    """
    Build the merged, canonical "keep segments" timeline.

    Returns:
      DataFrame columns:
        - task_id
        - entity_type   ('point_group' for merged keep segments)
        - entity_id     (1..N)
        - start_s
        - end_s
        - source
        - confidence
    """
    base_points = _build_point_segments(df_silver, task_id=task_id)
    merged = _merge_and_validate_segments(base_points)

    # Determine task_id deterministically
    if task_id is not None:
        merged_task_id = str(task_id).strip()
    else:
        unique_tasks = base_points["task_id"].astype(str).str.strip().unique().tolist()
        if len(unique_tasks) != 1:
            raise ValueError(
                f"df_silver contains multiple task_id values ({unique_tasks}). "
                f"Pass task_id=... to build a single timeline deterministically."
            )
        merged_task_id = unique_tasks[0]

    merged.loc[:, "task_id"] = merged_task_id
    merged.loc[:, "entity_type"] = "point_group"
    merged.loc[:, "entity_id"] = range(1, len(merged) + 1)
    merged.loc[:, "source"] = "silver"
    merged.loc[:, "confidence"] = 1.0

    merged = merged[[
        "task_id",
        "entity_type",
        "entity_id",
        "start_s",
        "end_s",
        "source",
        "confidence",
    ]]

    return merged.reset_index(drop=True)


# ============================================================
# SECTION 5: TIMELINE CONTRACT (FFmpeg worker feed)
# ============================================================

def timeline_to_edl(df_timeline: pd.DataFrame) -> Dict:
    """
    Convert the timeline dataframe to an EDL-like dict that can be:
      - stored (future)
      - sent to an FFmpeg worker (next phase)

    Output schema:
      {
        task_id: str,
        profile: str,
        pad_before_s: float,
        pad_after_s: float,
        merge_gap_s: float,
        min_segment_s: float,
        segments: [{start_s: float, end_s: float}, ...]
      }
    """
    if df_timeline is None or df_timeline.empty:
        raise ValueError("df_timeline is empty; cannot build EDL.")

    segs = [
        {"start_s": float(r.start_s), "end_s": float(r.end_s)}
        for r in df_timeline.itertuples(index=False)
    ]

    return {
        "task_id": str(df_timeline["task_id"].iloc[0]),
        "profile": "mvp_trim_v1",
        "pad_before_s": float(PAD_BEFORE_S),
        "pad_after_s": float(PAD_AFTER_S),
        "merge_gap_s": float(MERGE_GAP_S),
        "min_segment_s": float(MIN_SEGMENT_S),
        "segments": segs,
    }


# ============================================================
# SECTION 6: OPTIONAL CLI SMOKE TEST
# ============================================================

if __name__ == "__main__":
    print(
        "build_video_timeline.py loaded.\n"
        "Import and call build_video_timeline_from_silver(df_silver, task_id=...)."
    )
