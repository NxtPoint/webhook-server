"""Per-candidate ball-trajectory discontinuities — the hit-model anchor set.

Pure functions over per-task arrays. All timestamps are SECONDS in the
task's sampled-frame space (frame_idx / sampled_fps) — convert via
timestamps only, never frame counts (feedback_t5_two_frame_spaces).

B1-critical: NO clustering beyond a ~0.1s same-event dedup. Bounce and hit
discontinuities are 0.3-0.7s neighbours; merging them was the single
biggest recall killer in the probe ladder (73/102 at gap=0.3s vs 96/102
at gap=0.2s with peaks). The classifier — not the candidate generator —
decides which discontinuities are hits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

import numpy as np

# Permissive thresholds — recall-first (B1: ang>45/speed>1 covers 96/102
# even WITH clustering damage; per-candidate raises the ceiling to ~99%).
ANGLE_MIN_DEG = 45.0
SPEED_MIN_PX = 1.0
DEDUP_GAP_S = 0.12  # same-event duplicates only (3 frames @25fps)


@dataclass
class HitCandidate:
    ts: float
    frame_idx: int
    angle_deg: float
    speed_in: float        # px/frame entering the discontinuity
    speed_out: float       # px/frame leaving it
    vy_in: float           # signed image-y velocity in (y down = +ve = toward near)
    vy_out: float
    x: float               # ball image x at the candidate
    y: float               # ball image y
    court_x: float | None = None
    court_y: float | None = None
    extras: dict = field(default_factory=dict)


def hit_candidates(ball_rows: Sequence[dict], fps: float,
                   angle_min: float = ANGLE_MIN_DEG,
                   speed_min: float = SPEED_MIN_PX) -> List[HitCandidate]:
    """Generate per-detection discontinuity candidates from ball_rows
    (dicts with frame_idx, x, y, court_x, court_y — bronze shape)."""
    rows = [r for r in ball_rows if r.get("x") is not None and r.get("y") is not None]
    if len(rows) < 3:
        return []
    fi = np.array([float(r["frame_idx"]) for r in rows])
    x = np.array([float(r["x"]) for r in rows])
    y = np.array([float(r["y"]) for r in rows])

    dt = np.diff(fi)
    vx = np.diff(x) / dt
    vy = np.diff(y) / dt
    n1 = np.hypot(vx[:-1], vy[:-1])
    n2 = np.hypot(vx[1:], vy[1:])
    dot = vx[:-1] * vx[1:] + vy[:-1] * vy[1:]
    cosang = np.clip(dot / np.maximum(n1 * n2, 1e-6), -1.0, 1.0)
    ang = np.degrees(np.arccos(cosang))

    out: List[HitCandidate] = []
    for i in np.flatnonzero((ang > angle_min) & (np.maximum(n1, n2) > speed_min)):
        j = i + 1  # interior detection index into rows
        r = rows[j]
        out.append(HitCandidate(
            ts=float(fi[j]) / fps,
            frame_idx=int(fi[j]),
            angle_deg=float(ang[i]),
            speed_in=float(n1[i]),
            speed_out=float(n2[i]),
            vy_in=float(vy[i]),
            vy_out=float(vy[i + 1]) if i + 1 < len(vy) else float(vy[i]),
            x=float(x[j]), y=float(y[j]),
            court_x=r.get("court_x"), court_y=r.get("court_y"),
        ))

    # Same-event dedup only: within DEDUP_GAP_S keep the highest angle.
    out.sort(key=lambda c: c.ts)
    deduped: List[HitCandidate] = []
    for c in out:
        if deduped and c.ts - deduped[-1].ts <= DEDUP_GAP_S:
            if c.angle_deg > deduped[-1].angle_deg:
                deduped[-1] = c
            continue
        deduped.append(c)
    return deduped


def attribute_player(c: HitCandidate) -> int:
    """WHO hit this ball — deterministic rule, not a model output.

    Image y grows DOWNWARD; the near player is at the bottom. A ball
    arriving at the hitter travels TOWARD them, so the incoming vertical
    direction names the hitter: vy_in > 0 (moving down-image, toward near)
    -> the NEAR player (pid 0) strikes it back; vy_in < 0 -> FAR (pid 1).
    Serves (no incoming ball) resolve by position instead: the toss is hit
    at the server's own end, so image y above the frame midline -> far.
    Validated against SA pid labels in dataset.build (trust-the-rule).
    """
    if abs(c.vy_in) > 0.5:
        return 0 if c.vy_in > 0 else 1
    # Near-static incoming ball (toss / first touch): fall back to position.
    return 1 if c.extras.get("frame_h", 1080.0) * 0.45 > c.y else 0
