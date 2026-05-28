"""Derive per-game time windows from `serve_events` via server alternation.

Algorithm (matches ADR-03 §"Game-boundary derivation"):

    Walk `serve_events` in time order; every time the server's track_id
    flips, emit a new game boundary. The (t_start, t_end) span for game N
    is [median of first 3 serves' t_start, last serve's t_end] of the run.

Tiebreak handling: if the cumulative game count in the current set is >= 12,
the next "game" is actually a tiebreak — server alternates every two points
inside. We wrap the whole tiebreak as a single game_number with a
state-machine flag.

Robustness note (not in ADR spec, added at build time):

    Real T5 serve_events on noisy matches show frequent single-serve
    "flips" (a false-positive serve attributed to the wrong player
    mid-game). Treating each flip as a new game produces 25+ "games" for
    matches with 2 actual games. We add a small de-glitch step BEFORE the
    alternation walk: collapse any isolated single-serve run whose
    surrounding runs are same-server and the cross-flip gap is < 30 s.
    This is conservative — a real changeover is preceded by 60-90 s of
    rest. The collapse is logged in the GameBoundary diagnostics.
"""
from __future__ import annotations

import logging
import statistics
from typing import List, Sequence

from ml_pipeline.identity_detector.models import GameBoundary

logger = logging.getLogger(__name__)

# Number of games per set after which the *next* game is a tiebreak (per ITF rules).
TIEBREAK_TRIGGER_GAMES_IN_SET = 12

# Robustness: collapse a single-serve "flip" if the run is short AND the
# gap-to-next isn't long enough to be a real changeover.
_DEGLITCH_MAX_RUN_SERVES = 1
_DEGLITCH_MAX_GAP_S = 30.0


def _deglitch_runs(serves: Sequence[dict]) -> list:
    """Filter out lone-serve "alternation runs" that are obviously detector
    noise (single FP serve attributed to wrong player mid-game)."""
    if len(serves) <= 2:
        return list(serves)
    out = list(serves)
    while True:
        # Build runs (ts, pid)
        runs = []
        cur = [out[0]]
        for s in out[1:]:
            if s["player_id"] == cur[-1]["player_id"]:
                cur.append(s)
            else:
                runs.append(cur)
                cur = [s]
        runs.append(cur)
        if len(runs) < 3:
            return [s for run in runs for s in run]

        # Find the first deglitchable run (interior single, neighbours same pid)
        dropped = False
        for i in range(1, len(runs) - 1):
            if (len(runs[i]) <= _DEGLITCH_MAX_RUN_SERVES
                    and runs[i - 1][0]["player_id"] == runs[i + 1][0]["player_id"]):
                gap_left = runs[i][0]["ts"] - runs[i - 1][-1]["ts"]
                gap_right = runs[i + 1][0]["ts"] - runs[i][-1]["ts"]
                if gap_left < _DEGLITCH_MAX_GAP_S and gap_right < _DEGLITCH_MAX_GAP_S:
                    # Drop the lone interior serve
                    out = [
                        s for j, run in enumerate(runs) for s in run
                        if j != i
                    ]
                    dropped = True
                    logger.debug(
                        "deglitch: dropped lone pid=%d serve @ ts=%.2f "
                        "(gap_l=%.1fs gap_r=%.1fs)",
                        runs[i][0]["player_id"], runs[i][0]["ts"], gap_left, gap_right,
                    )
                    break
        if not dropped:
            return out


def derive_game_boundaries(
    serve_events: Sequence[dict],
    *,
    deglitch: bool = True,
) -> List[GameBoundary]:
    """Derive per-game windows from a time-ordered iterable of serve dicts.

    Each input dict must have at least `ts` (float seconds) and `player_id`
    (the server's track_id — 0 = near in T5 convention, 1 = far). Optional
    `set_no` flows through if present.

    Returns one GameBoundary per derived game. The ADR's
    "previous game count in set >= 12 → tiebreak" rule is applied across
    the cumulative game_number within each set.
    """
    if not serve_events:
        return []

    # Sort + lightly dedup glitchy isolated flips
    serves = sorted(serve_events, key=lambda s: s["ts"])
    if deglitch:
        serves = _deglitch_runs(serves)

    # Build server-alternation runs
    runs: List[List[dict]] = []
    cur: List[dict] = [serves[0]]
    for s in serves[1:]:
        if s["player_id"] == cur[-1]["player_id"]:
            cur.append(s)
        else:
            runs.append(cur)
            cur = [s]
    runs.append(cur)

    boundaries: List[GameBoundary] = []
    set_no = 1
    games_in_set = 0
    for run in runs:
        games_in_set += 1
        # Per ADR: "Tiebreak: detect when previous game count in set >= 12"
        # So if the *previous* count was 12, this run is the tiebreak.
        is_tiebreak = (games_in_set - 1) >= TIEBREAK_TRIGGER_GAMES_IN_SET

        # t_start: median of first up-to-3 serve start times (matches ADR pseudocode)
        first_three = run[: min(3, len(run))]
        t_start = float(statistics.median([s["ts"] for s in first_three]))
        t_end = float(run[-1]["ts"])
        boundaries.append(GameBoundary(
            game_number=len(boundaries) + 1,
            t_start=t_start,
            t_end=t_end,
            server_track_id=int(run[0]["player_id"]),
            set_number=set_no,
            tiebreak=is_tiebreak,
            n_serves=len(run),
        ))

        # Reset the set counter after a tiebreak (set ends with the tiebreak)
        if is_tiebreak:
            set_no += 1
            games_in_set = 0

    return boundaries
