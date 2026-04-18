"""Pose-based serve detection prototype.

Reads Player 0 (near, rock-solid pose tracking) keypoint sequences from
ml_analysis.player_detections and scans for the serve signature:

    1. Dominant wrist above nose (racket trophy pose)
    2. Non-dominant wrist above same-side shoulder (tossing arm raised)
    3. Player court_y near baseline (+/- 2m of y=23.77 for the near player)
    4. Temporal isolation (>= 5s since previous detected serve)

Matches the Silent Impact 2025 finding that the passive/tossing arm is the
most discriminative signal for serves vs smashes/overheads.

Compares detected timestamps against SportAI ground truth on baseline task
081e089c-f7b1-49ce-b51c-d623bcc60953. No writes, no schema changes.

Usage (DATABASE_URL set):
    python -m ml_pipeline.diag.serve_pose_prototype
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Optional

from sqlalchemy import create_engine, text as sql_text


T5_TID = "081e089c-f7b1-49ce-b51c-d623bcc60953"
SA_TID = "4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb"

# Near-player baseline zone in court metres (cy axis, 0..23.77 court span)
NEAR_BASELINE_Y_MIN = 21.5
NEAR_BASELINE_Y_MAX = 27.0

# Keypoint confidence floor; below this we ignore the joint.
MIN_KP_CONF = 0.3

# Minimum pose-frames within a candidate cluster (at PLAYER_DETECTION_INTERVAL=5
# the trophy pose should span at least one sampled frame; two gives more safety).
MIN_FRAMES_IN_PEAK = 1

# Minimum seconds between accepted serves (same-serve + cooldown dedupe).
MIN_SERVE_INTERVAL_S = 5.0


def _parse_kp(raw) -> Optional[list]:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not raw or len(raw) < 11:
        return None
    # Flat 51-float form → reshape
    if isinstance(raw[0], (int, float)):
        if len(raw) < 51:
            return None
        return [[raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]] for i in range(17)]
    return raw


def _score_frame(kp, is_left_handed: bool):
    """Return (score, features) where score in {0,1,2,3} matches how many
    of the three serve-pose conditions hold, and features is a dict for
    later logging."""
    try:
        nose = kp[0]
        l_sh = kp[5]
        r_sh = kp[6]
        l_wr = kp[9]
        r_wr = kp[10]
    except (IndexError, TypeError):
        return 0, None

    dom_wr = l_wr if is_left_handed else r_wr
    dom_sh = l_sh if is_left_handed else r_sh
    pas_wr = r_wr if is_left_handed else l_wr
    pas_sh = r_sh if is_left_handed else l_sh

    # Confidence gates — only demand conf on the joints we reference.
    if dom_wr[2] < MIN_KP_CONF or pas_wr[2] < MIN_KP_CONF:
        return 0, None
    if dom_sh[2] < MIN_KP_CONF or pas_sh[2] < MIN_KP_CONF:
        return 0, None

    score = 0
    # 1. Dominant wrist above nose (racket-above-head — trophy pose)
    cond_trophy = False
    if nose[2] >= MIN_KP_CONF:
        cond_trophy = dom_wr[1] < nose[1]
    else:
        # Fallback: dominant wrist well above dominant shoulder (> 20 px)
        cond_trophy = dom_wr[1] < dom_sh[1] - 20
    if cond_trophy:
        score += 1

    # 2. Non-dominant (tossing) wrist above same-side shoulder — the Silent
    # Impact passive-arm insight. This is the cue that separates serve
    # from overhead/smash (both have racket up, but only a serve tosses).
    cond_toss = pas_wr[1] < pas_sh[1]
    if cond_toss:
        score += 1

    # 3. Both wrists together above shoulder line — final confirmation of
    # the full trophy stance.
    shoulder_y = (l_sh[1] + r_sh[1]) / 2.0
    cond_both_up = (dom_wr[1] < shoulder_y) and (pas_wr[1] < shoulder_y)
    if cond_both_up:
        score += 1

    return score, {
        "dom_wrist_y": dom_wr[1],
        "pas_wrist_y": pas_wr[1],
        "nose_y": nose[1] if nose[2] >= MIN_KP_CONF else None,
        "shoulder_y": shoulder_y,
        "trophy": cond_trophy,
        "toss": cond_toss,
        "both_up": cond_both_up,
    }


def detect_pose_serves(conn, task_id: str, player_id: int,
                       is_left_handed: bool, fps: float,
                       baseline_y_min: float = NEAR_BASELINE_Y_MIN,
                       baseline_y_max: float = NEAR_BASELINE_Y_MAX) -> List[dict]:
    """Scan a player's pose sequence for serve candidates."""
    rows = conn.execute(sql_text("""
        SELECT frame_idx, keypoints, court_y
        FROM ml_analysis.player_detections
        WHERE job_id = :tid AND player_id = :pid AND keypoints IS NOT NULL
        ORDER BY frame_idx
    """), {"tid": task_id, "pid": player_id}).mappings().all()

    candidates: List[dict] = []
    for r in rows:
        kp = _parse_kp(r["keypoints"])
        if kp is None:
            continue
        cy = r["court_y"]
        # Spatial prior — must be in baseline zone. Skip if projection failed.
        if cy is None or not (baseline_y_min <= cy <= baseline_y_max):
            continue
        score, feats = _score_frame(kp, is_left_handed)
        if score >= 2:
            candidates.append({
                "frame_idx": r["frame_idx"],
                "ts": r["frame_idx"] / fps,
                "score": score,
                "court_y": float(cy),
                **(feats or {}),
            })

    # Collapse clusters: a serve motion spans ~1.5s so candidates within
    # 2s of each other are the same serve. Pick the one with lowest
    # dom_wrist_y (highest arm = peak of trophy pose).
    if not candidates:
        return []
    clusters: List[List[dict]] = [[candidates[0]]]
    for c in candidates[1:]:
        if c["ts"] - clusters[-1][-1]["ts"] <= 2.0:
            clusters[-1].append(c)
        else:
            clusters.append([c])

    peaks = []
    for cluster in clusters:
        if len(cluster) < MIN_FRAMES_IN_PEAK:
            continue
        peak = min(cluster, key=lambda x: x["dom_wrist_y"])
        peak["cluster_size"] = len(cluster)
        peaks.append(peak)

    # Temporal dedupe — MIN_SERVE_INTERVAL_S between accepted peaks.
    accepted: List[dict] = []
    for p in peaks:
        if accepted and (p["ts"] - accepted[-1]["ts"]) < MIN_SERVE_INTERVAL_S:
            # Keep the one with the tighter trophy pose (higher score, then
            # lower wrist_y)
            if (p["score"], -p["dom_wrist_y"]) > (accepted[-1]["score"], -accepted[-1]["dom_wrist_y"]):
                accepted[-1] = p
            continue
        accepted.append(p)

    return accepted


