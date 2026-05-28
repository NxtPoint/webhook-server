"""Per-game changeover detection — the per-game flip rule from ADR-03 §"v1 algorithm".

Inputs (per inter-game gap):
  - `pose_rows` per track_id: time-ordered (frame_idx, ts, court_y) tuples
  - gap window [t_end_game_N, t_start_game_N+1]
  - expected changeover flag (ITF: True for game_no in {1,3,5,7,9,11} plus
    every 6 points inside a tiebreak)

Output:
  - `ChangeoverDecision(swapped: bool, confidence: float, source: IdentitySource,
                        diagnostics: dict)`

Decision matrix v2 (2026-05-28 — TRACKER-BINDING AWARE; see ADR-03 §"v1 finding"):
  - expected AND detected (visual + ITF agree):          conf = 0.95
  - expected, not detected, gap <= 90s:                  trust ITF (swap),          conf = 0.85
  - expected, not detected, gap >  90s:                  trust ITF + flag medical,  conf = 0.80
  - not expected AND detected (real tracker ID swap):    flag anomaly,              conf = 0.40
  - not expected, not detected:                          stable,                    conf = 0.95

WHY DEFAULTS FLIPPED (2026-05-28): the YOLOv8 tracker pre-binds pid=0=near,
pid=1=far permanently, so `detected` fires 0% even when players DID swap.
Tennis rules ARE deterministic — players change ends after every odd game
per ITF. So the source of truth is the ITF rule; the visual dual-cross is
the CHECK (corroboration when present), not the source. Previously the
"expected but not detected" branch defaulted to "no swap" (conf 0.5) which
was wrong for every ITF-expected boundary on a tracker-bound system — bench
fired 0% on 3 fixtures. Defaulting to "trust ITF, swap=True, conf 0.85"
flips the bench to ~95% per-game identity correctness.

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

    # Decision matrix v2 (2026-05-28) — tracker-binding-aware; see module
    # docstring for why the defaults flipped. ITF rule is the source of
    # truth; visual dual-cross is the check.
    if expected:
        if detected:
            # Both signals agree — rare under tracker binding but the
            # strongest case. Max confidence.
            return ChangeoverDecision(True, 0.95, IdentitySource.RULE_V1, diagnostics)
        if gap_duration_s > LONG_GAP_S:
            # Medical / long-break case. ITF still mandates the swap on
            # resumption; the long gap is one extra signal something
            # non-normal happened so confidence is a touch lower.
            return ChangeoverDecision(
                True, 0.80, IdentitySource.RULE_V1_MEDICAL_BREAK, diagnostics)
        # Normal towel-break, tracker-binding-hidden case: trust ITF.
        # 0.85 stays above the 0.5 silver-fallback threshold so silver
        # uses A/B labels (not the near/far fallback) for this game.
        return ChangeoverDecision(True, 0.85, IdentitySource.RULE_V1, diagnostics)
    # Not expected:
    if detected:
        # Rare under tracker binding, but a real ID swap mid-rally would
        # land here. Anomaly — flag for silver to treat cautiously.
        return ChangeoverDecision(
            True, 0.40, IdentitySource.RULE_V1_ANOMALY, diagnostics)
    # Stable case — no expected swap, no detected swap.
    return ChangeoverDecision(False, 0.95, IdentitySource.RULE_V1, diagnostics)
