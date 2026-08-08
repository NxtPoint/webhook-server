"""Post-analysis sanity gate for SportAI result payloads.

Why: `VIDEO_QUALITY_CHECK_ENABLED` gates the video BEFORE analysis. Nothing
checked what came BACK. A failed SportAI analysis returns HTTP 200 with a
well-formed payload containing no ball, no rallies and no bounces — so the
ingest "succeeded", silver got nothing, the dashboard rendered empty, the
customer got a "your video is ready" email, and a credit was consumed.

Measured 2026-08-08 over every payload in s3://<bucket>/raw-json/ (10 matches):

    codec  n  n_rallies              final_conf
    h264   6  101, 96, 27, 27, 24, 9  0.618-0.699
    hevc   4  2, 1, 0, 0              0.408-0.526

Every HEVC upload was broken; every H.264 one worked. On task 42280d38 the
player tracking was FINE (the two real players tracked across 82k/85k frames at
0.71 pose confidence) — only the ball detector returned nothing, which is why
the footage looks perfect to a human. That match also carried 74 phantom
"players", and their near-empty `location_heatmap` grids are what OOM-killed the
512 MB ingest worker (see [[feedback_gz_size_is_not_an_ingest_memory_proxy]]).

So this module answers one question: did SportAI actually analyse this match?

REJECT is deliberately restricted to the UNAMBIGUOUS case — zero ball positions
AND zero rallies, i.e. there is literally nothing for silver to build from. A
real match cannot have zero ball positions, so this carries no false-positive
risk and needs no tuned threshold. Anything softer (low confidence, few rallies)
is a WARNING: it is flagged to ops and recorded, but still ingested, because
`df594aea` proves a genuinely-poor match can still be worth keeping (9 rallies
reported over 74 minutes, yet 100 hand-verified real points).

Do NOT turn the warnings into rejections without re-measuring. The confidence
gap (0.526 -> 0.618) is real but rests on 10 matches, and `silver.match_quality`
already tiers on the same numbers for downstream reliability display.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Master switch. Off = assess and log only, never reject (rollback without deploy).
SANITY_GATE_ENABLED = (os.getenv("INGEST_SANITY_GATE_ENABLED", "1").strip() != "0")

# Warn-only threshold on SportAI's own aggregate confidence. The observed h264/hevc
# gap is 0.526-0.618; 0.55 sits inside it. Warning only — see module docstring.
MIN_FINAL_CONF = float(os.getenv("INGEST_SANITY_MIN_FINAL_CONF") or "0.55")

# Codecs SportAI has never successfully analysed here. Warning only: the reject
# decision is made on the OUTPUT, never on the container format.
SUSPECT_CODECS = {
    c.strip().lower()
    for c in (os.getenv("INGEST_SANITY_SUSPECT_CODECS") or "hevc,h265").split(",")
    if c.strip()
}


@dataclass
class Verdict:
    ok: bool = True
    reason: str = ""                       # short machine-ish code, "" when ok
    detail: str = ""                       # human sentence for ops + ingest_error
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _n(payload: dict, key: str) -> int:
    v = payload.get(key)
    return len(v) if isinstance(v, (list, dict)) else 0


def assess(payload: dict) -> Verdict:
    """Judge a SportAI result payload. Never raises — a bug here must not be able
    to fail an ingest, so anything unexpected returns an OK verdict."""
    v = Verdict()
    try:
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        vinfo = meta.get("video_info") if isinstance(meta.get("video_info"), dict) else {}
        conf = payload.get("confidences") if isinstance(payload.get("confidences"), dict) else {}
        final = conf.get("final_confidences") if isinstance(conf.get("final_confidences"), dict) else {}

        n_ball = _n(payload, "ball_positions")
        n_rally = _n(payload, "rallies")
        n_bounce = _n(payload, "ball_bounces")
        n_players = _n(payload, "players")
        codec = str(vinfo.get("codec") or "").lower()
        final_conf = final.get("final")
        try:
            final_conf = float(final_conf) if final_conf is not None else None
        except (TypeError, ValueError):
            final_conf = None

        v.stats = {
            "ball_positions": n_ball, "rallies": n_rally, "ball_bounces": n_bounce,
            "players": n_players, "codec": codec or None, "final_conf": final_conf,
            "duration_s": vinfo.get("duration"),
        }

        # --- only judge a payload we actually recognise -------------------------
        # `"rallies": []` (SportAI ran and found none) and a MISSING `rallies`
        # key (a payload shape we don't understand) look identical to a plain
        # zero-check. Conflating them means a SportAI key rename would reject
        # every match on the platform at once. Judge only when both keys we
        # reject on are present AND are lists; otherwise fail open and warn.
        shape_ok = isinstance(payload.get("rallies"), list) and isinstance(
            payload.get("ball_bounces"), list)
        if not shape_ok:
            v.warnings.append(
                "payload shape not recognised (missing or non-list 'rallies' / "
                "'ball_bounces') — sanity gate skipped, ingesting anyway"
            )
            return v

        # --- the one unambiguous rejection -------------------------------------
        # No rallies AND no floor bounces => silver has no point structure to
        # derive from, so the dashboard is empty whatever else is present. Not a
        # threshold; a structural fact about the payload.
        #
        # Deliberately NOT keyed on ball_positions: 6abd37ca returned 496 ball
        # positions with zero rallies and zero bounces and is just as empty, so
        # a ball-based rule would have let it through. Every healthy match
        # measured has >= 9 rallies and >= 162 bounces; the two rejects have 0/0.
        if n_rally == 0 and n_bounce == 0:
            v.ok = False
            v.reason = "empty_analysis"
            v.detail = (
                f"SportAI returned no usable analysis: {n_ball} ball positions, "
                f"{n_rally} rallies, {n_bounce} floor bounces "
                f"({n_players} players detected, codec={codec or 'unknown'}, "
                f"final_confidence={final_conf}). Nothing to ingest."
            )

        # --- warnings (still ingested) -----------------------------------------
        if codec in SUSPECT_CODECS:
            v.warnings.append(
                f"codec={codec}: every {codec} upload measured so far produced a "
                f"broken or near-empty analysis (h264 has not)"
            )
        if final_conf is not None and final_conf < MIN_FINAL_CONF:
            v.warnings.append(
                f"SportAI final confidence {final_conf:.3f} < {MIN_FINAL_CONF} "
                f"(healthy matches measured 0.618-0.699)"
            )
        # Phantom-player storm: the shape that OOM-killed the 512 MB worker.
        if n_players > 10:
            v.warnings.append(
                f"{n_players} players detected for a singles match — phantom "
                f"detections carry per-player location_heatmap grids and inflate "
                f"ingest memory sharply"
            )
        return v
    except Exception as e:  # noqa: BLE001 — must never fail an ingest
        log.warning("SANITY GATE assess() failed, passing through: %s", e)
        return Verdict()


def should_reject(v: Verdict) -> bool:
    """Honour the kill switch at the decision point, so a disabled gate still
    assesses and logs (which is what tells us whether to re-enable it)."""
    return SANITY_GATE_ENABLED and not v.ok
