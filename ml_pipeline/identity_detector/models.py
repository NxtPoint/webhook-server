"""Dataclasses for identity_detector v1.

Two atomic units:

  - `GameBoundary` — one game's time window plus the server's track_id,
    derived from `serve_events` via the server-alternation invariant.

  - `IdentitySegment` — one game's (player_a_side, player_b_side, confidence,
    source) record, ready to persist into `ml_analysis.player_identity_segments`.

Side is always 'near' or 'far' (string), matching the schema column type
and the existing T5 silver derivation that uses `court_y > HALF_Y` for near.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Side(str, Enum):
    NEAR = "near"
    FAR = "far"


class IdentitySource(str, Enum):
    """`source` column values written to ml_analysis.player_identity_segments."""
    RULE_V1 = "rule_v1"                       # rule fired cleanly; high conf
    RULE_V1_ANOMALY = "rule_v1_anomaly"       # detected swap but not expected (tracker swap)
    RULE_V1_TERMINATED = "rule_v1_terminated" # gap closed early; conf medium
    RULE_V1_MEDICAL_BREAK = "rule_v1_medical_break"  # expected gap missed; assume swap
    RULE_V1_INITIAL = "rule_v1_initial"       # game 1 — set by upload-form mapping, no rule
    NEEDS_REVIEW = "needs_review"             # conf < 0.5; surface in dashboard


@dataclass
class GameBoundary:
    """One game window from server-alternation derivation.

    `tiebreak` flips True for the synthetic single-game-number tiebreak
    boundary (server alternates every two points inside).
    """
    game_number: int
    t_start: float
    t_end: float
    server_track_id: int
    set_number: int = 1
    tiebreak: bool = False
    n_serves: int = 0


@dataclass
class IdentitySegment:
    """One row destined for ml_analysis.player_identity_segments.

    `job_id` matches `ml_analysis.serve_events.task_id` (UUID, written as
    a UUID/string into the segments table)."""
    job_id: str
    game_number: int
    player_a_side: Side
    player_b_side: Side
    confidence: float
    source: IdentitySource
    diagnostics: dict = field(default_factory=dict)

    def to_db_row(self) -> dict:
        return {
            "job_id": self.job_id,
            "game_number": self.game_number,
            "player_a_side": self.player_a_side.value,
            "player_b_side": self.player_b_side.value,
            "confidence": float(self.confidence),
            "source": self.source.value,
        }