def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL env var required", file=sys.stderr)
        return 2
    engine = create_engine(db_url)

    with engine.connect() as conn:
        fps = conn.execute(sql_text(
            "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs WHERE job_id=:t"
        ), {"t": T5_TID}).scalar() or 25.0

        # SportAI ground truth — near-player serves only (where pose detection
        # applies). hy > 22 = near player in SportAI coords; hy < 2 = far.
        sa_near = conn.execute(sql_text("""
            SELECT ball_hit_s, serve_side_d, ball_speed
            FROM silver.point_detail
            WHERE task_id = :tid AND model = 'sportai' AND serve_d = TRUE
              AND ball_hit_location_y > 22
            ORDER BY ball_hit_s
        """), {"tid": SA_TID}).mappings().all()

        sa_far = conn.execute(sql_text("""
            SELECT ball_hit_s, serve_side_d, ball_speed
            FROM silver.point_detail
            WHERE task_id = :tid AND model = 'sportai' AND serve_d = TRUE
              AND ball_hit_location_y < 2
            ORDER BY ball_hit_s
        """), {"tid": SA_TID}).mappings().all()

        print(f"SportAI ground truth: {len(sa_near)} near-player serves, {len(sa_far)} far-player")
        print()

        # Try both handednesses so we can diagnose.
        for handedness_label, is_left in [("right-handed", False), ("left-handed", True)]:
            print(f"=== Scanning Player 0 pose, assuming {handedness_label} ===")
            peaks = detect_pose_serves(conn, T5_TID, 0, is_left, fps)
            print(f"  detected {len(peaks)} candidate serves")
            print()
            # Align with SA near-player ground truth
            matched = 0
            print(f"{'T5 ts':>8} {'score':>5} {'cluster':>7} {'dom_wr_y':>8} {'cy':>5}  |  {'SA ts':>8} {'gap':>6}")
            print("-" * 75)
            sa_used = [False] * len(sa_near)
            for p in peaks:
                best_i, best_gap = None, 1e9
                for i, s in enumerate(sa_near):
                    if sa_used[i]:
                        continue
                    gap = p["ts"] - float(s["ball_hit_s"])
                    if abs(gap) < abs(best_gap):
                        best_i, best_gap = i, gap
                if best_i is not None and abs(best_gap) <= 5.0:
                    sa_used[best_i] = True
                    matched += 1
                    sa_ts = float(sa_near[best_i]["ball_hit_s"])
                    print(f"{p['ts']:8.2f} {p['score']:>5} {p['cluster_size']:>7} {p['dom_wrist_y']:>8.0f} {p['court_y']:>5.1f}  |  {sa_ts:8.2f} {best_gap:+6.2f}")
                else:
                    print(f"{p['ts']:8.2f} {p['score']:>5} {p['cluster_size']:>7} {p['dom_wrist_y']:>8.0f} {p['court_y']:>5.1f}  |  {'—':>8} {'no match':>6}")
            print()
            print(f"  matched: {matched} / {len(sa_near)} SA near serves")
            unmatched_sa = [float(sa_near[i]['ball_hit_s']) for i in range(len(sa_near)) if not sa_used[i]]
            if unmatched_sa:
                print(f"  missed SA near serves (ts): {unmatched_sa}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
