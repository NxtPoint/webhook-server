"""Rally-state machine — gates serve candidates to legitimate pre-point
moments.

Per the Kijak 2003 / TAL4Tennis 2025 / Springer 2024 literature, a
tennis match decomposes into {pre-point, serve, rally, between-points}.
A serve NEVER fires mid-rally — if you detected what looks like a serve
pose mid-rally, it's a smash or a reach shot, not a serve.

The cleanest, cheapest signal for "in rally vs between points" is ball
bounce activity:
  - IN_RALLY: a bounce within the previous 3 seconds
  - PRE_POINT / BETWEEN_POINTS: 3+ seconds since last bounce

The difference between PRE_POINT and BETWEEN_POINTS is only meaningful
for reporting — for serve gating we just need "not IN_RALLY".

This module is intentionally simple. If false-positives persist in
validation we can upgrade to a proper Viterbi-decoded HMM over a
richer feature stream (ball speed, player motion, crowd audio),
but the simple version handles the 24-serve reference match cleanly.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from enum import Enum
from typing import List, Sequence


class RallyState(str, Enum):
    UNKNOWN = "unknown"
    PRE_POINT = "pre_point"
    BETWEEN_POINTS = "between_points"
    IN_RALLY = "in_rally"


@dataclass
class RallyStateMachine:
    """Given a list of bounce timestamps, answer per-ts state queries.

    Bounce timestamps are the ONLY input — keeps this pure and testable.
    In production the list comes from ml_analysis.ball_detections WHERE
    is_bounce=TRUE, ordered by frame_idx/fps. In offline mode it can be
    supplied from any source.

    idle_threshold_s: seconds of no bounces to leave IN_RALLY.
    match_start_s: timestamps before this are PRE_POINT by convention
        (the first point of the match hasn't happened yet).
    """
    bounce_ts: Sequence[float]
    idle_threshold_s: float = 3.0
    match_start_s: float = 0.0

    def __post_init__(self):
        self._sorted = sorted(self.bounce_ts)

    def state_at(self, ts: float) -> RallyState:
        """Rally state at this ts. Cheap O(log n) lookup."""
        # Find the most recent bounce at or before ts
        idx = bisect.bisect_right(self._sorted, ts) - 1
        if idx < 0:
            # No bounces yet — pre-first-point if past match start.
            return RallyState.PRE_POINT
        time_since_bounce = ts - self._sorted[idx]
        if time_since_bounce <= self.idle_threshold_s:
            return RallyState.IN_RALLY
        # Enough idle time to call this between-points. Flavour as
        # PRE_POINT if a bounce follows within ~5s (about to serve),
        # else BETWEEN_POINTS. Callers only care "not IN_RALLY".
        next_idx = idx + 1
        if next_idx < len(self._sorted):
            gap_to_next = self._sorted[next_idx] - ts
            if gap_to_next <= 5.0:
                return RallyState.PRE_POINT
        return RallyState.BETWEEN_POINTS

    def allow_serve_at(self, ts: float) -> bool:
        """Convenience: is this a legitimate moment for a serve?"""
        return self.state_at(ts) != RallyState.IN_RALLY

    def time_since_last_bounce(self, ts: float) -> float:
        """Useful for confidence weighting — longer idle = more confident it's a serve."""
        idx = bisect.bisect_right(self._sorted, ts) - 1
        if idx < 0:
            return ts - self.match_start_s
        return ts - self._sorted[idx]

    def time_to_next_bounce(self, ts: float) -> float:
        """Useful for confirming a pose-detected serve produced a bounce soon after."""
        idx = bisect.bisect_left(self._sorted, ts)
        if idx >= len(self._sorted):
            return float("inf")
        return self._sorted[idx] - ts


def build_from_db(conn, task_id: str, fps: float) -> RallyStateMachine:
    """Factory — load bounce timestamps for a task and return a ready-to-query
    state machine."""
    from sqlalchemy import text as sql_text
    rows = conn.execute(sql_text("""
        SELECT frame_idx FROM ml_analysis.ball_detections
        WHERE job_id = :tid AND is_bounce = TRUE
        ORDER BY frame_idx
    """), {"tid": task_id}).scalars().all()
    return RallyStateMachine(bounce_ts=[r / fps for r in rows])
