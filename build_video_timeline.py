# ============================================================
# build_video_timeline.py
#
# PURPOSE
#   Build a canonical, deterministic "video timeline" from Silver
#   (Python-only; no SQL). This timeline is the foundation for:
#     - MVP1: trimming dead time (FFmpeg later)
#     - MVP2: clickable deep links to events
#     - Future: ML labeling / SportAI replacement
#
# WHAT THIS FILE DOES (Phase 0)
#   1) Build point-level segments from Silver (start/end per point)
#   2) Pad each segment (2s before + 2s after)
#   3) Merge overlaps and small gaps into clean "keep segments"
#   4) Validate monotonicity and sanity
#   5) Return a merged timeline DataFrame (ready for persistence later)
#
# WHAT THIS FILE DOES NOT DO (yet)
#   - FFmpeg processing
#   - S3 uploads
#   - Database writes
#
# USAGE (example)
#   from build_video_timeline import build_video_timeline_from_silver
#   df_timeline = build_video_timeline_from_silver(df_silver, task_id="...")
# ============================================================

from __future__ import annotations

import pandas as pd

# ============================================================
# CONFIG (MVP1 defaults)
# ============================================================

PAD_BEFORE_S = 2.0       # seconds kept before each point start
PAD_AFTER_S  = 2.0       # seconds kept after each point end

MERGE_GAP_S = 1.0        # if gap between segments <= this, merge them
MIN_SEGMENT_S = 2.0      # discard merged segments shorter than this

# ============================================================
# SECTION 1: INPUT VALIDATION
# ============================================================

REQUIRED_COLUMNS = {"task_id", "point_number", "ball_hit_s"}


def _validate_silver(df_silver: pd.DataFrame) -> None:
    """Fail fast if required columns are missing or empty."""
    if df_silver is None or len(df_silver) == 0:
        raise ValueError("df_silver is empty or None")

    missing = REQUIRED_COLUMNS - set(df_silver.columns)
    if missing:
        raise ValueError(f"df_silver missing required columns: {sorted(missing)}")


# ============================================================
# SECTION 2: BUILD POINT-LEVEL SEGMENTS FROM SILVER
# ============================================================

def _build_point_segments(df_silver: pd.DataFrame, *, task_id: str | None = None) -> pd.DataFrame:
    """
    Build one row per point with start/end seconds derived from Silver.

    Rules:
      - point_start_s = min(ball_hit_s) within point
      - point_end_s   = max(ball_hit_s) within point
      - padding applied: start_s = max(point_start_s - PAD_BEFORE_S, 0)
                         end_s   = point_end_s + PAD_AFTER_S
    """
    _validate_silver(df_silver)

    df = df_silver.copy()

    # Optional filter to a single task_id for safety + easier debugging
    if task_id is not None:
        df = df[df["task_id"] == task_id].copy()
        if df.empty:
            raise ValueError(f"No rows found in df_silver for task_id={task_id!r}")

    # Ensure numeric seconds
    df["ball_hit_s"] = pd.to_numeric(df["ball_hit_s"], errors="coerce")
    df = df.dropna(subset=["ball_hit_s"])
    if df.empty:
        raise ValueError("After coercing ball_hit_s to numeric, no valid rows remain.")

    points = (
        df.groupby(["task_id", "point_number"], as_index=False)
          .agg(
              point_start_s=("ball_hit_s", "min"),
              point_end_s=("ball_hit_s", "max"),
          )
          .sort_values(["task_id", "point_number"])
          .reset_index(drop=True)
    )

    # Apply padding
    points["start_s"] = (points["point_start_s"] - PAD_BEFORE_S).clip(lower=0)
    points["end_s"] = points["point_end_s"] + PAD_AFTER_S

    # Shape into a canonical timeline-like dataframe (point granularity)
    timeline = points[["task_id", "point_number", "start_s", "end_s"]].rename(
        columns={"point_number": "entity_id"}
    )
    timeline["entity_type"] = "point"
    timeline["source"] = "silver"
    timeline["confidence"] = 1.0

    return timeline.reset_index(drop=True)


# ============================================================
# SECTION 3: MERGE + CLEAN SEGMENTS (NON-OVERLAPPING / MONOTONIC)
# ============================================================

