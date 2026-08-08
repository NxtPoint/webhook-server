"""Post-analysis sanity gate for SportAI result payloads.

Why: `VIDEO_QUALITY_CHECK_ENABLED` gates the video BEFORE analysis. Nothing
checked what came BACK. A failed SportAI analysis returns HTTP 200 with a
well-formed payload, so bronze ingested happily, silver wrote nothing, and the
match still read `last_status='completed'` — showing up in the customer's
sidebar as a finished match with 0 points, and counting as billable to
`billing_import_from_bronze` (which selects on exactly that status).

Measured 2026-08-08 over every payload in s3://<bucket>/raw-json/ (10 matches),
against the silver rows each actually produced:

    ball_positions   valid swings   silver rows   n
    >= 5591          94 - 611       94 - 611      8   usable
    496 / 0          0              0             2   broken (42280d38, 6abd37ca)

The failure chain is: ball tracking collapses -> SportAI cannot validate swings
against ball proximity, so it flags EVERY swing invalid -> build_silver_v2.
_resolve_two_players counts distinct player_id over `valid IS TRUE` only, finds
0, and raises "Cannot resolve 2 players (found 0)" -> zero silver rows.

Note the error message misleads: 6abd37ca carries exactly 2 correctly-identified
players and 228 swings. Nothing was wrong with the player IDs; all 228 swings
were flagged invalid.

CODEC IS NOT THE CAUSE. Both failures were HEVC, but 2 of the 4 HEVC matches
analysed fine (155 and 154 silver rows) — so HEVC is a weak risk signal at n=2
failures, nothing more. An earlier version of this file claimed "every HEVC
upload was broken"; that was built on `n_rallies`, which does not track
usability at all (df594aea reports 9 rallies and has 611 silver rows). Do not
reinstate a codec-based rejection.

So this module answers one question: did SportAI actually analyse this match?

REJECT is restricted to structural, threshold-free facts — zero valid swings, or
no rallies AND no bounces. Anything softer (low confidence, suspect codec, few
rallies) is a WARNING: flagged to ops but still ingested, because `df594aea`
proves a genuinely-poor match can still be worth keeping (9 reported rallies
over 74 minutes, yet 100 hand-verified real points).

Do NOT turn the warnings into rejections without re-measuring — they rest on 10
matches, and `silver.match_quality` already tiers on the same confidence numbers
for downstream reliability display.
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

        # SportAI's per-swing `valid` flag is the DIRECT predictor of the silver
        # failure: build_silver_v2._resolve_two_players counts distinct player_id
        # over `valid IS TRUE` swings only, so zero valid swings => "Cannot
        # resolve 2 players (found 0)" => zero silver rows, however many swings
        # or players the payload nominally contains.
        n_swings = n_valid = 0
        for _p in (payload.get("players") or []):
            if not isinstance(_p, dict):
                continue
            for _s in (_p.get("swings") or []):
                if isinstance(_s, dict):
                    n_swings += 1
                    if _s.get("valid") is True:
                        n_valid += 1
        codec = str(vinfo.get("codec") or "").lower()
        final_conf = final.get("final")
        try:
            final_conf = float(final_conf) if final_conf is not None else None
        except (TypeError, ValueError):
            final_conf = None

        v.stats = {
            "ball_positions": n_ball, "rallies": n_rally, "ball_bounces": n_bounce,
            "players": n_players, "swings": n_swings, "valid_swings": n_valid,
            "codec": codec or None, "final_conf": final_conf,
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

        # --- rejections: both structural, both threshold-free -------------------
        # (1) Zero VALID swings. The direct cause of the observed silver failure
        #     (see above). Measured over all 10 archived payloads this separates
        #     perfectly: the 8 usable matches have 94-611 valid swings, the 2
        #     broken ones have exactly 0 (with 228 and 458 swings present but all
        #     flagged invalid, because SportAI validates a swing against ball
        #     proximity and ball tracking had collapsed).
        # (2) No rallies AND no floor bounces => no point structure to derive.
        #
        # Deliberately NOT keyed on ball_positions. It is the best-separating
        # NUMBER (usable >= 5591, broken 0 and 496) but that is an 11x gap fitted
        # to 10 samples, and a count threshold on a 10-min match is not the same
        # as on a 2-hour one. `valid == 0` is the same evidence without a knob.
        if n_valid == 0 and n_swings > 0:
            v.ok = False
            v.reason = "no_valid_swings"
            v.detail = (
                f"SportAI flagged every one of {n_swings} detected swings as "
                f"invalid (0 valid), so silver cannot resolve either player. "
                f"{n_ball} ball positions, {n_rally} rallies, {n_bounce} floor "
                f"bounces, {n_players} players, codec={codec or 'unknown'}, "
                f"final_confidence={final_conf}."
            )
        elif n_rally == 0 and n_bounce == 0:
            v.ok = False
            v.reason = "empty_analysis"
            v.detail = (
                f"SportAI returned no point structure: {n_ball} ball positions, "
                f"{n_rally} rallies, {n_bounce} floor bounces "
                f"({n_players} players detected, codec={codec or 'unknown'}, "
                f"final_confidence={final_conf}). Nothing to ingest."
            )

        # --- warnings (still ingested) -----------------------------------------
        if codec in SUSPECT_CODECS:
            v.warnings.append(
                f"codec={codec}: both matches that failed outright were {codec}, "
                f"but 2 of 4 {codec} matches analysed fine — a weak risk signal, "
                f"not a cause"
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
