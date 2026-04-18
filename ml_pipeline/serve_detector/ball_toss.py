"""Optional ball-toss confirmation signal.

A real serve begins with a ball toss: the ball rises from the server's
hand to above their head, reverses, and is struck. If TrackNet catches
any of this motion, we can confirm the pose-detected serve with higher
confidence.

This signal is INTENTIONALLY WEAK and OPTIONAL:
  - TrackNet recall is only ~13% of frames (lower still near the player
    where the ball is large and may saturate the heatmap).
  - Near-player serves particularly suffer because the ball is
    eclipsed by the server's body on many frames.
  - Far-player serves would rely on far-half ball tracking, which is
    the broken branch that motivated this whole rewrite.

Usage pattern: pose_signal finds a strong candidate, then ball_toss
either BOOSTS confidence (if a rising-ball signature is visible) or
leaves it alone (if the ball isn't tracked around that moment). A
failing ball_toss check NEVER rejects a pose-detected serve.

Rising-ball detection criteria (in a ±1s window around candidate
contact frame):
  - At least 3 ball detections near the player's pixel-x (within N px)
  - Ball pixel-y is monotonically decreasing (rising) for the majority
    of those samples, spanning at least 40 px vertically
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class BallTossEvidence:
    has_rising_ball: bool
    samples: int
    y_drop_px: float            # how far the ball rose (pixels)
    nearest_x_delta_px: float   # how close the ball stayed to the server in x


def detect_ball_toss(
    ball_rows: Sequence[dict],
    player_bbox: tuple,
    contact_frame: int,
    fps: float,
    *,
    pre_window_s: float = 1.2,
    post_window_s: float = 0.2,
    x_tolerance_px: float = 200.0,
    min_samples: int = 3,
    min_rise_px: float = 40.0,
) -> BallTossEvidence:
    """Look for a rising-ball signature in the window around contact_frame.

    ball_rows: iterable of {frame_idx, x, y} dicts from
        ml_analysis.ball_detections (or equivalent local source)
    player_bbox: (x1, y1, x2, y2) pixel bbox of the player AT contact
    """
    if not ball_rows or player_bbox is None:
        return BallTossEvidence(has_rising_ball=False, samples=0,
                                y_drop_px=0.0, nearest_x_delta_px=0.0)

    px1, _, px2, _ = player_bbox
    player_cx = (px1 + px2) / 2.0

    lo = contact_frame - int(round(fps * pre_window_s))
    hi = contact_frame + int(round(fps * post_window_s))

    nearby = []
    for b in ball_rows:
        fi = b.get("frame_idx")
        if fi is None or fi < lo or fi > hi:
            continue
        bx = b.get("x")
        by = b.get("y")
        if bx is None or by is None:
            continue
        if abs(bx - player_cx) > x_tolerance_px:
            continue
        nearby.append((fi, bx, by))

    nearby.sort()
    if len(nearby) < min_samples:
        return BallTossEvidence(has_rising_ball=False, samples=len(nearby),
                                y_drop_px=0.0,
                                nearest_x_delta_px=0.0 if not nearby
                                else min(abs(bx - player_cx)
                                         for _, bx, _ in nearby))

    # Rising means pixel-y goes DOWN (image origin top-left). We want the
    # earliest samples to have LARGER y than later samples — a majority of
    # pairwise comparisons support decreasing y → rising ball.
    ys = [y for _, _, y in nearby]
    y_peak_early = max(ys[: len(ys) // 2 + 1])  # max y in first half (lowest point)
    y_peak_late = min(ys[len(ys) // 2:])         # min y in second half (highest point)
    y_drop = y_peak_early - y_peak_late
    has_rise = y_drop >= min_rise_px

    return BallTossEvidence(
        has_rising_ball=has_rise,
        samples=len(nearby),
        y_drop_px=y_drop,
        nearest_x_delta_px=min(abs(bx - player_cx) for _, bx, _ in nearby),
    )
