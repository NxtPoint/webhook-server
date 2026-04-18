"""Component-level sanity tests for the serve_detector package.

No pytest in this repo — run with:
    python -m ml_pipeline.serve_detector.tests.test_components

Tests are small on purpose: the full-pipeline validation lives in
ml_pipeline/serve_detector/validate_offline.py (runs against a real
pose JSONL + DB ball data on a real match).
"""
from __future__ import annotations

import sys

from ml_pipeline.serve_detector.ball_toss import detect_ball_toss
from ml_pipeline.serve_detector.pose_signal import (
    find_serve_candidates,
    score_pose_frame,
)
from ml_pipeline.serve_detector.rally_state import RallyStateMachine


def _kp(nose=(500, 300, 0.9), lsh=(400, 400, 0.95), rsh=(600, 400, 0.95),
        lwr=(380, 600, 0.9), rwr=(620, 600, 0.9)):
    """Build a 17-element COCO keypoint list with only the joints we use."""
    zero = [0, 0, 0]
    return [list(nose), zero, zero, zero, zero, list(lsh), list(rsh),
            zero, zero, list(lwr), list(rwr), zero, zero, zero, zero, zero, zero]


def test_rally_state_basics():
    m = RallyStateMachine(bounce_ts=[10.0, 10.5, 11.0, 11.5, 12.0, 30.0])
    assert m.state_at(10.2).value == "in_rally"
    assert m.state_at(15.1).value == "between_points"  # 3s idle, next bounce >5s away
    assert m.state_at(26.0).value == "pre_point"       # idle but serve coming up
    assert m.time_since_last_bounce(13.0) == 1.0
    assert m.time_to_next_bounce(13.0) == 17.0
    print("  RallyStateMachine: OK")


def test_pose_score_trophy():
    # Right-handed full trophy: R-wrist above nose, L-wrist above L-shoulder,
    # both wrists above shoulder line
    kp = _kp(nose=(500, 300, 0.9),
             lsh=(400, 400, 0.95), rsh=(600, 400, 0.95),
             lwr=(350, 350, 0.9),   # above L-shoulder y=400
             rwr=(550, 250, 0.9))   # above nose y=300
    s = score_pose_frame(kp, is_left_handed=False)
    assert s.usable
    assert s.trophy and s.toss and s.both_up and s.total == 3, f"got {s}"
    print(f"  Trophy pose: total={s.total} OK")


def test_pose_score_ready_position():
    # Arms down by hips
    kp = _kp(nose=(500, 300, 0.9),
             lsh=(400, 400, 0.95), rsh=(600, 400, 0.95),
             lwr=(380, 600, 0.9), rwr=(620, 600, 0.9))
    s = score_pose_frame(kp, is_left_handed=False)
    assert s.usable and s.total == 0, f"got {s}"
    print(f"  Ready pose: total={s.total} OK")


def test_pose_score_single_arm_up():
    # Smash / overhead: racket arm up, other arm low — should score 1
    # (trophy only, no toss)
    kp = _kp(nose=(500, 300, 0.9),
             lsh=(400, 400, 0.95), rsh=(600, 400, 0.95),
             lwr=(380, 600, 0.9),   # low
             rwr=(550, 250, 0.9))   # above nose
    s = score_pose_frame(kp, is_left_handed=False)
    assert s.usable and s.trophy and not s.toss and s.total == 1, f"got {s}"
    print(f"  Single-arm-up (smash): total={s.total} OK")


def test_pose_score_low_confidence_dominant_wrist():
    # Dominant wrist conf too low → unusable frame
    kp = _kp(nose=(500, 300, 0.9),
             lsh=(400, 400, 0.95), rsh=(600, 400, 0.95),
             lwr=(350, 350, 0.9), rwr=(550, 250, 0.1))
    s = score_pose_frame(kp, is_left_handed=False)
    assert not s.usable
    print(f"  Low dom-wrist conf: usable={s.usable} OK")


