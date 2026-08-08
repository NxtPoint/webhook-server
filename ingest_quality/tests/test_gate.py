"""Sanity-gate regression check. Pure logic — no DB, no AWS, no deps, instant.

    .venv/Scripts/python -m ingest_quality.tests.test_gate

Fixtures encode the SHAPES measured on 2026-08-08 across all 10 payloads in
s3://<bucket>/raw-json/. The two rejects are real matches that produced empty
dashboards; the passes include df594aea, a genuinely badly-tracked match that
must NOT be rejected (9 reported rallies over 74 min, yet 100 hand-verified
points). Run this after touching ingest_quality/.
"""
from __future__ import annotations

import sys

from ingest_quality import assess, should_reject


def _payload(*, rallies=0, bounces=0, ball=0, players=2, codec="h264", final=0.62,
             swings=120, valid=100):
    """`valid` swings are spread over the first two players; any remainder of
    `swings` is emitted invalid. valid=0 reproduces the real failure mode."""
    pl = [{"player_id": i, "swings": []} for i in range(players)]
    for i in range(swings):
        pl[i % max(len(pl), 1)]["swings"].append({"valid": i < valid})
    return {
        "players": pl,
        "rallies": [{} for _ in range(rallies)],
        "ball_bounces": [{} for _ in range(bounces)],
        "ball_positions": [{} for _ in range(ball)],
        "meta": {"video_info": {"codec": codec, "duration": 3000.0}},
        "confidences": {"final_confidences": {"final": final}},
    }


# (name, payload, expect_reject, expect_warnings)
CASES = [
    # --- the two real empties: 0 rallies AND 0 bounces ---
    ("42280d38 zero valid swings", _payload(rallies=0, bounces=0, ball=0, players=74,
                                            codec="hevc", final=0.451,
                                            swings=458, valid=0), True, True),
    ("6abd37ca 2 real players, 0 valid", _payload(rallies=0, bounces=0, ball=496, players=2,
                                                  codec="hevc", final=0.408,
                                                  swings=228, valid=0), True, True),
    # HEVC that analysed FINE — codec must never be a reject reason (2 of 4 hevc
    # matches produced 154-155 silver rows; an earlier gate claimed 0/4).
    ("f7223270 hevc but USABLE", _payload(rallies=2, bounces=155, ball=24234, players=7,
                                          codec="hevc", final=0.526,
                                          swings=182, valid=155), False, True),
    ("36625d04 hevc but USABLE", _payload(rallies=1, bounces=155, ball=17419, players=13,
                                          codec="hevc", final=0.465,
                                          swings=289, valid=154), False, True),
    # --- healthy h264: must pass clean, no warnings ---
    ("e4a74645 healthy", _payload(rallies=101, bounces=1039, ball=34870, players=3,
                                  final=0.699, swings=647, valid=596), False, False),
    ("299013b3 healthy", _payload(rallies=96, bounces=764, ball=62863, players=3,
                                  final=0.627, swings=576, valid=491), False, False),
    ("df594aea badly-tracked but REAL", _payload(rallies=9, bounces=982, ball=47754,
                                                 players=3, final=0.618,
                                                 swings=734, valid=611), False, False),
]


def main() -> int:
    fails = []
    for name, payload, want_reject, want_warn in CASES:
        v = assess(payload)
        got_reject, got_warn = should_reject(v), bool(v.warnings)
        ok = got_reject == want_reject and got_warn == want_warn
        print(f"  {'ok  ' if ok else 'FAIL'} {name:34} reject={got_reject!s:5} "
              f"warn={got_warn!s:5} ({len(v.warnings)})")
        if not ok:
            fails.append(f"{name}: reject {got_reject}!={want_reject}, warn {got_warn}!={want_warn}")

    # A malformed payload must never be able to fail an ingest.
    for bad in ({}, {"rallies": None, "ball_bounces": "nonsense"}, {"meta": 7}):
        v = assess(bad)
        if not v.ok:
            fails.append(f"malformed payload {bad!r} rejected — assess() must fail open")
    print(f"  {'ok  ' if not any('malformed' in f for f in fails) else 'FAIL'} "
          f"{'malformed payloads fail open':34}")

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"\nall {len(CASES)} fixtures + fail-open checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
