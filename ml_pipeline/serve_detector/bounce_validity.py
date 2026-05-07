"""Bounce validity filter — drop phantom bounces that aren't rally evidence.

Tomo's bounce-validity rule (May 7, see project_t5_may07_phantom_bounces):

    A bounce is valid rally evidence ONLY if it crosses the net (i.e. the
    ball travelled from one side of the net to the other between two
    consecutive bounces) OR the bounce is a net-hit (ball hit the net and
    stopped).

    Same-side multi-bounce sequences are pre-serve racquet-bouncing or
    TrackNet noise on near-baseline court features — they must NOT advance
    the rally state machine.

This module is a pure function that filters a list of bounce dicts to
just those that satisfy the rule. The two consumers in production are:

    - `ml_pipeline/roi_extractors/pose.py::extract_far_pose` — builds the
      `RallyStateMachine` from in-memory `result.ball_detections` to gate
      ROI pose extraction at the source. With the validity filter applied
      here, mid-rally trophy poses during phantom-cluster windows aren't
      blocked.

    - `ml_pipeline/serve_detector/rally_state.py::build_from_db` — loads
      bounces from `ml_analysis.ball_detections WHERE is_bounce=TRUE`
      to build the rally state machine consumed by the FAR-bounce serve
      detector and the augmented rally check on the NEAR pose path.

Both consumers share THIS filter (no third pathway). Bench locked at
20/24 on a798eff0 — the filter must not regress that.

HALF_Y = 11.885 m (singles court midline). Defined in
build_silver_v2.py::SPORT_CONFIG["tennis_singles"]["half_y"] and
used identically in serve_detector/detector.py and as
COURT_LENGTH_M / 2.0 throughout. We hardcode the same value here
to avoid a circular import on the silver builder, and to match the
existing constant used by both consumer modules (rally_state.py
inherits HALF_Y from detector.py).
"""
from __future__ import annotations

from typing import Iterable, List

# Singles court length: 23.77 m. Midline = 11.885 m. Same value as
# `serve_detector.detector.HALF_Y` and `build_silver_v2.SPORT_CONFIG`.
HALF_Y = 11.885


def _side(court_y) -> int:
    """Return -1 if y is on the FAR side of the net, +1 if on the NEAR side,
    0 if exactly on the line. None court_y returns 0 (ambiguous)."""
    if court_y is None:
        return 0
    if court_y < HALF_Y:
        return -1
    if court_y > HALF_Y:
        return 1
    return 0


def validate_bounces(bounces: Iterable[dict]) -> List[dict]:
    """Filter `bounces` to just those that satisfy the cross-net validity rule.

    Args:
        bounces: iterable of dicts. Each dict needs at minimum a `frame_idx`
            field and optionally `court_y` (metres) + `is_net_hit` (bool).
            Any extra fields on the dict are preserved unchanged on the
            output list.

    Returns:
        A new list (sorted by frame_idx) containing only bounces that
        constitute legitimate rally evidence. The filter rule:

          * Net-hits (`is_net_hit=True`) are always kept.
          * A bounce with `court_y` on the opposite side of the net from
            an adjacent neighbour (previous or next bounce in time order)
            is kept — these two bounces together describe a ball that
            crossed the net.
          * A bounce whose `court_y` is None is kept — we can't verify
            the crossing rule without a court projection, and dropping
            unprojected bounces would silently lose real signal in older
            data. The downstream rally state machine already tolerates
            sparse bounces.
          * A bounce with no neighbours at all (the only bounce in the
            input) is kept — a single isolated bounce is unambiguously
            real serve activity, not a phantom cluster.
          * All other bounces (same side as both neighbours, with
            projected court_y) are dropped as phantom rally evidence.

    The filter is order-stable on input that's already sorted by frame_idx
    and is O(n) in the bounce count.
    """
    sorted_bounces = sorted(bounces, key=lambda b: b.get("frame_idx", 0))
    n = len(sorted_bounces)
    if n == 0:
        return []
    if n == 1:
        # Lone bounce — nothing to compare against. Keep it; it's not
        # a phantom cluster by definition.
        return list(sorted_bounces)

    out: List[dict] = []
    sides = [_side(b.get("court_y")) for b in sorted_bounces]
    for i, b in enumerate(sorted_bounces):
        if b.get("is_net_hit") is True:
            out.append(b)
            continue

        s_i = sides[i]
        if s_i == 0:
            # court_y missing or exactly on the line — can't validate,
            # keep conservatively. Matches pre-filter behaviour for
            # unprojected bounces.
            out.append(b)
            continue

        # Look at the immediate previous and next bounces in time order.
        # If either neighbour is on the OPPOSITE side of the net, the
        # pair `(prev, b)` or `(b, next)` describes a ball that crossed
        # the net — this bounce is valid rally evidence.
        prev_side = sides[i - 1] if i > 0 else 0
        next_side = sides[i + 1] if i + 1 < n else 0
        if prev_side == -s_i or next_side == -s_i:
            out.append(b)
            continue
        # Same side as every neighbour with a known court_y → drop.

    return out
