"""Per-game changeover detection — the per-game flip rule from ADR-03 §"v1 algorithm".

Inputs (per inter-game gap):
  - `pose_rows` per track_id: time-ordered (frame_idx, ts, court_y) tuples
  - gap window [t_end_game_N, t_start_game_N+1]
  - expected changeover flag (ITF: True for game_no in {1,3,5,7,9,11} plus
    every 6 points inside a tiebreak)

Output:
  - `ChangeoverDecision(swapped: bool, confidence: float, source: IdentitySource,
                        diagnostics: dict)`

Decision matrix (ADR §"Decision matrix"):
  - rule fires cleanly (detected swap == expected):      confidence = 0.95
  - expected but not detected, gap > 90s:                assume swap (medical),     conf = 0.6
  - expected but not detected, gap <= 90s:               assume no swap (towel),    conf = 0.5
  - not expected but detected:                           tracker swap anomaly,      conf = 0.4

`court_y` semantics match the rest of the pipeline (`build_silver_v2`,
`serve_detector`): COURT_LENGTH_M = 23.77; the NET is at HALF_Y = 11.885;
court_y > HALF_Y = near baseline; court_y < HALF_Y = far baseline.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ml_pipeline.identity_detector.models import IdentitySource, Side

logger = logging.getLogger(__name__)

# Must match serve_detector / build_silver_v2 SPORT_CONFIG
COURT_LENGTH_M = 23.77
HALF_Y = COURT_LENGTH_M / 2.0  # 11.885

# Window size on each side of the gap to median-filter court_y over.
_SIDE_SAMPLE_S = 5.0

# Long-gap threshold for the "expected-but-not-detected" branch
# (medical/long break) — ADR §"Decision matrix".
LONG_GAP_S = 90.0


@dataclass
class ChangeoverDecision:
    """Result of analysing one inter-game gap."""
    swapped: bool
    confidence: float
    source: IdentitySource
    diagnostics: Dict = field(default_factory=dict)


def _median_court_y(
    pose_rows: Sequence[Tuple[float, Optional[float]]],
    t_lo: float,
    t_hi: float,
) -> Optional[float]:
    """Median court_y over (ts in [t_lo, t_hi], court_y not None)."""
    ys = [y for (ts, y) in pose_rows if y is not None and t_lo <= ts <= t_hi]
    if not ys:
        return None
    return float(statistics.median(ys))


def _side_of(court_y: Optional[float]) -> Optional[Side]:
    if court_y is None:
        return None
    return Side.NEAR if court_y > HALF_Y else Side.FAR


def is_expected_changeover(game_number: int) -> bool:
    """ITF rule: players change sides after games 1, 3, 5, 7, 9, 11, ...
    i.e. after every odd-numbered game. "After game N" means the changeover
    falls between game N and game N+1, so the receiver of that boundary is
    game (N+1) — we check whether the *transition into* this game flips.
    Concretely: changeover happens BEFORE game 2, 4, 6, 8, 10, 12 — i.e.
    before every even-numbered game.

    This matches the ADR pseudocode:
        EXPECTED_CHANGEOVER per ITF: True if game_no in {1,3,5,7,9,11}
                                     AND every 6 points in tiebreak
    where game_no there is the index of the game JUST ENDED. We accept
    the boundary index = (game_just_ended) which equals (next_game - 1).
    """
    # game_number here is the index of the game JUST ENDED (so a flip
    # between games 1 and 2 carries game_number=1).
    return game_number % 2 == 1


def detect_changeover(
    pose_rows_track_a: Sequence[Tuple[float, Optional[float]]],
    pose_rows_track_b: Sequence[Tuple[float, Optional[float]]],
    *,
    gap_start_s: float,
    gap_end_s: float,
    expected: bool,
) -> ChangeoverDecision:
    """Apply the v1 decision matrix to one inter-game gap.

    pose_rows_track_*: sequences of (ts, court_y). Two tracks (the YOLOv8
    tracker's 0/1 — caller passes them in whatever order; the detection is
    invariant to which is which because we require BOTH to cross).
    """
    side_a_before = _side_of(_median_court_y(
        pose_rows_track_a, gap_start_s - _SIDE_SAMPLE_S, gap_start_s))
    side_a_after = _side_of(_median_court_y(
        pose_rows_track_a, gap_end_s, gap_end_s + _SIDE_SAMPLE_S))
    side_b_before = _side_of(_median_court_y(
        pose_rows_track_b, gap_start_s - _SIDE_SAMPLE_S, gap_start_s))
    side_b_after = _side_of(_median_court_y(
        pose_rows_track_b, gap_end_s, gap_end_s + _SIDE_SAMPLE_S))

    gap_duration_s = gap_end_s - gap_start_s

    # Dual-cross check: BOTH tracks must change side.
    crossed_a = (side_a_before is not None and side_a_after is not None
                 and side_a_before != side_a_after)
    crossed_b = (side_b_before is not None and side_b_after is not None
                 and side_b_before != side_b_after)
    detected = crossed_a and crossed_b

    diagnostics = {
        "gap_duration_s": gap_duration_s,
        "side_a_before": side_a_before.value if side_a_before else None,
        "side_a_after": side_a_after.value if side_a_after else None,
        "side_b_before": side_b_before.value if side_b_before else None,
        "side_b_after": side_b_after.value if side_b_after else None,
        "expected": expected,
        "detected": detected,
    }

    # ADR decision matrix
    if detected and expected:
        return ChangeoverDecision(True, 0.95, IdentitySource.RULE_V1, diagnostics)
    if expected and not detected:
        if gap_duration_s > LONG_GAP_S:
            # Assume the changeover did happen but pose data was sparse
            return ChangeoverDecision(
                True, 0.60, IdentitySource.RULE_V1_MEDICAL_BREAK, diagnostics)
        # Quick changeover (towel only — players may not have swapped)
        return ChangeoverDecision(
            False, 0.50, IdentitySource.RULE_V1_TERMINATED, diagnostics)
    if detected and not expected:
        # Tracker ID swap mid-game; players didn't actually swap. The rule
        # records that the side *did* change but flags it as anomalous so
        # downstream silver knows to treat it cautiously.
        return ChangeoverDecision(
            True, 0.40, IdentitySource.RULE_V1_ANOMALY, diagnostics)
    # Not expected and not detected: stable case.
    return ChangeoverDecision(False, 0.95, IdentitySource.RULE_V1, diagnostics)
