"""Dataclasses for stroke detection.

StrokeEvent is the atomic unit — one detected stroke contact by one
player at one frame. Carries the velocity-signal diagnostics so a
downstream reviewer can audit which peaks fired and why.

`frame_idx` is the velocity-peak frame. `predicted_hit_frame` is the
peak-plus-offset frame (the model's best guess of true contact, since
wrist velocity peaks during the backswing-to-contact transition rather
than at contact itself — see ball_hit_pose.py probe findings).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StrokeEvent:
    """One detected stroke contact."""
    task_id: str
    frame_idx: int                # raw velocity-peak frame
    ts: float                     # ts at predicted_hit_frame (post-offset)
    predicted_hit_frame: int      # frame_idx + PEAK_TO_CONTACT_OFFSET
    player_id: int                # 0 = near, 1 = far (whichever wrist peaked)
    confidence: float             # 0..1

    peak_velocity_px_per_frame: float
    pre_peak_v: Optional[float] = None    # smoothed velocity at frame i-3
    post_peak_v: Optional[float] = None   # smoothed velocity at frame i+3
    decel_ratio: Optional[float] = None   # post_peak_v / peak_velocity

    # The complete hit fact — silver projects these verbatim (rule #1/#2).
    ball_hit_location_x: Optional[float] = None   # hitter court_x at the hit
    ball_hit_location_y: Optional[float] = None   # hitter court_y at the hit
    hitter_side_near: Optional[bool] = None       # resolved side (near = court_y > HALF_Y)
    volley: Optional[bool] = None                 # no bounce since the previous hit (out of the air)

    diagnostics: dict = field(default_factory=dict)

    def to_db_row(self) -> dict:
        return {
            "task_id": self.task_id,
            "frame_idx": self.frame_idx,
            "ts": self.ts,
            "predicted_hit_frame": self.predicted_hit_frame,
            "player_id": self.player_id,
            "confidence": self.confidence,
            "peak_velocity_px_per_frame": self.peak_velocity_px_per_frame,
            "pre_peak_v": self.pre_peak_v,
            "post_peak_v": self.post_peak_v,
            "decel_ratio": self.decel_ratio,
            "ball_hit_location_x": self.ball_hit_location_x,
            "ball_hit_location_y": self.ball_hit_location_y,
            "hitter_side_near": self.hitter_side_near,
            "volley": self.volley,
        }
