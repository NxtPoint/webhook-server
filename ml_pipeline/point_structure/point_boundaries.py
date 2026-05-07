"""Point boundary detection.

Pure function — no DB, no I/O. Given:

    serves       : List[ServeEvent] (frame-indexed, sorted by ts)
    ball_events  : iterable of BallEvent (any object with a `.frame_idx`
                   and optional `.is_bounce`/court coords)
    fps          : video frame rate

returns a list of (start_frame, end_frame) tuples — one per detected
point, in chronological order.

# Algorithm

For each accepted serve s_i:
    start_frame := s_i.frame_idx
    end_frame   := the smaller of:
                     (a) s_{i+1}.frame_idx - 1, OR
                     (b) the frame at which we observe an idle gap of
                         IDLE_GAP_S seconds in valid bounce activity
                         AFTER the last seen bounce within the rally.
    The serve's own frame counts as the rally's "first event" for the
    purpose of the idle clock — so a serve immediately followed by
    >IDLE_GAP_S of no bounces ends at serve_frame + idle_window.

If no idle gap and no next serve, the point runs to the last frame seen
in `ball_events` (or the serve frame itself if no events follow).

# TODO (Phase 3 integration)

For now this treats EVERY bronze bounce as rally evidence. Once agent
BOUNCE's `validate_bounces()` lands (Phase 1), the caller should pass
the filtered (net-crossing) bounces here so racquet-bouncing pre-/
between-point clusters stop ending points prematurely. That swap is a
Phase-3 wiring job — this function deliberately doesn't import or
depend on `bounce_validity` to keep the phases independent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple


# Idle gap in seconds after which we consider the point over.
# 4.0s matches the practical "between points" window — long enough to
# survive a slow rally exchange, short enough to terminate before the
# next serve walk-up.
IDLE_GAP_S: float = 4.0


@dataclass
class BallEvent:
    """Minimal shape we need from a bronze bounce / ball-detection row.

    Callers that already have richer dataclasses (e.g. the BatchBounce
    rows from `bronze_export.py`) can pass them directly as long as the
    object exposes `.frame_idx` and (optionally) `.is_bounce`. The
    function uses duck-typing — any object with `.frame_idx` works.
    """
    frame_idx: int
    is_bounce: bool = True


@dataclass
class PointBoundary:
    """Internal richer shape — exported so the diag tool can describe
    the close-reason of each point."""
    start_frame: int
    end_frame: int
    start_serve_frame: int          # the originating serve (== start_frame)
    end_reason: str                  # "next_serve" | "idle_gap" | "stream_end"
    bounce_count: int                # number of bounces inside [start, end]


def _bounce_frames(ball_events: Iterable, fps: float) -> List[int]:
    """Project bronze events down to the sorted list of bounce frames.

    Treats anything without an explicit `is_bounce` attribute as a
    bounce (matches the `BallEvent` default). Phase-3 swap-point: pass
    pre-filtered (net-crossing) bounces in instead of all bronze rows.
    """
    out: List[int] = []
    for ev in ball_events:
        # Filter to bounces. Default True for plain dicts/objects without
        # the attribute (treat opaque events as rally evidence).
        is_b = getattr(ev, "is_bounce", True)
        if not is_b:
            continue
        try:
            out.append(int(ev.frame_idx))
        except AttributeError:
            # Allow dict-style access too
            out.append(int(ev["frame_idx"]))
    out.sort()
    return out


def _find_idle_end(
    start_frame: int,
    bounces: Sequence[int],
    idle_frames: int,
    hard_cap_frame: Optional[int],
) -> Tuple[int, str, int]:
    """Walk forward from start_frame; return the frame at which we hit
    an idle gap >= idle_frames.

    Returns (end_frame, end_reason, bounce_count_in_window).

    `hard_cap_frame` is the frame just before the next serve, or None if
    this is the last serve. We never extend past it.
    """
    # Find first bounce at or after start_frame
    import bisect
    i = bisect.bisect_left(bounces, start_frame)

    last_event_frame = start_frame  # the serve itself anchors the idle clock
    bounce_count = 0

    while i < len(bounces):
        b = bounces[i]
        # Stop if we've already exceeded the hard cap — but first check
        # whether the rally went idle BEFORE the next serve. If yes,
        # close on idle_gap (more accurate than blindly butting up to
        # the next serve frame).
        if hard_cap_frame is not None and b > hard_cap_frame:
            break
        gap = b - last_event_frame
        if gap >= idle_frames:
            # Idle gap detected. Point ends at last_event_frame + idle_frames
            # (the moment the "no activity" rule fires), capped to hard_cap.
            end = last_event_frame + idle_frames
            if hard_cap_frame is not None:
                end = min(end, hard_cap_frame)
            return end, "idle_gap", bounce_count
        last_event_frame = b
        bounce_count += 1
        i += 1

    # Loop exited either because we ran out of bounces or because the
    # next bounce belongs to the next serve. If the gap from the last
    # rally event to the hard cap exceeds idle_frames, close on idle.
    if hard_cap_frame is not None:
        if hard_cap_frame - last_event_frame >= idle_frames:
            end = last_event_frame + idle_frames
            return end, "idle_gap", bounce_count
        return hard_cap_frame, "next_serve", bounce_count
    end = last_event_frame + idle_frames
    return end, "stream_end", bounce_count


def detect_point_boundaries(
    serves,
    ball_events,
    fps: float,
    idle_gap_s: float = IDLE_GAP_S,
) -> List[Tuple[int, int]]:
    """Return [(start_frame, end_frame), ...] in chronological order.

    `serves` is a sequence of ServeEvent (or any object with `frame_idx`).
    The caller is responsible for passing only ACCEPTED serves —
    `find_serve_candidates` output that has already been gated.

    `ball_events` is any iterable of bounce/ball rows. See `_bounce_frames`
    for the duck-typing rules.

    `fps` is the video frame rate. Used only to convert `idle_gap_s` →
    frames. If your serves carry `ts` already in seconds and you'd
    rather work in ts, multiply outside.

    Returns the simple shape the spec asks for. For diagnostic detail
    (end_reason, bounce_count) call `detect_point_boundaries_detailed`.
    """
    detailed = detect_point_boundaries_detailed(
        serves=serves,
        ball_events=ball_events,
        fps=fps,
        idle_gap_s=idle_gap_s,
    )
    return [(p.start_frame, p.end_frame) for p in detailed]


def detect_point_boundaries_detailed(
    serves,
    ball_events,
    fps: float,
    idle_gap_s: float = IDLE_GAP_S,
) -> List[PointBoundary]:
    """Same as `detect_point_boundaries` but returns rich PointBoundary
    rows (used by the audit tool to characterise each close-reason)."""
    if fps <= 0:
        raise ValueError(f"fps must be > 0 (got {fps})")
    if idle_gap_s <= 0:
        raise ValueError(f"idle_gap_s must be > 0 (got {idle_gap_s})")

    # Sort serves by frame (the spec doesn't assume sorted input).
    serve_list = sorted(serves, key=lambda s: int(getattr(s, "frame_idx", 0)))
    if not serve_list:
        return []

    bounces = _bounce_frames(ball_events, fps)
    idle_frames = max(1, int(round(fps * idle_gap_s)))

    points: List[PointBoundary] = []
    for i, srv in enumerate(serve_list):
        start = int(srv.frame_idx)
        # Hard cap = frame just before the next serve. None for the last.
        if i + 1 < len(serve_list):
            next_serve = int(serve_list[i + 1].frame_idx)
            hard_cap = max(start, next_serve - 1)
        else:
            hard_cap = None

        end, reason, bcount = _find_idle_end(
            start_frame=start,
            bounces=bounces,
            idle_frames=idle_frames,
            hard_cap_frame=hard_cap,
        )
        # Defensive — never end before we start
        if end < start:
            end = start
        points.append(PointBoundary(
            start_frame=start,
            end_frame=end,
            start_serve_frame=start,
            end_reason=reason,
            bounce_count=bcount,
        ))

    return points
