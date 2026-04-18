"""Per-frame serve-pose scoring.

Based on the Silent Impact 2025 finding that the PASSIVE (tossing) arm
carries the most discriminative signal for serves vs smashes/overheads.
Ground truth reference: THETIS 12-class dataset and Mora CVPR-W 2017
tennis-pose classifiers — every serve in their labelled data shows:

  1. Non-dominant wrist rising above same-side shoulder (the ball toss)
  2. Dominant wrist rising above the head (trophy / pre-impact pose)
  3. Both wrists ABOVE the shoulder line (fully extended trophy)

We score each pose-carrying frame on those three conditions (0..3) and
then do cluster + peak picking in find_serve_candidates(). A rally shot
or a ready position never scores 2+ because the non-dominant arm stays
low. A smash scores 1-2 but never 3 (no true ball toss).

COCO keypoint order (17 points):
  0=nose, 5=L-shoulder, 6=R-shoulder, 9=L-wrist, 10=R-wrist, 11=L-hip,
  12=R-hip. We use {nose, both shoulders, both wrists} + optional hip
  for trunk-axis reference.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional, Sequence


MIN_KP_CONF = 0.3


@dataclass
class PoseScore:
    """Per-frame serve-pose evaluation."""
    usable: bool                  # enough confident keypoints to score
    trophy: bool                  # dominant wrist above nose
    toss: bool                    # non-dominant wrist above same-side shoulder
    both_up: bool                 # both wrists above shoulder line
    total: int                    # sum of {trophy, toss, both_up} ∈ {0, 1, 2, 3}
    dom_wrist_y: float = 0.0      # pixel y of dominant wrist (lower = higher in image)
    pas_wrist_y: float = 0.0
    nose_y: float = 0.0
    shoulder_y: float = 0.0

    def as_features(self) -> dict:
        return {
            "usable": self.usable,
            "trophy": self.trophy,
            "toss": self.toss,
            "both_up": self.both_up,
            "total": self.total,
            "dom_wrist_y": self.dom_wrist_y,
            "pas_wrist_y": self.pas_wrist_y,
            "nose_y": self.nose_y,
            "shoulder_y": self.shoulder_y,
        }


def parse_keypoints(raw) -> Optional[list]:
    """Normalise keypoints to a 17-element [x, y, conf] list. Returns
    None if the input can't be coerced."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not raw or len(raw) < 11:
        return None
    # Flat 51-element form (some YOLO outputs) → reshape
    if isinstance(raw[0], (int, float)):
        if len(raw) < 51:
            return None
        return [[float(raw[i * 3]), float(raw[i * 3 + 1]), float(raw[i * 3 + 2])]
                for i in range(17)]
    # Already nested — normalise float types
    return [[float(raw[i][0]), float(raw[i][1]), float(raw[i][2])]
            for i in range(min(17, len(raw)))]


