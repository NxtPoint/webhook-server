"""Dataclasses for bounce detection.

BounceEvent is the atomic unit — one detected ground bounce at one frame.
Mirrors `ml_pipeline.serve_detector.models.ServeEvent` shape so downstream
silver consumers can rely on the same patterns (frame_idx + ts + confidence
+ source provenance).

The `in_point` field is a MODEL OUTPUT per ADR-01 §Q3-B (Tomo's stated
preference) — the bounce model itself decides whether a bounce occurred
during a live point, rather than letting silver gate after the fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SignalSource(str, Enum):
    """Provenance string written to ml_analysis.ball_bounces.source.

    Distinguishes:
      - the trained model (v1, v2 ...) — once weights land,
      - the stage-1 stopgap that emits zeros without trained weights,
      - the legacy raw-TrackNet-derived signal (for backfill / diag).
    """
    BOUNCE_DETECTOR_V1 = "bounce_detector_v1"
    BOUNCE_DETECTOR_V2 = "bounce_detector_v2"  # the deployed weights (v2_7match.pt)
    STOPGAP_UNTRAINED = "STOPGAP-untrained"   # v0 — no weights loaded
    LEGACY_RAW = "legacy_raw"                  # raw ball_detections.is_bounce passthrough


class PlayerSide(str, Enum):
    """Which side of the net the bounce landed on (or 'net_cord' for
    the in-between case)."""
    NEAR = "near"
    FAR = "far"
    NET_CORD = "net_cord"


@dataclass
class BounceEvent:
    """One detected ground bounce.

    Frame-indexed at the bounce moment (the y-velocity minimum on the
    ball trajectory). court_x / court_y in court-canonical metres.
    """
    task_id: str                                  # uuid string; cast to UUID at insert
    frame_idx: int
    ts: float
    confidence: float                             # 0..1
    in_point: bool                                # model-emitted per ADR §Q3-B
    source: SignalSource = SignalSource.STOPGAP_UNTRAINED

    court_x: Optional[float] = None
    court_y: Optional[float] = None
    player_side: Optional[PlayerSide] = None

    # Diagnostic — not persisted, useful for bench + visual debug
    diagnostics: dict = field(default_factory=dict)

    def to_db_row(self) -> dict:
        """Convert to a dict matching ml_analysis.ball_bounces schema.

        player_side is downcast to its enum value (or None); source likewise.
        """
        return {
            "job_id": self.task_id,
            "frame_idx": int(self.frame_idx),
            "ts": float(self.ts),
            "court_x": float(self.court_x) if self.court_x is not None else None,
            "court_y": float(self.court_y) if self.court_y is not None else None,
            "player_side": (
                self.player_side.value if self.player_side is not None else None
            ),
            "confidence": float(self.confidence),
            "in_point": bool(self.in_point),
            "source": self.source.value,
        }
