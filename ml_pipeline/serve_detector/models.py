"""Dataclasses for serve detection.

ServeEvent is the atomic unit — one detected serve by one player at one
frame. Carries confidence + provenance so downstream silver/gold can
distinguish pose-triggered serves (high-confidence, near player) from
bounce-triggered serves (far player) from combined-signal serves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SignalSource(str, Enum):
    """Which signal(s) triggered this serve event."""
    POSE_ONLY = "pose_only"          # pose signature, ball not seen
    POSE_AND_BALL = "pose_and_ball"  # pose + rising-ball toss confirmation
    BOUNCE_ONLY = "bounce_only"      # ball bounce in service box, no pose (far player)
    POSE_AND_BOUNCE = "pose_and_bounce"  # pose + bounce confirmed

    @property
    def has_pose(self) -> bool:
        return self in (SignalSource.POSE_ONLY, SignalSource.POSE_AND_BALL,
                        SignalSource.POSE_AND_BOUNCE)

    @property
    def has_bounce(self) -> bool:
        return self in (SignalSource.BOUNCE_ONLY, SignalSource.POSE_AND_BOUNCE)


@dataclass
class ServeEvent:
    """One detected serve. Frame-indexed at the estimated CONTACT moment
    (not the bounce). Silver's ball_hit_s is derived from this."""
    task_id: str
    frame_idx: int
    ts: float
    player_id: int                    # 0 = near-camera, 1 = far
    source: SignalSource
    confidence: float                 # 0..1, fusion of available signals

    # Pose-signal detail (None if source is BOUNCE_ONLY)
    pose_score: Optional[float] = None          # 0..3 per pose_signal rules
    trophy_peak_frame: Optional[int] = None     # frame where dominant wrist is highest

    # Ball-signal detail
    has_ball_toss: bool = False
    bounce_frame: Optional[int] = None          # if linked to a subsequent bounce
    bounce_court_x: Optional[float] = None
    bounce_court_y: Optional[float] = None

    # Rally-state context
    rally_state: str = "unknown"                # "pre_point" | "between_points" | "unknown"

    # Raw hitter court position at contact (for silver's hit_x/hit_y fields)
    hitter_court_x: Optional[float] = None
    hitter_court_y: Optional[float] = None

    # Raw pixel bbox at contact frame (for visual debugging / diag)
    hitter_bbox: Optional[tuple] = None         # (x1, y1, x2, y2)

    diagnostics: dict = field(default_factory=dict)

    def to_db_row(self) -> dict:
        """Convert to a dict matching ml_analysis.serve_events schema."""
        return {
            "task_id": self.task_id,
            "frame_idx": self.frame_idx,
            "ts": self.ts,
            "player_id": self.player_id,
            "source": self.source.value,
            "confidence": self.confidence,
            "pose_score": self.pose_score,
            "trophy_peak_frame": self.trophy_peak_frame,
            "has_ball_toss": self.has_ball_toss,
            "bounce_frame": self.bounce_frame,
            "bounce_court_x": self.bounce_court_x,
            "bounce_court_y": self.bounce_court_y,
            "rally_state": self.rally_state,
            "hitter_court_x": self.hitter_court_x,
            "hitter_court_y": self.hitter_court_y,
        }
