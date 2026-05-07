"""Point boundary detection for T5 pipeline.

Phase 2 of the T5 north star ladder. Given accepted ServeEvents (from
the serve detector) and a stream of ball-bounce evidence, identify
[(start_frame, end_frame), ...] for each detected point.

A point STARTS at an accepted serve (the serve frame).
A point ENDS at the next accepted serve OR at an idle gap of N seconds
in valid bounce activity (whichever comes first).

This module is the input that Phase 3 (`build_silver_match_t5.py`) will
use to filter pre-/between-point activity out of `silver.point_detail`.
For now it is consumed only by the audit diag tool.
"""
from ml_pipeline.point_structure.point_boundaries import (
    BallEvent,
    PointBoundary,
    detect_point_boundaries,
)

__all__ = [
    "BallEvent",
    "PointBoundary",
    "detect_point_boundaries",
]