def score_pose_frame(keypoints, is_left_handed: bool) -> PoseScore:
    """Score one frame's keypoints on serve-pose criteria.

    Returns a PoseScore with total ∈ {0..3}. A frame is "usable" if at
    least the DOMINANT wrist and ONE shoulder have sufficient confidence
    (and aren't zeroed-out placeholders). The toss and both_up signals
    additionally need the passive wrist to be valid; if it isn't, those
    signals are simply reported False rather than marking the whole
    frame unusable — otherwise we lose the trophy-pose peak moments
    where the passive arm is mid-swing and its wrist has transient
    low confidence.
    """
    kp = parse_keypoints(keypoints)
    empty = PoseScore(usable=False, trophy=False, toss=False,
                      both_up=False, total=0)
    if kp is None:
        return empty

    nose = kp[0]
    l_sh = kp[5]
    r_sh = kp[6]
    l_wr = kp[9]
    r_wr = kp[10]

    dom_wr = l_wr if is_left_handed else r_wr
    pas_wr = r_wr if is_left_handed else l_wr
    dom_sh = l_sh if is_left_handed else r_sh
    pas_sh = r_sh if is_left_handed else l_sh

    # Guard against YOLO's zeroed-out keypoints (returns [0, 0, low_conf]
    # when a joint is occluded). Treat those as un-usable regardless of
    # the conf value, since their coords pollute "wrist above X"
    # comparisons in image coords.
    def is_valid(p):
        return p[2] >= MIN_KP_CONF and not (p[0] == 0.0 and p[1] == 0.0)

    # Minimum to evaluate ANY signal: dominant wrist + one shoulder.
    l_sh_ok = is_valid(l_sh)
    r_sh_ok = is_valid(r_sh)
    if not is_valid(dom_wr) or not (l_sh_ok or r_sh_ok):
        return empty

    # Shoulder-line pixel y — prefer average when both valid, else the one
    if l_sh_ok and r_sh_ok:
        shoulder_y = (l_sh[1] + r_sh[1]) / 2.0
    elif l_sh_ok:
        shoulder_y = l_sh[1]
    else:
        shoulder_y = r_sh[1]

    # 1. Trophy — dominant wrist above nose (racket-over-head). When nose
    # confidence is weak (player facing away / occluded), fall back to
    # "dominant wrist CLEARLY above dominant shoulder" so we don't lose
    # the signal just because the head isn't seen.
    if is_valid(nose):
        trophy = dom_wr[1] < nose[1]
        nose_y = nose[1]
    else:
        # Require dominant wrist at least 30 px above dominant shoulder
        # (or just any shoulder if dom-shoulder was invalid) — that's
        # a clearer signal than just "above" which happens in many poses.
        ref_y = dom_sh[1] if is_valid(dom_sh) else shoulder_y
        trophy = dom_wr[1] < ref_y - 30
        nose_y = shoulder_y - 50  # stand-in for display only

    # 2. Toss — passive (non-dominant) wrist above same-side shoulder.
    # This is the Silent Impact discriminator: serves toss, smashes don't.
    # Only scorable when passive arm has valid keypoints.
    pas_valid = is_valid(pas_wr) and is_valid(pas_sh)
    toss = pas_valid and pas_wr[1] < pas_sh[1]

    # 3. Both up — both wrists above the shoulder line. Confirms the
    # full trophy-pose stance (not just one arm raised briefly).
    both_up = is_valid(pas_wr) and (dom_wr[1] < shoulder_y) and (pas_wr[1] < shoulder_y)

    total = int(trophy) + int(toss) + int(both_up)
    return PoseScore(
        usable=True,
        trophy=trophy,
        toss=toss,
        both_up=both_up,
        total=total,
        dom_wrist_y=dom_wr[1],
        pas_wrist_y=pas_wr[1] if is_valid(pas_wr) else 0.0,
        nose_y=nose_y,
        shoulder_y=shoulder_y,
    )


@dataclass
class PoseServeCandidate:
    """One pose-detected serve candidate before ball/state confirmation."""
    frame_idx: int
    ts: float
    player_id: int
    peak_score: int             # peak PoseScore.total across the cluster
    cluster_size: int           # number of usable frames in the motion cluster
    confidence: float           # 0..1 — initial pose-only confidence
    dom_wrist_y_peak: float     # for cluster picking / peak selection
    court_x: Optional[float] = None
    court_y: Optional[float] = None
    bbox: Optional[tuple] = None


