"""Candidate anchor generation for the serve model.

Pure functions over per-task arrays (no DB access — dataset.py and the
future detector wire-in do the loading). All timestamps are SECONDS in the
task's sampled-frame space (frame_idx / sampled_fps) — callers convert via
timestamp only, never frame counts (feedback_t5_two_frame_spaces).

Measured anchor recall on the 200 corpus FAR labels (2026-06-06):
bounce 86%, pose 74%, union 98.5%.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import List, Sequence

HALF_Y = 11.885  # singles court midline, same constant as serve_detector

# Anchors closer than this are merged (keep the earliest of a burst): a far
# serve produces one bounce + a pose burst around the same moment.
MERGE_GAP_S = 1.0


@dataclass
class Anchor:
    ts: float
    source: str               # 'bounce' | 'pose'
    bounce_court_x: float | None = None
    bounce_court_y: float | None = None
    extras: dict = field(default_factory=dict)


def bounce_anchors(ball_rows: Sequence[dict], fps: float) -> List[Anchor]:
    """Every bounce on the NEAR half (or unprojected) is a candidate anchor.

    Deliberately NO service-box gate — faults landing long/wide are real
    serve attempts (the heuristic's blind spot). `ball_rows` may carry
    either the legacy is_bounce flags or the CNN-rewritten ones; both work.
    """
    out: List[Anchor] = []
    for b in ball_rows:
        if not b.get("is_bounce"):
            continue
        cy = b.get("court_y")
        if cy is not None and cy <= HALF_Y:
            continue  # far-half bounce — not evidence of a FAR player's serve
        out.append(Anchor(
            ts=b["frame_idx"] / fps,
            source="bounce",
            bounce_court_x=b.get("court_x"),
            bounce_court_y=cy,
        ))
    return out


def pose_anchors(roi_ts: Sequence[float],
                 min_burst: int = 3,
                 burst_window_s: float = 2.0) -> List[Anchor]:
    """Bursts of far-player ROI pose rows are candidate anchors.

    The ROI sweep only emits rows in not-IN_RALLY windows near the far
    baseline, so a dense burst IS serve-preparation evidence. Anchor at the
    burst centre.
    """
    ts = sorted(roi_ts)
    out: List[Anchor] = []
    i = 0
    while i < len(ts):
        j = bisect_right(ts, ts[i] + burst_window_s)
        if j - i >= min_burst:
            out.append(Anchor(ts=(ts[i] + ts[j - 1]) / 2.0, source="pose",
                              extras={"burst_rows": j - i}))
            i = j
        else:
            i += 1
    return out


def merge_anchors(*anchor_lists: Sequence[Anchor]) -> List[Anchor]:
    """Union all anchor sources, merging bursts closer than MERGE_GAP_S.

    On merge, bounce anchors win (they carry coordinates the features use);
    the merged anchor keeps the union of extras.
    """
    merged: List[Anchor] = []
    for a in sorted([a for lst in anchor_lists for a in lst], key=lambda a: a.ts):
        if merged and a.ts - merged[-1].ts < MERGE_GAP_S:
            keep, drop = merged[-1], a
            if drop.source == "bounce" and keep.source != "bounce":
                keep, drop = drop, keep
                merged[-1] = keep
            keep.extras.update(drop.extras)
            keep.extras["merged_sources"] = keep.extras.get("merged_sources", {keep.source}) | {drop.source}
            continue
        merged.append(a)
    return merged