def _merge_and_validate_segments(df_segments: pd.DataFrame) -> pd.DataFrame:
    """
    Input:
      DataFrame with at least columns: start_s, end_s
    Output:
      Clean merged segments:
        - sorted by start_s
        - overlaps merged
        - small gaps (<= MERGE_GAP_S) merged
        - segments shorter than MIN_SEGMENT_S dropped
    """
    if df_segments is None or df_segments.empty:
        raise ValueError("df_segments is empty; nothing to merge.")

    seg = df_segments[["start_s", "end_s"]].copy()
    seg["start_s"] = pd.to_numeric(seg["start_s"], errors="coerce")
    seg["end_s"] = pd.to_numeric(seg["end_s"], errors="coerce")
    seg = seg.dropna(subset=["start_s", "end_s"])
    seg = seg[seg["end_s"] > seg["start_s"]].copy()

    if seg.empty:
        raise ValueError("No valid segments remain after cleaning start/end seconds.")

    seg = seg.sort_values("start_s").reset_index(drop=True)

    merged_rows: list[tuple[float, float]] = []
    cur_start: float | None = None
    cur_end: float | None = None

    for row in seg.itertuples(index=False):
        s = float(row.start_s)
        e = float(row.end_s)

        if cur_start is None:
            cur_start, cur_end = s, e
            continue

        gap = s - cur_end  # type: ignore[arg-type]

        # Merge if overlapping or close enough
        if gap <= MERGE_GAP_S:
            cur_end = max(cur_end, e)  # type: ignore[arg-type]
        else:
            # finalize current
            if (cur_end - cur_start) >= MIN_SEGMENT_S:  # type: ignore[operator]
                merged_rows.append((cur_start, cur_end))  # type: ignore[arg-type]
            cur_start, cur_end = s, e

    # finalize last
    if cur_start is not None and cur_end is not None:
        if (cur_end - cur_start) >= MIN_SEGMENT_S:
            merged_rows.append((cur_start, cur_end))

    out = pd.DataFrame(merged_rows, columns=["start_s", "end_s"])

    # Final validations
    if out.empty:
        raise ValueError("All segments were filtered out; check PAD/MERGE/MIN thresholds.")

    if not (out["end_s"] > out["start_s"]).all():
        raise AssertionError("Invalid segment found where end_s <= start_s")

    # Strictly increasing start times
    diffs = out["start_s"].diff().dropna()
    if not (diffs > 0).all():
        raise AssertionError("Merged segments are not strictly increasing by start_s")

    return out.reset_index(drop=True)


# ============================================================
# SECTION 4: PUBLIC API (THIS IS WHAT YOU IMPORT AND USE)
# ============================================================

def build_video_timeline_from_silver(df_silver: pd.DataFrame, *, task_id: str | None = None) -> pd.DataFrame:
    """
    Public function: build the merged, canonical "keep segments" timeline.

    Returns:
      DataFrame columns:
        - task_id
        - entity_type   ('point_group' for merged segments)
        - entity_id     (1..N)
        - start_s
        - end_s
        - source
        - confidence
    """
    base_points = _build_point_segments(df_silver, task_id=task_id)
    merged = _merge_and_validate_segments(base_points)

    # Attach metadata to merged segments
    # We keep task_id by selecting it from base_points (single task if task_id passed)
    if task_id is not None:
        merged_task_id = task_id
    else:
        # If multiple task_ids exist, we require caller to filter.
        unique_tasks = base_points["task_id"].unique().tolist()
        if len(unique_tasks) != 1:
            raise ValueError(
                f"df_silver contains multiple task_id values ({unique_tasks}). "
                f"Pass task_id=... to build a single timeline deterministically."
            )
        merged_task_id = unique_tasks[0]

    merged["task_id"] = merged_task_id
    merged["entity_type"] = "point_group"
    merged["entity_id"] = range(1, len(merged) + 1)
    merged["source"] = "silver"
    merged["confidence"] = 1.0

    # Reorder columns for consistency
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
# SECTION 5: OPTIONAL CLI TEST (SAFE LOCAL DEBUG)
#   This section does nothing unless you run:
#     python build_video_timeline.py
# ============================================================

if __name__ == "__main__":
    # Minimal self-test scaffold (you must replace with real df_silver load)
    # Example:
    #   df_silver = pd.read_parquet("some_silver_extract.parquet")
    #   out = build_video_timeline_from_silver(df_silver, task_id="7")
    #   print(out.head(10))
    #
    print("build_video_timeline.py loaded. Import and call build_video_timeline_from_silver(df_silver, task_id=...).")
