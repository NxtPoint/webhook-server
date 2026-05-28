"""Pre-gates for bounce candidates — cheap, decisive, applied BEFORE
the CNN ever sees a candidate.

Per ADR-01 §"Pre-gates": today's 84% FP rate is dominated by three
specific FP classes (racket hits, net-cord clips, out-of-rally noise).
Filtering them with hard gates before scoring converts the model's task
from "find rare events in noise" to "score candidates that survived the
gate" — same shape as serve_detector's pre-clustering.

The gates use only court-space coordinates + bronze-already-computed
fields; no model inference required. Each returns True if the candidate
should PASS (i.e. continue to scoring); False if it should be rejected
outright.

Constants are kept here (not in ml_pipeline.config) so this module
remains independently testable with a hand-built fake bronze row. Court
half-length matches the SPORT_CONFIG_SINGLES values referenced in
serve_detector/detector.py.
"""
from __future__ import annotations

import logging
import math
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# Court constants — must match ml_pipeline.config SPORT_CONFIG_SINGLES.
COURT_LENGTH_M = 23.77
HALF_Y = COURT_LENGTH_M / 2.0
NET_Y = HALF_Y                                # net is at court_y == 11.885

# Gate thresholds — sourced from ADR-01 §"Pre-gates" table verbatim.
WRIST_PROXIMITY_M = 0.6                       # racket-hit rejection
NET_LINE_PROXIMITY_M = 1.0                    # net-cord rejection (XY proximity)
# z-trajectory (above-net) is a future feature — Phase 5+ bronze adds
# ball_z; until then we approximate "above net" via court_y proximity +
# trajectory analysis upstream. The gate signature accepts an explicit
# above_net_flag so callers can wire it in once available; v0 keeps
# False as default and the gate then only fires on planar net proximity.


def passes_wrist_proximity_gate(
    candidate_xy: tuple[Optional[float], Optional[float]],
    wrist_positions: Sequence[tuple[Optional[float], Optional[float]]],
    threshold_m: float = WRIST_PROXIMITY_M,
) -> bool:
    """Reject if the candidate point is within `threshold_m` of any wrist.

    candidate_xy:  (court_x, court_y) of the bounce candidate, or (None, None).
    wrist_positions: iterable of (court_x, court_y) for every wrist of every
        player at the candidate's frame (typically 4 entries: both wrists
        of both players). Missing entries (None, None) are skipped.

    Returns True (PASS) if no wrist is within threshold OR if either coord
    is missing for the candidate (no way to evaluate — let scoring decide).
    """
    cx, cy = candidate_xy
    if cx is None or cy is None:
        return True
    for wx, wy in wrist_positions:
        if wx is None or wy is None:
            continue
        if (cx - wx) ** 2 + (cy - wy) ** 2 <= threshold_m ** 2:
            return False
    return True


def passes_net_line_gate(
    candidate_xy: tuple[Optional[float], Optional[float]],
    above_net_flag: bool = False,
    threshold_m: float = NET_LINE_PROXIMITY_M,
) -> bool:
    """Reject if the candidate is within `threshold_m` of the net line
    in court-y AND is above the net (z trajectory).

    above_net_flag: True if upstream ball-trajectory analysis says the
        ball is above-net at the candidate frame. v0 defaults False —
        the planar gate alone won't trigger and net-cord events still
        need the model to score them down. The flag is wired in once
        bronze.ball_detections carries a ball_z (Phase 5+).

    Returns True (PASS) if the candidate is clearly below-net height
    OR is well clear of the net line in court-y.
    """
    cx, cy = candidate_xy
    if cy is None:
        return True
    near_net = abs(cy - NET_Y) <= threshold_m
    if near_net and above_net_flag:
        return False
    return True


def passes_rally_state_gate(rally_state: Optional[str]) -> bool:
    """Reject if the rally state is neither `in_rally` nor `serve_in_flight`.

    rally_state values come from `ml_pipeline.serve_detector.rally_state`
    via `serve_events.rally_state` — the same enum the serve_detector
    persists. Accepted values that PASS:
      - 'in_rally'           — live point
      - 'serve_in_flight'    — ball travelling between serve hit and bounce
                               (emitted by serve_detector once the rally
                                state machine learns it; v0 doesn't yet
                                produce this value but we whitelist it
                                forward-compatibly)

    `pre_point`, `between_points`, `unknown` → REJECT.
    """
    if rally_state is None:
        # Unknown is a reject — conservative. Trained model can reconsider
        # later if recall suffers.
        return False
    return rally_state.lower() in ("in_rally", "serve_in_flight")


def apply_pre_gates(
    candidate_xy: tuple[Optional[float], Optional[float]],
    wrist_positions: Sequence[tuple[Optional[float], Optional[float]]],
    rally_state: Optional[str],
    above_net_flag: bool = False,
) -> tuple[bool, Optional[str]]:
    """Run all three gates. Returns (passed, rejection_reason_or_None).

    Rejection reason is the first failing gate's name — useful for the
    bench's per-task breakdown ("how many candidates did each gate
    eliminate?") which informs threshold tuning.
    """
    if not passes_rally_state_gate(rally_state):
        return (False, "rally_state")
    if not passes_net_line_gate(candidate_xy, above_net_flag=above_net_flag):
        return (False, "net_line")
    if not passes_wrist_proximity_gate(candidate_xy, wrist_positions):
        return (False, "wrist_proximity")
    return (True, None)
