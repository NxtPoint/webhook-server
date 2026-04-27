"""Serve detector orchestrator.

Two entry points:
  - detect_serves_for_task(conn, task_id) — production: reads from
    ml_analysis.player_detections + ball_detections, persists ServeEvent
    rows to ml_analysis.serve_events.
  - detect_serves_offline(pose_rows, ball_rows, bounce_ts, ...) —
    validation / local testing: consumes in-memory data, returns a
    list of ServeEvent objects without touching any DB.

Design — two serve signals:

  NEAR PLAYER: pose-first.
    YOLOv8x-pose reliably resolves the near player's body (100-200 px
    bbox) including wrist/shoulder/nose keypoints. We scan the pose
    sequence for the serve signature (trophy + toss + both-up) and
    pick the peak frame as contact. Ball-toss and rally-state gate
    the candidate.

  FAR PLAYER: bounce-first (legacy path, refined).
    Far player is 30-40 px, has no reliable pose. But the far player's
    serves bounce in the near service boxes where TrackNet detects them
    reliably. For each bounce on the near half (cy > HALF_Y) that has
    no nearby detected serve from the near player, treat the far
    player as the hitter and emit a ServeEvent if geometric + rally
    state gates pass.

Both signals emit ServeEvent. The merge step dedupes any cross-player
false positives by timestamp proximity.
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional, Sequence

from sqlalchemy import text as sql_text

from ml_pipeline.serve_detector.ball_toss import detect_ball_toss
from ml_pipeline.serve_detector.models import ServeEvent, SignalSource
from ml_pipeline.serve_detector.pose_signal import (
    PoseServeCandidate,
    find_serve_candidates,
    score_pose_frame,
)
from ml_pipeline.serve_detector.rally_state import (
    RallyState,
    RallyStateMachine,
    build_from_db,
)
from ml_pipeline.serve_detector.schema import (
    delete_serves_for_task,
    init_serve_events_schema,
)

logger = logging.getLogger(__name__)

# Court constants — must match ml_pipeline.config SPORT_CONFIG_SINGLES
COURT_LENGTH_M = 23.77
HALF_Y = COURT_LENGTH_M / 2.0
BASELINE_NEAR = COURT_LENGTH_M
BASELINE_FAR = 0.0
SERVICE_LINE_FROM_NET_M = 6.40

# Minimum seconds between any two accepted serves (cross-player dedupe).
CROSS_PLAYER_DEDUP_S = 3.0


def _get_dominant_hand(conn, task_id: str) -> bool:
    """Return True if the submitter is left-handed. Default right."""
    row = conn.execute(sql_text("""
        SELECT COALESCE(m.dominant_hand, 'right') AS hand
        FROM bronze.submission_context sc
        LEFT JOIN billing.member m
            ON lower(m.email) = lower(sc.email) AND m.is_primary = true
        WHERE sc.task_id = :tid LIMIT 1
    """), {"tid": task_id}).fetchone()
    return (row[0] if row else "right") == "left"


def _load_pose_rows(conn, task_id: str, player_id: int,
                    is_left_handed: bool = False) -> list:
    """Load all pose-carrying detections for one player, ordered by frame.

    Augmentation: if ml_analysis.player_detections_roi exists and has rows
    for this (task, player_id), merge those in. This is how the native-crop
    YOLOv8x-pose diag tool (extract_far_player_pose.py) lifts far-player
    pose coverage — full-frame YOLO misses 30-40 px bodies, but a tight
    crop around the far baseline resolves the keypoints. Same pattern as
    _load_ball_rows merging from ball_detections_roi.

    Dedup rule: bronze and ROI may both have a row at frame F. Prefer the
    one with a usable court_y (baseline-zone filter downstream requires
    non-NULL court_y). In practice on far-player footage, bronze
    full-frame YOLO resolves keypoints but cannot project 30-40 px feet
    to the court (court_y is NULL), while ROI native-crop extractors
    write court_y=0.0 by construction. If both have court_y the bronze
    row wins (canonical). ROI rows for frames where bronze has no row
    at all are appended.
    """
    rows = conn.execute(sql_text("""
        SELECT frame_idx, keypoints, court_x, court_y,
               bbox_x1, bbox_y1, bbox_x2, bbox_y2
        FROM ml_analysis.player_detections
        WHERE job_id = :tid AND player_id = :pid AND keypoints IS NOT NULL
        ORDER BY frame_idx
    """), {"tid": task_id, "pid": player_id}).mappings().all()

    # Check if ROI table exists (same information_schema guard as _load_ball_rows)
    table_exists = conn.execute(sql_text("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'ml_analysis'
              AND table_name = 'player_detections_roi'
        )
    """)).scalar()

    roi_rows = []
    if table_exists:
        roi_rows = conn.execute(sql_text("""
            SELECT frame_idx, keypoints, court_x, court_y,
                   bbox_x1, bbox_y1, bbox_x2, bbox_y2, source
            FROM ml_analysis.player_detections_roi
            WHERE job_id = :tid AND player_id = :pid AND keypoints IS NOT NULL
            ORDER BY frame_idx, source
        """), {"tid": task_id, "pid": player_id}).mappings().all()

    # ROI ensemble (no-dedup): when multiple ROI rows exist at same
    # frame_idx from DIFFERENT source tags, KEEP BOTH. pose_signal
    # scores each row independently; both flow into clustering. The
    # score>=1 filter naturally drops any row where this particular
    # model's keypoints don't show trophy signal at that frame, while
    # keeping the other model's correct reading.
    #
    # Prior dedup strategies all failed to capture the Base∪Large =
    # 8/11 union observed on d1fed568:
    #   (a) pick-highest-keypoint-confidence: REGRESSED 6/11 -> 5/11
    #       (Large's high-conf wrong-orientation frames won)
    #   (b) primary-wins (supplement-only): 6/11 no-op (Base has
    #       frames at Large-only serves but scored 0; secondary never
    #       gets a chance)
    #   (c) score-aware (pick best score_pose_frame per frame): 6/11
    #       composition swap (gained 497, lost 434) — per-frame best
    #       doesn't guarantee cluster-level coherence
    # No-dedup lets the clustering resolve which source "wins" at the
    # CLUSTER level: a cluster with several score>=1 frames from
    # either source can pass the min_cluster_size gate even if neither
    # source alone has enough frames. The peak-picker (min dom_wrist_y)
    # naturally selects the best trophy frame across both sources.
    # Duplicate-frame cluster_size inflation is benign: a 4-frame
    # cluster is still a cluster with real signal; the arm_extension
    # gate + min_serve_interval dedup prevent spurious fires.
    if roi_rows:
        # Light dedup: if two rows have IDENTICAL bbox (within 1 px)
        # they're the same detection and only one should enter the
        # scorer. Otherwise (different bbox, i.e. each model saw a
        # different candidate body) keep both.
        from collections import defaultdict as _dd
        _by_frame = _dd(list)
        for r in roi_rows:
            _by_frame[int(r["frame_idx"])].append(r)
        selected = []
        for f in sorted(_by_frame.keys()):
            rows_f = _by_frame[f]
            # Dedup on (bbox_x1, bbox_y1) within 2 px tolerance
            kept = []
            for r in rows_f:
                dup = False
                for k in kept:
                    if (abs(r["bbox_x1"] - k["bbox_x1"]) < 2 and
                            abs(r["bbox_y1"] - k["bbox_y1"]) < 2):
                        dup = True
                        break
                if not dup:
                    kept.append(r)
            selected.extend(kept)
        roi_rows = selected

    # Build by-frame index. Merge policy:
    #   pid=1 (far player): ROI wins wholesale when both exist. The full-
    #     frame YOLO pose detector routinely misclassifies a STATIC
    #     non-player (chair umpire, line judge) as pid=1 — observed on
    #     d1fed568 for frames 9550-9720 where bronze pid=1 was fixed at
    #     bbox center (470, 240) for 6+ seconds, which is OUTSIDE the
    #     far-baseline ROI (654-1358). Those bronze keypoints describe
    #     the umpire's chest-level hands, not the server's trophy pose.
    #     The ROI extractor (extract_vitpose_far.py) is designed for
    #     small-body far-player detection with a side-prior filter, and
    #     its keypoints are the canonical far-player signal.
    #   pid=0 (near player): bronze wins as before (near YOLO resolves
    #     100-200 px bodies reliably; no ROI extractor runs for near).
    #     If an ROI row exists only for a frame bronze missed, append it.
    #     If bronze has NULL court_y, borrow ROI's for baseline-zone gate.
    by_frame = {}
    for r in rows:
        by_frame[int(r["frame_idx"])] = {
            "frame_idx": r["frame_idx"],
            "keypoints": r["keypoints"],
            "court_x": r["court_x"],
            "court_y": r["court_y"],
            "bbox": (r["bbox_x1"], r["bbox_y1"], r["bbox_x2"], r["bbox_y2"]),
            "_origin": "bronze",
        }
    added = 0
    overridden = 0
    roi_wins = 0
    skipped = 0
    far_player = (player_id == 1)
    for r in roi_rows:
        f = int(r["frame_idx"])
        existing = by_frame.get(f)
        roi_entry = {
            "frame_idx": r["frame_idx"],
            "keypoints": r["keypoints"],
            "court_x": r["court_x"],
            "court_y": r["court_y"],
            "bbox": (r["bbox_x1"], r["bbox_y1"], r["bbox_x2"], r["bbox_y2"]),
            "_origin": "roi",
        }
        if existing is None:
            by_frame[f] = roi_entry
            added += 1
        elif far_player:
            # ROI wins wholesale for pid=1 — bronze pid=1 is unreliable
            by_frame[f] = roi_entry
            roi_wins += 1
        elif existing["court_y"] is None and r["court_y"] is not None:
            # Near-player merge: keep bronze kp, borrow ROI court coords
            existing["court_x"] = r["court_x"]
            existing["court_y"] = r["court_y"]
            existing["_origin"] = "bronze+roi_coords"
            overridden += 1
        else:
            skipped += 1

    out = [{k: v for k, v in row.items() if not k.startswith("_")}
           for row in by_frame.values()]

    if roi_rows:
        logger.info(
            "player_detections pid=%d augmented: bronze=%d +roi_only=%d "
            "(roi_override=%d borrowed_cy=%d dup_skipped=%d)",
            player_id, len(rows), added, roi_wins, overridden, skipped,
        )
    out.sort(key=lambda r: r["frame_idx"])
    return out


