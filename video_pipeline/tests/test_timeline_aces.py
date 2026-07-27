"""Aces must survive into the highlight reel.

No pytest (CLAUDE.md rule #1) — run with:
    python -m video_pipeline.tests.test_timeline_aces

Needs pandas, which is blocked on the Windows dev box by Application Control,
so it self-skips there. Run it in a container that has pandas:

    docker run --rm -v "C:\\dev\\webhook-server:/app" -w /app tf-ffmpeg-test \\
        sh -c "pip install -q pandas sqlalchemy && python -m video_pipeline.tests.test_timeline_aces"

WHY THIS EXISTS (2026-07-27): a point's span is max(ball_hit_s)-min(ball_hit_s)
and build_video_timeline drops anything under MIN_POINT_DURATION_S (0.5s). An
ace has exactly ONE ball-hit event, so span = 0.00s and it was silently cut from
every reel. Measured on df594aea: 9 of 100 points dropped, all serve-only —
7 aces + 2 serve errors. The reel was losing the best points in the match.
"""
from __future__ import annotations

import sys

FAILURES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def main() -> int:
    try:
        import pandas as pd
    except Exception as e:
        print(f"SKIP: pandas unavailable here ({type(e).__name__}) — "
              f"run in a container (see module docstring)")
        return 0

    from video_pipeline.build_video_timeline import (
        MIN_POINT_DURATION_S, build_video_timeline_from_silver, timeline_to_edl,
    )
    from video_pipeline.video_trim_api import SINGLE_SHOT_PAD_S, pad_single_shot_points

    TASK = "t"

    def row(pt, t, excl=False):
        return {"task_id": TASK, "point_number": pt, "ball_hit_s": float(t), "exclude_d": excl}

    # pt1 = normal rally; pt2 = ACE (one shot); pt3 = normal
    raw = pd.DataFrame([
        row(1, 100.0), row(1, 104.0), row(1, 108.0),
        row(2, 200.0),                                   # <- the ace
        row(3, 300.0), row(3, 305.0),
    ])

    print("baseline (no padding) — reproduces the bug")
    tl = build_video_timeline_from_silver(raw.copy(), task_id=TASK)
    pts = sorted(tl["entity_id"].tolist()) if "entity_id" in tl.columns else []
    check(len(tl) == 2, f"ace is dropped without the fix (got {len(tl)} segments)")

    print("with pad_single_shot_points")
    padded = pad_single_shot_points(raw.copy())
    check(len(padded) == len(raw) + 1, "exactly one synthetic row added")

    span2 = (padded.loc[padded.point_number == 2, "ball_hit_s"].max()
             - padded.loc[padded.point_number == 2, "ball_hit_s"].min())
    check(abs(span2 - SINGLE_SHOT_PAD_S) < 1e-6, f"ace span is now {SINGLE_SHOT_PAD_S}s")
    check(span2 >= MIN_POINT_DURATION_S, "ace span clears MIN_POINT_DURATION_S")

    tl2 = build_video_timeline_from_silver(padded, task_id=TASK)
    check(len(tl2) == 3, f"all 3 points now produce segments (got {len(tl2)})")

    edl = timeline_to_edl(tl2)
    segs = edl["segments"]
    ace = [s for s in segs if s["start_s"] <= 200.0 <= s["end_s"]]
    check(len(ace) == 1, "a segment covers the ace timestamp")
    if ace:
        dur = ace[0]["end_s"] - ace[0]["start_s"]
        check(dur >= 4.0, f"ace clip is watchable ({dur:.1f}s, padding included)")
        check(ace[0]["start_s"] < 200.0, "clip starts before the serve (toss visible)")
        check(ace[0]["end_s"] > 200.0, "clip continues past the serve")

    print("non-regression")
    # Multi-shot points must be untouched.
    untouched = pad_single_shot_points(pd.DataFrame([row(1, 10.0), row(1, 20.0)]))
    check(len(untouched) == 2, "multi-shot point gets no synthetic row")

    # Excluded rows must not resurrect a point, and must not mask a real single.
    with_excl = pad_single_shot_points(pd.DataFrame([
        row(5, 50.0), row(5, 90.0, excl=True),          # spine span = 0 -> pad
    ]))
    check(len(with_excl) == 3, "excluded rows don't mask a single-shot point")

    check(len(pad_single_shot_points(pd.DataFrame(
        columns=["task_id", "point_number", "ball_hit_s", "exclude_d"]))) == 0,
        "empty frame is handled")

    # A point with two rows at the SAME timestamp is still span 0 -> must pad,
    # and must add only ONE row.
    dupe = pad_single_shot_points(pd.DataFrame([row(7, 70.0), row(7, 70.0)]))
    check(len(dupe) == 3, f"same-timestamp point pads exactly once (got {len(dupe)})")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all ace-in-reel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