def find_serve_candidates(
    pose_rows: Sequence[dict],
    player_id: int,
    is_left_handed: bool,
    fps: float,
    *,
    min_peak_score: int = 1,
    min_cluster_peak: int = 1,
    min_cluster_size: int = 4,
    cluster_max_gap_s: float = 1.2,
    min_serve_interval_s: float = 4.0,
) -> List[PoseServeCandidate]:
    """Scan a sequence of pose rows for serve candidates.

    Each pose_row must have: frame_idx, keypoints (raw form),
    court_x, court_y, bbox (optional), and ideally already filtered
    to the player we're scanning.

    Algorithm:
      1. Score each frame → PoseScore
      2. Keep frames with total >= min_peak_score AND usable
      3. Group consecutive kept frames (gap ≤ cluster_max_gap_s) into
         clusters — each cluster is one serve motion
      4. Pick the PEAK frame per cluster = the one with the highest
         dominant wrist (lowest dom_wrist_y pixel value)
      5. Enforce min_serve_interval_s between accepted peaks; on
         collision prefer the higher-scoring peak.

    A real serve cluster shows different signal peaks at different phases
    (toss-only during ball release, trophy-only during swing, both-up
    fleeting at the top). We accept individual frames with score >= 1
    (any single discriminative signal) into the cluster, but require
    the CLUSTER to contain at least one frame with score >= min_cluster_peak
    and to span min_cluster_size frames of sustained motion. This
    prevents "player happens to raise hand for 1 frame" false positives
    while catching serves where the triple-signal peak is only 1-2 frames.

    Returns candidates ordered by ts ascending.
    """
    # Step 1-2: score + filter (keep any usable frame with at least one signal)
    scored = []
    for row in pose_rows:
        score = score_pose_frame(row["keypoints"], is_left_handed)
        if not score.usable:
            continue
        if score.total < min_peak_score:
            continue
        scored.append((row, score))

    if not scored:
        return []

    # Step 3: cluster consecutive by ts gap
    gap_frames = max(1, int(round(fps * cluster_max_gap_s)))
    clusters: List[List] = [[scored[0]]]
    for row, score in scored[1:]:
        prev_row = clusters[-1][-1][0]
        if row["frame_idx"] - prev_row["frame_idx"] <= gap_frames:
            clusters[-1].append((row, score))
        else:
            clusters.append([(row, score)])

    # Step 4: peak picking — filter clusters by size + peak strength +
    # ARM-ABOVE-HEAD test on the peak frame. The arm-above-head test is
    # the cleanest single discriminator: a serve ALWAYS shows the
    # dominant wrist rising WELL above the shoulder line (typically
    # 40+ px) at peak, whereas ready position, returns, and rally
    # shots keep the wrist near or below the shoulders.
    peaks: List[PoseServeCandidate] = []
    for cluster in clusters:
        if len(cluster) < min_cluster_size:
            continue
        max_score = max(s.total for _, s in cluster)
        if max_score < min_cluster_peak:
            continue
        # The peak frame = the one with highest dominant wrist (smallest
        # dom_wrist_y pixel). This is the TROPHY moment of the serve.
        peak_row, peak_score = min(cluster, key=lambda x: x[1].dom_wrist_y)
        # Require the peak arm to be clearly ABOVE the shoulder line —
        # at least 30 px higher in image (= lower pixel y) than the
        # shoulders. This rules out "player happened to score 1 for 5
        # frames with hands around chest height" false positives.
        arm_extension_px = peak_score.shoulder_y - peak_score.dom_wrist_y
        if arm_extension_px < 30:
            continue
        peaks.append(PoseServeCandidate(
            frame_idx=peak_row["frame_idx"],
            ts=peak_row["frame_idx"] / fps,
            player_id=player_id,
            peak_score=max_score,
            cluster_size=len(cluster),
            # Confidence — peak score (1..3), cluster length, AND arm
            # extension all factor in. A sustained cluster (5+ frames)
            # with arm extension 60+ px reaches ~1.0; a brief score=1
            # cluster with barely-raised arm caps at ~0.4.
            confidence=min(1.0,
                (max_score / 3.0) * 0.4
                + min(len(cluster), 8) / 8.0 * 0.3
                + min(arm_extension_px, 100) / 100.0 * 0.3),
            dom_wrist_y_peak=peak_score.dom_wrist_y,
            court_x=peak_row.get("court_x"),
            court_y=peak_row.get("court_y"),
            bbox=peak_row.get("bbox"),
        ))

    # Step 5: temporal dedupe
    min_gap_frames = int(round(fps * min_serve_interval_s))
    accepted: List[PoseServeCandidate] = []
    for p in peaks:
        if accepted and (p.frame_idx - accepted[-1].frame_idx) < min_gap_frames:
            # Same-serve cluster. Keep the better one (higher score, then
            # higher arm = lower dom_wrist_y).
            cur = accepted[-1]
            better = (p.peak_score, -p.dom_wrist_y_peak) > (cur.peak_score, -cur.dom_wrist_y_peak)
            if better:
                accepted[-1] = p
            continue
        accepted.append(p)

    return accepted