def _load_ball_rows(conn, task_id: str) -> list:
    """Load all ball detections (for ball_toss lookups).

    Optionally merges in rows from ml_analysis.ball_detections_roi — the
    table populated by ml_pipeline.diag.extract_roi_bounces for serve-
    window ROI-cropped TrackNet passes. These ROI rows carry extra
    bounces in the service boxes that the full-frame bronze pass
    systematically misses (ball ~1-2 px at 640×360 global scale). When
    the table doesn't exist or has no rows for this task, behaviour is
    identical to the pre-augmentation version.

    Dedup rule: if the bronze layer already has a bounce within ±3
    frames AND within 1.5 m of an ROI bounce, we drop the ROI row —
    the bronze one already anchors the serve. Otherwise the ROI row
    is kept (and will fill a gap the serve_detector currently can't).
    """
    rows = conn.execute(sql_text("""
        SELECT frame_idx, x, y, is_bounce, court_x, court_y, speed_kmh
        FROM ml_analysis.ball_detections
        WHERE job_id = :tid
        ORDER BY frame_idx
    """), {"tid": task_id}).mappings().all()
    rows = [dict(r) for r in rows]

    # Check table existence via information_schema BEFORE selecting — if the
    # table doesn't exist, a direct SELECT raises an exception that poisons
    # the current transaction (Postgres aborts the whole block; all later
    # queries fail with InFailedSqlTransaction even though we caught the
    # Python exception). Guarding with information_schema avoids this.
    table_exists = conn.execute(sql_text("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'ml_analysis'
              AND table_name = 'ball_detections_roi'
        )
    """)).scalar()
    if not table_exists:
        logger.debug("ball_detections_roi table not present — skipping ROI merge")
        return rows

    try:
        roi_rows = conn.execute(sql_text("""
            SELECT frame_idx, x, y, is_bounce, court_x, court_y
            FROM ml_analysis.ball_detections_roi
            WHERE job_id = :tid
            ORDER BY frame_idx
        """), {"tid": task_id}).mappings().all()
    except Exception as exc:
        logger.warning("ball_detections_roi query failed (%s) — skipping ROI merge", exc)
        return rows

    if not roi_rows:
        return rows

    bronze_bounces = [
        (r["frame_idx"], r.get("court_x"), r.get("court_y"))
        for r in rows if r.get("is_bounce")
    ]

    def _is_dup_of_bronze(fi, cx, cy) -> bool:
        for bfi, bcx, bcy in bronze_bounces:
            if abs(bfi - fi) > 3:
                continue
            if cx is None or bcx is None or cy is None or bcy is None:
                return True  # close in frame and no coords to compare — assume dup
            if (bcx - cx) ** 2 + (bcy - cy) ** 2 <= 1.5 ** 2:
                return True
        return False

    added = 0
    skipped = 0
    for r in roi_rows:
        fi = r["frame_idx"]
        cx = r.get("court_x")
        cy = r.get("court_y")
        if r.get("is_bounce") and _is_dup_of_bronze(fi, cx, cy):
            skipped += 1
            continue
        rows.append({
            "frame_idx": fi,
            "x": r["x"],
            "y": r["y"],
            "is_bounce": r.get("is_bounce", False),
            "court_x": cx,
            "court_y": cy,
            "speed_kmh": None,
        })
        added += 1

    rows.sort(key=lambda r: r["frame_idx"])
    logger.info(
        "ball_detections augmented: +%d ROI rows (skipped %d duplicates)",
        added, skipped,
    )
    return rows


def _baseline_zone(court_y: Optional[float]) -> Optional[str]:
    """Classify a court_y into 'near' / 'far' baseline zones. Tolerant of
    calibration extrapolation slack past the painted baselines AND of
    slight mid-court drift during the serve motion (player shifts
    forward ~1-2m during the toss + trophy pose even though their
    feet stay behind the baseline)."""
    if court_y is None:
        return None
    if 18.5 <= court_y <= 28.0:
        return "near"
    if -3.5 <= court_y <= 4.5:
        return "far"
    return None


def _detect_pose_based_serves(
    pose_rows: list,
    player_id: int,
    is_left_handed: bool,
    fps: float,
    task_id: str,
    rally: RallyStateMachine,
    ball_rows: list,
) -> List[ServeEvent]:
    """Pose-first detection for the given player."""
    # Require the baseline spatial prior: only score frames where the
    # player is within baseline zones (they can't serve from mid-court).
    # Serves start from behind a baseline — tolerate ±2-3m slack for
    # calibration noise but reject any mid-court frames outright.
    baseline_rows = [r for r in pose_rows if _baseline_zone(r.get("court_y")) is not None]

    candidates = find_serve_candidates(
        pose_rows=baseline_rows,
        player_id=player_id,
        is_left_handed=is_left_handed,
        fps=fps,
    )
    logger.info(
        "serve_detector: %d pose serve candidates for player %d (from %d baseline-zone pose rows)",
        len(candidates), player_id, len(baseline_rows),
    )

    events: List[ServeEvent] = []
    for c in candidates:
        # Rally-state gate — pose is the primary signal. Accept a
        # candidate in IN_RALLY state if it shows either:
        #   (a) peak_score >= 3  (all three signals captured simultaneously
        #       somewhere in the cluster — unambiguous trophy)
        #   OR
        #   (b) confidence >= 0.65 AND cluster_size >= 20
        #       (sustained pose over ~0.8s+ with solid trophy geometry —
        #       rally-ending bounces sometimes keep rally_state=IN_RALLY
        #       stuck between points, which was blocking real score-1
        #       clusters on task 8a5e0b5e at ts 120.28 and 178.44
        #       despite dt 0.04-0.08s matches, conf 0.68-0.73,
        #       cluster_size 25-30).
        #
        # Anything failing both → reject.
        state = rally.state_at(c.ts)
        if c.player_id == 0:
            # Near (pid=0): bronze pose data is dense and reliable —
            # 0.65/20 has held since 2026-04-22 (added to catch real
            # serves at task 8a5e0b5e ts=120.28 and 178.44 where rally
            # state was stuck IN_RALLY).
            sustained_ok = (c.confidence >= 0.65 and c.cluster_size >= 20)
        else:
            # Far (pid=1): tighter thresholds, added 2026-04-25 after task
            # 4a591553 surfaced four mid-rally pid=1 trophy poses with
            # conf 0.89-0.99 and sustained clusters that defeated the
            # 0.65/20 sustained_ok exception. Real far-player serves
            # observed in the 386/410/463 MATCH set have conf 0.86-0.88
            # and cluster_size in the 20-30 range — bumping to 0.85/30
            # excludes the FP cluster while still admitting real serves
            # whose rally-state happens to read IN_RALLY (e.g. when the
            # previous rally-end bounce was within 3s of the toss).
            sustained_ok = (c.confidence >= 0.85 and c.cluster_size >= 30)
        if (state == RallyState.IN_RALLY
                and c.peak_score < 3
                and not sustained_ok):
            logger.debug(
                "serve_detector: pose candidate @ ts=%.2f REJECTED "
                "(rally IN_RALLY, peak_score=%d, conf=%.2f, cluster_size=%d)",
                c.ts, c.peak_score, c.confidence, c.cluster_size,
            )
            continue

        # Optional ball-toss confirmation (boosts confidence, never rejects)
        toss = detect_ball_toss(
            ball_rows=ball_rows,
            player_bbox=c.bbox,
            contact_frame=c.frame_idx,
            fps=fps,
        )

        # Link to nearest subsequent bounce within 1.5s — but ONLY if
        # it's on the OPPOSITE side of the net from the hitter. A serve
        # MUST land on the other side. Without this check the loop picks
        # up the first same-side rally-return bounce (often 0.5-1s after
        # the serve) and attaches it as the serve bounce, which gives
        # SUSPECT_BOUNCE verdicts in reconcile_serves_strict and pollutes
        # silver's bounce_court_x/y with wrong coords. Seen on task
        # 8a5e0b5e at ts 120.28 and 178.44 — linked bounces at y=21-22
        # (near side) for near-player serves whose true bounces were on
        # the far side but not detected by TrackNet. When no valid
        # opposite-side bounce is found within 1.5s, bounce coords stay
        # NULL — matches the behaviour of all other near-player serves
        # on this video whose serve bounce TrackNet missed.
        bounce_frame = None
        bounce_cx = None
        bounce_cy = None
        hitter_near = c.court_y is not None and c.court_y > HALF_Y
        hitter_far = c.court_y is not None and c.court_y < HALF_Y

        # Service-box court-y ranges (serve MUST bounce inside the opponent's
        # service box; if a bounce is on the opposite side of net but way
        # outside the box — e.g. at the near baseline — it's a rally bounce
        # that happened to come soon after, not the serve bounce itself).
        # Tolerance of 1.5 m past the service line in both directions
        # (real wide serves land in the doubles alley).
        NEAR_SB_Y_MIN = HALF_Y - 1.5  # 10.39
        NEAR_SB_Y_MAX = HALF_Y + SERVICE_LINE_FROM_NET_M + 1.5  # 19.78
        FAR_SB_Y_MIN = HALF_Y - SERVICE_LINE_FROM_NET_M - 1.5   # 3.99
        FAR_SB_Y_MAX = HALF_Y + 1.5  # 13.39

        # Only accept a bounce link if the bounce is IN THE SERVICE BOX
        # on the opposite side of the net from the hitter. If no such
        # bounce exists within 1.5s (because the true serve bounce
        # wasn't detected), leave bounce coords NULL — better than
        # attaching a rally-return bounce with wrong coords.
        # Seen on d1fed568 ts=584.92: no WASB bounce for this serve,
        # the previous "fall back to any opposite-side bounce" logic
        # picked a back-court rally bounce at court_y=23.1 and produced
        # a SUSPECT_BOUNCE verdict.
        max_search_frames = int(round(fps * 1.5))
        for b in ball_rows:
            if not b.get("is_bounce"):
                continue
            fi = b.get("frame_idx", 0)
            if fi < c.frame_idx:
                continue
            if fi - c.frame_idx > max_search_frames:
                break
            bcy = b.get("court_y")
            # Strict opposite-side + in-service-box check
            if bcy is None:
                # No coords — can't verify, skip
                continue
            if hitter_near:
                if not (FAR_SB_Y_MIN <= bcy <= FAR_SB_Y_MAX):
                    continue
            elif hitter_far:
                if not (NEAR_SB_Y_MIN <= bcy <= NEAR_SB_Y_MAX):
                    continue
            else:
                # Unknown hitter side — accept any bounce
                pass
            bounce_frame = fi
            bounce_cx = b.get("court_x")
            bounce_cy = bcy
            break

        # Fusion confidence: pose-only base, +0.1 if ball-toss seen,
        # +0.1 if linked bounce exists. Cap at 1.0.
        conf = c.confidence
        if toss.has_rising_ball:
            conf = min(1.0, conf + 0.1)
        if bounce_frame is not None:
            conf = min(1.0, conf + 0.1)

        # Source classification
        if toss.has_rising_ball and bounce_frame is not None:
            source = SignalSource.POSE_AND_BOUNCE
        elif toss.has_rising_ball:
            source = SignalSource.POSE_AND_BALL
        elif bounce_frame is not None:
            source = SignalSource.POSE_AND_BOUNCE
        else:
            source = SignalSource.POSE_ONLY

        # NOTE on the pid=1 POSE_ONLY context gate (removed 2026-04-27):
        # Two iterations of bounce-context filters (commits caf5d60,
        # 96f1ccd, 19fec92) all penalised real far serves more than
        # they removed FPs. Bronze TrackNet routinely misses the post-
        # serve bounces that real far MATCHes need (the ball lands ~17m
        # away from the camera and clears 1-2 px), so time_to_next_bounce
        # is often >4s for real serves. Mid-rally trophy-pose FPs sit
        # AT rally times where bronze bounces ARE dense, so they pass
        # the same gate. Net result on task 4a591553: 13/14 near + 0/10
        # far strict MATCH vs original 12/14 + 3/10 — lost 2 strict
        # MATCHes total. Bounce timing is NOT a usable discriminator
        # for far-player serves with the current bronze layer.
        # Keeping the cleaner wins (NEAR-SB bounce filter at line 569,
        # per-pid sustained_ok at line 408) which don't hurt real
        # MATCHes — they only filter clearly-different FP classes.

        events.append(ServeEvent(
            task_id=task_id,
            frame_idx=c.frame_idx,
            ts=c.ts,
            player_id=player_id,
            source=source,
            confidence=conf,
            pose_score=float(c.peak_score),
            trophy_peak_frame=c.frame_idx,
            has_ball_toss=toss.has_rising_ball,
            bounce_frame=bounce_frame,
            bounce_court_x=bounce_cx,
            bounce_court_y=bounce_cy,
            rally_state=state.value,
            hitter_court_x=c.court_x,
            hitter_court_y=c.court_y,
            hitter_bbox=c.bbox,
            diagnostics={
                "cluster_size": c.cluster_size,
                "dom_wrist_y_peak": c.dom_wrist_y_peak,
                "toss_samples": toss.samples,
                "toss_y_drop": toss.y_drop_px,
            },
        ))
    return events


def _detect_bounce_based_serves_far(
    task_id: str,
    fps: float,
    ball_rows: list,
    pose_rows_far: list,
    rally: RallyStateMachine,
    pose_serve_times_near: List[float],
) -> List[ServeEvent]:
    """Bounce-first detection for the far player.

    Rationale: the far player's pose is too sparse (30-40 px body) to
    run pose-first reliably, but their serves produce bounces in the
    NEAR service boxes (bronze-side near half) which TrackNet catches
    well. We pick up those bounces and attribute them to the far
    player when:
      (a) bounce is on the near half of court (cy > HALF_Y)
      (b) rally state is not IN_RALLY
      (c) no near-player pose-based serve fires within 3s either side
          (the pose signal wins when both detect)
    """
    events: List[ServeEvent] = []
    last_fired_ts = -1e9
    bounces = [b for b in ball_rows if b.get("is_bounce")]

    # Pre-sort near-player serve timestamps for quick dedup check
    near_times = sorted(pose_serve_times_near)

    import bisect
    MIN_SERVE_GAP_S = 5.0

    # NEAR service box bounds — a far-player's SERVE must land here, not
    # anywhere on the near half. The previous gate (cy > HALF_Y) accepted
    # rally bounces deep on the near baseline (cy ~22) when rally state
    # was momentarily fooled into BETWEEN_POINTS. Tightening to the actual
    # service box (cy in [10.39, 19.78]) plus the per-side x bounds makes
    # the bounce alone a near-sufficient signal that this WAS a serve.
    NEAR_SB_Y_MIN = HALF_Y - 1.5            # 10.39 — half-meter past net
    NEAR_SB_Y_MAX = HALF_Y + SERVICE_LINE_FROM_NET_M + 1.5   # 19.78 — past service line
    SINGLES_HALF_WIDTH_M = 4.115            # singles court half-width
    COURT_CENTRE_X = 5.485                  # COURT_WIDTH_DOUBLES_M / 2
    SB_X_TOL = 1.5                          # let wide serves into the doubles alley
    NEAR_SB_X_MIN = COURT_CENTRE_X - SINGLES_HALF_WIDTH_M - SB_X_TOL  # -0.13
    NEAR_SB_X_MAX = COURT_CENTRE_X + SINGLES_HALF_WIDTH_M + SB_X_TOL  # 11.10

    for b in bounces:
        cy = b.get("court_y")
        cx = b.get("court_x")
        if cy is None:
            continue
        if not (NEAR_SB_Y_MIN <= cy <= NEAR_SB_Y_MAX):
            continue  # bounce on far half OR on near baseline — not a serve bounce
        if cx is not None and not (NEAR_SB_X_MIN <= cx <= NEAR_SB_X_MAX):
            continue  # bounce way outside the doubles court x bounds
        ts = b["frame_idx"] / fps

        # Query state JUST BEFORE this bounce — the bounce itself is in
        # the rally state machine's list, so state_at(ts) always returns
        # IN_RALLY. We want "was the court idle leading into this bounce?"
        state = rally.state_at(ts - 0.1)
        if state == RallyState.IN_RALLY:
            continue

        # Skip if a near-player pose-based serve fires within the
        # BEFORE window — a genuine same-serve conflict where the
        # near player is the server and the pose signal beat us to it.
        # But DO NOT dedup against near-player events that fire AFTER
        # the far-player bounce: those are the near player RETURNING
        # the far player's serve, not a competing serve signal.
        # (Seen on 8a5e0b5e / d1fed568: near-player pose FP fires ~1 s
        # after the far serve bounce due to the return-stroke motion.)
        i = bisect.bisect_right(near_times, ts)
        # Any near-pose event in [ts - CROSS_PLAYER_DEDUP_S, ts] blocks.
        if i > 0 and (ts - near_times[i - 1]) < CROSS_PLAYER_DEDUP_S \
                 and (ts - near_times[i - 1]) >= 0:
            continue

        # Cooldown between consecutive far-player serves
        if ts - last_fired_ts < MIN_SERVE_GAP_S:
            continue

        # Confidence is lower than pose-based — 0.6 base, +0.1 if idle
        # time is long (clearly between-points), -0.1 if bounce_court_x
        # is weird (far from any service box).
        idle = rally.time_since_last_bounce(ts)
        conf = 0.6 + (0.1 if idle > 5.0 else 0.0)

        events.append(ServeEvent(
            task_id=task_id,
            frame_idx=b["frame_idx"],
            ts=ts,
            player_id=1,
            source=SignalSource.BOUNCE_ONLY,
            confidence=min(1.0, conf),
            bounce_frame=b["frame_idx"],
            bounce_court_x=b.get("court_x"),
            bounce_court_y=b.get("court_y"),
            rally_state=state.value,
            diagnostics={"idle_before_s": idle},
        ))
        last_fired_ts = ts

    logger.info("serve_detector: %d bounce-based far-player serves", len(events))
    return events


def _persist_events(conn, events: List[ServeEvent]) -> None:
    if not events:
        return
    rows = [e.to_db_row() for e in events]
    conn.execute(sql_text("""
        INSERT INTO ml_analysis.serve_events
            (task_id, frame_idx, ts, player_id, source, confidence,
             pose_score, trophy_peak_frame, has_ball_toss,
             bounce_frame, bounce_court_x, bounce_court_y,
             rally_state, hitter_court_x, hitter_court_y)
        VALUES
            (:task_id, :frame_idx, :ts, :player_id, :source, :confidence,
             :pose_score, :trophy_peak_frame, :has_ball_toss,
             :bounce_frame, :bounce_court_x, :bounce_court_y,
             :rally_state, :hitter_court_x, :hitter_court_y)
        ON CONFLICT (task_id, frame_idx, player_id) DO NOTHING
    """), rows)


def detect_serves_for_task(conn, task_id: str, *, replace: bool = True) -> List[ServeEvent]:
    """Production entry point. Runs pose-first for near player + bounce-first
    for far player. Persists to ml_analysis.serve_events. Returns the
    events for downstream consumption or logging."""
    init_serve_events_schema(conn)
    if replace:
        deleted = delete_serves_for_task(conn, task_id)
        if deleted:
            logger.info("serve_detector: deleted %d prior serve events", deleted)

    fps = conn.execute(sql_text(
        "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs WHERE job_id=:t"
    ), {"t": task_id}).scalar() or 25.0
    is_left_handed = _get_dominant_hand(conn, task_id)

    pose_near = _load_pose_rows(conn, task_id, 0, is_left_handed=is_left_handed)
    pose_far = _load_pose_rows(conn, task_id, 1, is_left_handed=is_left_handed)
    ball_rows = _load_ball_rows(conn, task_id)
    rally = build_from_db(conn, task_id, fps)

    # Order: FAR pose first, then NEAR pose using a rally state
    # machine augmented with the far-pose event timestamps. Rationale:
    # bronze TrackNet routinely misses the ball bounce from a FAR
    # player's serve into the near service box — without those
    # bounces, the rally state machine thinks the ball isn't in play
    # after a FAR serve, and the subsequent NEAR player's RETURN
    # STROKE (which can briefly score pose_score=3 on a high-
    # backswing return) passes the rally-state gate as if it were a
    # serve. Observed on 8a5e0b5e ts=502.72: SA labels FAR serve at
    # 502.72 then NEAR return at 503.72, but T5 fires pid=0
    # pose_only at 503.8 score=3 with rally_state=between_points,
    # beating the real FAR serve in reconcile timing-wins. Feeding
    # far-pose events into the rally state before near-pose detection
    # puts the state correctly at IN_RALLY for those windows.
    far_pose_events = _detect_pose_based_serves(
        pose_rows=pose_far,
        player_id=1,
        is_left_handed=is_left_handed,
        fps=fps,
        task_id=task_id,
        rally=rally,
        ball_rows=ball_rows,
    )

    # Augment rally state with far-pose serve times + 0.5s flight
    # (approximate hit→bounce time; pose fires at trophy which is ~0.5s
    # before hit, so trophy+0.5 ≈ hit, and another ~0.5s adds bounce
    # time; use trophy+1 as a rough "ball-in-play" marker). Use 8 s
    # idle threshold to match far-bounce code — a tennis rally can go
    # 10-15 s on this footage, and we don't want near-pose FPs to pass
    # the gate just because a few seconds elapsed since the far serve.
    far_pose_times = sorted([e.ts + 1.0 for e in far_pose_events])
    augmented_rally_for_near = RallyStateMachine(
        bounce_ts=sorted(list(rally.bounce_ts) + far_pose_times),
        idle_threshold_s=8.0,
    )

    near_events = _detect_pose_based_serves(
        pose_rows=pose_near,
        player_id=0,
        is_left_handed=is_left_handed,
        fps=fps,
        task_id=task_id,
        rally=augmented_rally_for_near,
        ball_rows=ball_rows,
    )

    # Rebuild the rally state machine using detected serve events AS
    # rally events, and with a LONGER idle threshold (8s). After a
    # near-player serve, the rally can continue for ~10-15s with
    # sporadic bounces. We want bounce-based far-player detection to
    # stay OFF during that rally. 8s is the typical longest gap
    # between shots within one point on MATCHI-style footage where
    # TrackNet occasionally loses the ball mid-rally.
    # Only NEAR pose events augment the rally state for far-bounce
    # detection. Far pose events MUST NOT be added — they describe
    # the SAME player the bounce detector is working on, and a far
    # pose firing at a slightly-wrong time would push the rally
    # state to IN_RALLY and block the correct far bounce. The intent
    # of the augmentation is "near player's serve strikes keep the
    # rally active so we don't re-emit them as far serves".
    near_pose_times = [e.ts for e in near_events]
    augmented_bounce_ts = sorted(list(rally.bounce_ts) + near_pose_times)
    rally_for_far = RallyStateMachine(
        bounce_ts=augmented_bounce_ts,
        idle_threshold_s=8.0,
    )

    # Cross-player dedup also uses NEAR pose only (see rationale above).
    far_bounce_events = _detect_bounce_based_serves_far(
        task_id=task_id,
        fps=fps,
        ball_rows=ball_rows,
        pose_rows_far=pose_far,
        rally=rally_for_far,
        pose_serve_times_near=near_pose_times,
    )

    all_events = near_events + far_pose_events + far_bounce_events
    all_events.sort(key=lambda e: e.ts)
    _persist_events(conn, all_events)
    logger.info(
        "serve_detector: persisted %d serve events (near_pose=%d far_pose=%d far_bounce=%d)",
        len(all_events), len(near_events), len(far_pose_events), len(far_bounce_events),
    )
    return all_events


# -----------------------------------------------------------------------------
# Offline / validation entry point — no DB, no side effects
# -----------------------------------------------------------------------------

def detect_serves_offline(
    *,
    task_id: str,
    pose_rows_near: Sequence[dict],
    pose_rows_far: Sequence[dict] = (),
    ball_rows: Sequence[dict] = (),
    is_left_handed: bool = False,
    fps: float = 25.0,
) -> List[ServeEvent]:
    """In-memory detection for local validation. Same logic as
    detect_serves_for_task but with explicitly-passed data and no
    DB writes."""
    bounce_ts = [
        b["frame_idx"] / fps for b in ball_rows if b.get("is_bounce")
    ]
    rally = RallyStateMachine(bounce_ts=bounce_ts)

    pose_near = list(pose_rows_near)
    pose_far = list(pose_rows_far)
    ball_list = list(ball_rows)

    near_events = _detect_pose_based_serves(
        pose_rows=pose_near, player_id=0,
        is_left_handed=is_left_handed, fps=fps, task_id=task_id,
        rally=rally, ball_rows=ball_list,
    )
    far_pose_events = _detect_pose_based_serves(
        pose_rows=pose_far, player_id=1,
        is_left_handed=is_left_handed, fps=fps, task_id=task_id,
        rally=rally, ball_rows=ball_list,
    )

    # See detect_serves_for_task — augment the rally state machine with
    # the near-player serve times so bounce-based far-player detection
    # doesn't treat return-bounces as serves. Longer idle threshold (8s)
    # keeps the machine in IN_RALLY during typical sparse-ball rallies.
    pose_serve_times = [e.ts for e in near_events] + [e.ts for e in far_pose_events]
    augmented = sorted(bounce_ts + pose_serve_times)
    rally_for_far = RallyStateMachine(bounce_ts=augmented, idle_threshold_s=8.0)

    far_bounce_events = _detect_bounce_based_serves_far(
        task_id=task_id, fps=fps, ball_rows=ball_list,
        pose_rows_far=pose_far, rally=rally_for_far,
        pose_serve_times_near=pose_serve_times,
    )
    all_events = near_events + far_pose_events + far_bounce_events
    all_events.sort(key=lambda e: e.ts)
    return all_events