def test_find_serve_candidates_sustained_cluster():
    # 5 consecutive pose frames with toss signal → should cluster + fire
    rows = []
    for i in range(5):
        fi = 100 + i * 5
        kp = _kp(nose=(500, 300, 0.9),
                 lsh=(400, 400, 0.95), rsh=(600, 400, 0.95),
                 lwr=(350, 250, 0.9),    # toss arm UP
                 rwr=(550, 500, 0.9))    # dom arm still low
        # Add one full-trophy frame at the peak
        if i == 2:
            kp = _kp(nose=(500, 300, 0.9),
                     lsh=(400, 400, 0.95), rsh=(600, 400, 0.95),
                     lwr=(350, 250, 0.9),
                     rwr=(560, 200, 0.9))   # dom arm above nose
        rows.append({"frame_idx": fi, "keypoints": kp, "court_y": None,
                     "court_x": None, "bbox": (400, 400, 600, 800)})
    cands = find_serve_candidates(rows, player_id=0, is_left_handed=False, fps=25.0)
    assert len(cands) == 1, f"expected 1 cluster, got {len(cands)}"
    c = cands[0]
    assert c.frame_idx == 110, f"expected peak at frame 110, got {c.frame_idx}"
    assert c.peak_score >= 2
    print(f"  Sustained cluster: peak={c.peak_score} cluster={c.cluster_size} OK")


def test_find_serve_candidates_brief_noise_rejected():
    # Single score-1 frame with short cluster — should be rejected
    rows = [
        {"frame_idx": 100, "keypoints": _kp(
            lwr=(350, 250, 0.9), rwr=(560, 500, 0.9)),
         "court_y": None, "court_x": None, "bbox": (400, 400, 600, 800)},
    ]
    cands = find_serve_candidates(rows, player_id=0, is_left_handed=False, fps=25.0)
    assert len(cands) == 0, f"expected no candidate from brief noise, got {len(cands)}"
    print(f"  Brief-noise rejected: {len(cands)} candidates OK")


def test_ball_toss_rising_ball():
    # 4 samples with pixel-y decreasing by ~150 px each step
    ball_rows = [
        {"frame_idx": 95, "x": 500, "y": 700},
        {"frame_idx": 100, "x": 500, "y": 500},
        {"frame_idx": 105, "x": 500, "y": 350},
        {"frame_idx": 110, "x": 500, "y": 250},
    ]
    ev = detect_ball_toss(ball_rows, player_bbox=(400, 500, 600, 900),
                          contact_frame=115, fps=25)
    assert ev.has_rising_ball
    assert ev.samples == 4
    assert ev.y_drop_px >= 40
    print(f"  Rising ball: drop={ev.y_drop_px:.0f}px OK")


def test_ball_toss_no_ball_near_player():
    # Ball is far from player's x — should not count
    ball_rows = [
        {"frame_idx": 100, "x": 100, "y": 500},
        {"frame_idx": 105, "x": 100, "y": 400},
    ]
    ev = detect_ball_toss(ball_rows, player_bbox=(900, 500, 1100, 900),
                          contact_frame=110, fps=25)
    assert not ev.has_rising_ball
    print(f"  Ball far from player: rising={ev.has_rising_ball} OK")


def main():
    tests = [
        test_rally_state_basics,
        test_pose_score_trophy,
        test_pose_score_ready_position,
        test_pose_score_single_arm_up,
        test_pose_score_low_confidence_dominant_wrist,
        test_find_serve_candidates_sustained_cluster,
        test_find_serve_candidates_brief_noise_rejected,
        test_ball_toss_rising_ball,
        test_ball_toss_no_ball_near_player,
    ]
    print(f"Running {len(tests)} serve_detector component tests:")
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  {t.__name__}: FAIL — {e}")
            failures += 1
    print()
    if failures:
        print(f"{failures} test(s) failed")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
