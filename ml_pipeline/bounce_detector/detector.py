"""Bounce detector orchestrator.

Mirrors the shape of `ml_pipeline.serve_detector.detector`:

  detect_bounces(task_id) -> list[BounceEvent]
      Production entry point. Reads bronze (ml_analysis.ball_detections,
      player_detections_roi, serve_events) for the task, runs pre-gates,
      scores survivors with the CNN, applies NMS, writes
      ml_analysis.ball_bounces rows, returns the list.

  detect_bounces_offline(...)
      In-memory variant for bench / replay — no DB writes.

# STOPGAP-untrained-stage1
# v0 ships WITHOUT trained weights. The model emits noise; the orchestrator
# treats all scores as zero by hard-clamping the threshold to 1.1 so no
# rows are ever written. This proves the plumbing works (DDL + bronze
# reads + pre-gate logic + feature extraction + DB write path) without
# polluting bronze with random predictions. The threshold returns to the
# ADR default (0.55) once `cnn.BounceCNNWrapper.load_weights()` succeeds.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import text as sql_text

from ml_pipeline.ball_merge import merged_ball_subquery
from ml_pipeline.bounce_detector.cnn import (
    BounceCNNWrapper,
    CENTRE_IDX,
    WINDOW_FRAMES,
)
from ml_pipeline.bounce_detector.db import (
    delete_bounces_for_task,
    init_bounce_schema,
)
from ml_pipeline.bounce_detector.feature_extractor import (
    COURT_LENGTH_M,
    HALF_Y,
    build_window,
)
from ml_pipeline.bounce_detector.models import (
    BounceEvent,
    PlayerSide,
    SignalSource,
)
from ml_pipeline.bounce_detector.pre_gates import apply_pre_gates
from ml_pipeline.config import FRAME_SAMPLE_FPS

logger = logging.getLogger(__name__)


# ADR-01 defaults. v0 OVERRIDES the threshold to 1.1 (no-row-ever) because
# the model is untrained. Once weights load successfully, threshold reverts
# to TRAINED_THRESHOLD.
TRAINED_THRESHOLD = 0.55
UNTRAINED_THRESHOLD = 1.1            # impossible -> zero rows persisted

NMS_MIN_GAP_S = 0.15                 # ADR §"Threshold defaults"


# COCO wrist keypoint indices.
KP_LEFT_WRIST = 9
KP_RIGHT_WRIST = 10


def _load_ball_rows(conn, task_id: str) -> list:
    """All ball detections for a task, one row per frame_idx, ordered.

    Source-preference deduped (roi_far_ball > roi_prod > main > NULL) via
    ball_merge — the sharp far-ROI ball lifts far-bounce candidate recall
    (40%->80% offline, 2026-06-13), and without the dedup the overlapping
    roi_far_ball + main rows would put 2 rows on every far frame and corrupt
    the gravity-residual peak detector. No-op until roi_* rows exist.
    """
    rows = conn.execute(sql_text(merged_ball_subquery(
        "frame_idx, x, y, court_x, court_y, is_bounce, speed_kmh"
    )), {"tid": task_id}).mappings().all()
    return [dict(r) for r in rows]


def _load_wrist_positions(conn, task_id: str) -> dict[int, list]:
    """Map frame_idx -> [(wx, wy), ...] for both wrists of both players.

    Reads keypoints (court coords) from player_detections (and optionally
    player_detections_roi for far-player coverage). v0 keeps it simple:
    full-frame player_detections only. Far-player wrist may be sparse —
    that's fine, the wrist gate only triggers when a wrist IS within
    range, so missing wrists just mean "no rejection here".
    """
    out: dict[int, list] = {}
    rows = conn.execute(sql_text("""
        SELECT frame_idx, player_id, keypoints, court_x, court_y
        FROM ml_analysis.player_detections
        WHERE job_id = :tid AND keypoints IS NOT NULL
        ORDER BY frame_idx
    """), {"tid": task_id}).mappings().all()
    for r in rows:
        kp = r["keypoints"]
        # Keypoints come in as nested JSONB or flat list. We're only after
        # the COURT-coordinate wrist position, not pixel; player_detections
        # carries court_x/court_y for the player CENTRE (feet), so we
        # approximate wrist position as that centre +- small offset. v1
        # should join player_detections_roi for true wrist court coords;
        # v0 falls back to player centre + a generous WRIST_PROXIMITY gate
        # is OK because we're only doing scaffold plumbing.
        cx = r["court_x"]
        cy = r["court_y"]
        if cx is None or cy is None:
            continue
        # Single wrist proxy at player centre — racket reach is ~1 m, the
        # 0.6 m gate threshold is generous enough that this approximation
        # still rejects racket-hits when ball passes through the player.
        out.setdefault(int(r["frame_idx"]), []).append((float(cx), float(cy)))
    return out


def _load_rally_states_by_frame(conn, task_id: str, fps: float,
                                last_frame_idx: int) -> dict[int, str]:
    """Build a per-frame rally_state lookup from serve_events.

    Maps every frame to the rally_state value of the most recent serve
    event at or before that frame. For frames before the first serve,
    state is 'pre_point' (no live point yet).

    serve_events.rally_state is the state at the moment the serve was
    DETECTED — i.e. before the serve hit. For frames AFTER a serve we
    transition to 'in_rally' until the next serve event (a conservative
    bound; a refined v2 would emit explicit rally-end markers). The
    bounce candidate gates pass on 'in_rally', so this conservative
    bound is precisely what we want.
    """
    rows = conn.execute(sql_text("""
        SELECT frame_idx, rally_state
        FROM ml_analysis.serve_events
        WHERE task_id::text = :tid
        ORDER BY frame_idx
    """), {"tid": task_id}).mappings().all()

    if not rows:
        # No serve events: every frame is 'unknown' -> gate rejects all
        # (correct for v0 — if we can't establish rally state we shouldn't
        # be writing bounces yet).
        return {}

    state_changes = []   # list of (frame_idx, state)
    for r in rows:
        state_changes.append((int(r["frame_idx"]), "in_rally"))

    out: dict[int, str] = {}
    sc_iter = iter(state_changes)
    next_change = next(sc_iter, None)
    cur_state = "pre_point"
    for fi in range(0, last_frame_idx + 1):
        while next_change is not None and fi >= next_change[0]:
            cur_state = next_change[1]
            next_change = next(sc_iter, None)
        out[fi] = cur_state
    return out


def _classify_player_side(court_y: Optional[float]) -> Optional[PlayerSide]:
    """Map a bounce court_y to NEAR / FAR / NET_CORD.

    Net-cord band is ±0.5 m around NET_Y; outside that, near if cy > HALF_Y
    else far.
    """
    if court_y is None:
        return None
    if abs(court_y - HALF_Y) <= 0.5:
        return PlayerSide.NET_CORD
    return PlayerSide.NEAR if court_y > HALF_Y else PlayerSide.FAR


def _candidate_frames_from_raw_bounces(ball_rows: list) -> list[int]:
    """v0 candidate generation: every frame flagged is_bounce=True in
    ml_analysis.ball_detections is a candidate. That's the raw TrackNet
    velocity-reversal signal — the exact set the audit measured at 84%
    FP rate. The bounce model's job is to filter those down.

    LIMITATION (measured 2026-05-28): TrackNet's is_bounce flag covers
    only ~9% of real SA-labelled floor bounces on Match 1 (6/67 strict
    matches within ±5 frames + ≤50 px per the label audit). A perfect
    model on this candidate pool therefore caps at ~9% recall vs SA.
    See `_candidate_frames_from_gravity_residual` for the v1+ alternative.
    """
    return [int(r["frame_idx"]) for r in ball_rows if r.get("is_bounce")]


# v1+ — sliding-window peak detector on the gravity-residual signal.
# Image-y based (NOT court-y) so the signal survives calibration failures —
# Match-4-class catastrophes still emit candidates because they don't need
# a homography.
#
# Default threshold tuned 2026-05-28 on Match 1 (`78c32f53`):
#   thr=10px → 761 candidates / 24 strict-match (36%) / 39 loose-match (58%)
#   thr=20px → 481 candidates / 19 strict-match (28%) / 25 loose-match (37%)
#   thr=30px → 356 candidates /  8 strict-match (12%) / 13 loose-match (19%)
# vs is_bounce baseline: 341 candidates / 6 strict (9%) / 19 loose (28%).
# At thr=10 we get a 4× lift on strict-match (the CNN's ceiling) for only
# 2.2× more candidates. Match 4 isn't a reliable validation (calibration-
# corrupt + SIGKILL'd mid-run); re-validate on next clean upload.
GR_DEFAULT_RESIDUAL_THRESHOLD_PX = 10.0
GR_DEFAULT_MIN_GAP_FRAMES = 4
GR_FIT_HALFWIDTH = 5


def _candidate_frames_from_gravity_residual(
    ball_rows: list,
    *,
    residual_threshold_px: float = GR_DEFAULT_RESIDUAL_THRESHOLD_PX,
    min_gap_frames: int = GR_DEFAULT_MIN_GAP_FRAMES,
) -> list[int]:
    """Sliding-window peak detector on the gravity-residual signal.

    Bounces are 2nd-order discontinuities in the ball's image-y
    coordinate. A parabolic fit on ±5 neighbours (excluding the
    candidate frame, matching feature_extractor._gravity_residual)
    accurately models ballistic mid-flight but mispredicts heavily at
    the bounce frame because the parabola straight-lines through the
    V-shaped bounce path. At a bounce: actual_y > predicted_y (the
    ball is DEEPER in image than the parabola predicts), so the
    residual is positive and large. Threshold + NMS turns the signal
    into a candidate set.

    Why image-y, not court-y:
      1. Physically correct — image-y exhibits ballistic motion; court-y
         (ground-plane projection) does not.
      2. Calibration-independent — Match-4-class catastrophes (100% NULL
         court_x) still emit candidates from this signal because it
         only depends on raw ball detection coordinates.
      3. Feature-parity with the CNN holds because the candidate
         generator's job is to FIND frames worth scoring; the CNN's
         features (court-based) score the windows around those frames.
         Different signals at different layers is fine.

    Returns sorted frame indices of accepted candidates.
    """
    import numpy as np
    from ml_pipeline.bounce_detector.feature_extractor import _gravity_residual

    if not ball_rows:
        return []
    last_fi = max(int(r["frame_idx"]) for r in ball_rows)
    y_seq = np.full(last_fi + 1, np.nan, dtype=np.float32)
    for r in ball_rows:
        y = r.get("y")
        if y is not None:
            y_seq[int(r["frame_idx"])] = float(y)

    # Score every frame that has a ball detection (skip detection-gap frames
    # — residual is undefined there).
    scored: list[tuple[int, float]] = []
    for r in ball_rows:
        fi = int(r["frame_idx"])
        if r.get("y") is None:
            continue
        res = _gravity_residual(y_seq, fi, halfwidth=GR_FIT_HALFWIDTH)
        if res > residual_threshold_px:
            scored.append((fi, res))

    # NMS by residual score (highest residual wins each cluster).
    scored.sort(key=lambda x: -x[1])
    accepted: list[int] = []
    for fi, _ in scored:
        if any(abs(fi - a) < min_gap_frames for a in accepted):
            continue
        accepted.append(fi)
    accepted.sort()
    return accepted


def _select_candidates(ball_rows: list, candidate_mode: Optional[str] = None) -> list[int]:
    """Dispatch on `candidate_mode` (explicit) or env var BOUNCE_CANDIDATE_MODE.

    Modes:
      'is_bounce' (default — safe, unchanged behaviour): TrackNet's
                  is_bounce=TRUE frames.
      'gravity_residual': sliding-window peak detector on image-y
                  gravity-residual. Lifts the recall ceiling from
                  TrackNet's 9% bounce-flag coverage to ball-detection
                  coverage (~50% on Match 1).

    Env-var rollback (per feedback memory env_var_rollback_pattern):
    setting BOUNCE_CANDIDATE_MODE=is_bounce restores v0 behaviour
    without a code revert.
    """
    import os
    mode = (candidate_mode or os.environ.get("BOUNCE_CANDIDATE_MODE", "is_bounce")).lower().strip()
    if mode == "gravity_residual":
        cands = _candidate_frames_from_gravity_residual(ball_rows)
        logger.info("bounce_detector: candidate_mode=gravity_residual emitted=%d "
                    "(vs is_bounce baseline of %d)",
                    len(cands),
                    sum(1 for r in ball_rows if r.get("is_bounce")))
        return cands
    # default + 'is_bounce' + any unrecognised value falls back to safe v0
    return _candidate_frames_from_raw_bounces(ball_rows)


def _nms(events: List[BounceEvent], min_gap_s: float) -> List[BounceEvent]:
    """Greedy NMS by confidence: highest-confidence event wins; suppress
    any other event within ±min_gap_s of it; repeat.

    O(n log n) — fine for the few hundred candidates per match.
    """
    if not events:
        return events
    sorted_by_conf = sorted(events, key=lambda e: -e.confidence)
    accepted: List[BounceEvent] = []
    for e in sorted_by_conf:
        clash = False
        for a in accepted:
            if abs(e.ts - a.ts) < min_gap_s:
                clash = True
                break
        if not clash:
            accepted.append(e)
    accepted.sort(key=lambda e: e.ts)
    return accepted


def _run_pipeline(
    *,
    task_id: str,
    fps: float,
    ball_rows: list,
    wrists_by_frame: dict[int, list],
    rally_by_frame: dict[int, str],
    cnn: BounceCNNWrapper,
    threshold: float,
    candidate_mode: Optional[str] = None,
) -> List[BounceEvent]:
    """Shared in-memory bounce-detection pipeline.

    Iterates candidates -> pre-gates -> feature window -> CNN score ->
    NMS. Mirrors `serve_detector.detector._run_pipeline` shape.
    """
    ball_by_frame: dict[int, dict] = {int(r["frame_idx"]): r for r in ball_rows}
    candidates = _select_candidates(ball_rows, candidate_mode=candidate_mode)

    stats = {
        "candidates": len(candidates),
        "rejected_rally_state": 0,
        "rejected_net_line": 0,
        "rejected_wrist_proximity": 0,
        "scored": 0,
        "above_threshold": 0,
    }

    events: List[BounceEvent] = []

    for fi in candidates:
        row = ball_by_frame.get(fi)
        if row is None:
            continue
        cx = row.get("court_x")
        cy = row.get("court_y")
        wrists = wrists_by_frame.get(fi, [])
        rally_state = rally_by_frame.get(fi)

        passed, reason = apply_pre_gates(
            candidate_xy=(cx, cy),
            wrist_positions=wrists,
            rally_state=rally_state,
            above_net_flag=False,         # bronze z-flag not yet available
        )
        if not passed:
            stats[f"rejected_{reason}"] += 1
            continue

        features = build_window(
            candidate_frame_idx=fi,
            ball_rows_by_frame=ball_by_frame,
            wrist_positions_at_centre=wrists,
            rally_state_at_centre=rally_state,
        )
        score = cnn.score(features)
        stats["scored"] += 1

        if score < threshold:
            continue
        stats["above_threshold"] += 1

        # fi is a T5 BRONZE frame index — bronze is sampled at FRAME_SAMPLE_FPS
        # (25) regardless of the SOURCE video fps. The threaded `fps` arg is the
        # source video_fps (e.g. 60 on Match 4); dividing by it gave event
        # timestamps 60/25=2.4× too small, so the bench could never match M4
        # events to SA-label seconds (the "fps mismatch breaks M4 bench" note).
        # Convert via the bronze sampling rate, not source fps. (Match-scoped;
        # practice bronze would use FRAME_SAMPLE_FPS_PRACTICE — revisit if the
        # bounce detector is ever run on practice.)
        ts = fi / FRAME_SAMPLE_FPS
        source = (
            SignalSource.BOUNCE_DETECTOR_V2
            if cnn.weights_loaded
            else SignalSource.STOPGAP_UNTRAINED
        )
        events.append(BounceEvent(
            task_id=task_id,
            frame_idx=fi,
            ts=ts,
            confidence=float(score),
            in_point=(rally_state in ("in_rally", "serve_in_flight")),
            source=source,
            court_x=cx,
            court_y=cy,
            player_side=_classify_player_side(cy),
            diagnostics={"score": float(score)},
        ))

    nms_events = _nms(events, NMS_MIN_GAP_S)
    stats["after_nms"] = len(nms_events)

    logger.info(
        "bounce_detector: task=%s candidates=%d pre_gate_rejected=%d "
        "scored=%d above_thr=%d after_nms=%d "
        "(rally_state=%d net_line=%d wrist=%d)",
        task_id,
        stats["candidates"],
        stats["rejected_rally_state"] + stats["rejected_net_line"]
        + stats["rejected_wrist_proximity"],
        stats["scored"],
        stats["above_threshold"],
        stats["after_nms"],
        stats["rejected_rally_state"],
        stats["rejected_net_line"],
        stats["rejected_wrist_proximity"],
    )
    return nms_events


def _persist_events(conn, events: List[BounceEvent]) -> None:
    if not events:
        return
    rows = [e.to_db_row() for e in events]
    conn.execute(sql_text("""
        INSERT INTO ml_analysis.ball_bounces
            (job_id, frame_idx, ts, court_x, court_y,
             player_side, confidence, in_point, source)
        VALUES
            (:job_id, :frame_idx, :ts, :court_x, :court_y,
             :player_side, :confidence, :in_point, :source)
    """), rows)


def detect_bounces(
    task_id: str,
    *,
    conn=None,
    replace: bool = True,
    weights_path: Optional[str] = None,
    candidate_mode: Optional[str] = None,
    threshold_override: Optional[float] = None,
) -> List[BounceEvent]:
    """Production entry point. Returns the list of detected bounces and
    persists them to ml_analysis.ball_bounces.

    `conn`: optional SQLAlchemy connection. When None, opens a fresh
    connection from db_init.engine and commits at the end (matches
    serve_detector's pattern of being callable either with an external
    transaction or self-contained).

    `weights_path`: path to the trained CNN weights file. When None or
    missing on disk, runs in STOPGAP mode (no rows written, threshold
    forced to 1.1). v0 always falls into this branch.
    """
    cnn = BounceCNNWrapper()
    cnn.load_weights(weights_path)
    threshold = threshold_override if threshold_override is not None else (
        TRAINED_THRESHOLD if cnn.weights_loaded else UNTRAINED_THRESHOLD
    )

    if conn is None:
        from db_init import engine
        with engine.begin() as managed_conn:
            return _detect_with_conn(
                conn=managed_conn, task_id=task_id, replace=replace,
                cnn=cnn, threshold=threshold, candidate_mode=candidate_mode,
            )
    return _detect_with_conn(
        conn=conn, task_id=task_id, replace=replace,
        cnn=cnn, threshold=threshold, candidate_mode=candidate_mode,
    )


def _detect_with_conn(
    *, conn, task_id: str, replace: bool,
    cnn: BounceCNNWrapper, threshold: float,
    candidate_mode: Optional[str] = None,
) -> List[BounceEvent]:
    init_bounce_schema(conn)
    if replace:
        deleted = delete_bounces_for_task(conn, task_id)
        if deleted:
            logger.info("bounce_detector: deleted %d prior bounce events", deleted)

    fps = conn.execute(sql_text(
        "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
        "WHERE job_id = :t OR task_id = :t LIMIT 1"
    ), {"t": task_id}).scalar() or 25.0

    ball_rows = _load_ball_rows(conn, task_id)
    if not ball_rows:
        logger.warning("bounce_detector: no ball_detections rows for task %s", task_id)
        return []
    last_frame_idx = max(int(r["frame_idx"]) for r in ball_rows)

    wrists_by_frame = _load_wrist_positions(conn, task_id)
    rally_by_frame = _load_rally_states_by_frame(
        conn, task_id, fps, last_frame_idx,
    )

    events = _run_pipeline(
        task_id=task_id, fps=fps,
        ball_rows=ball_rows,
        wrists_by_frame=wrists_by_frame,
        rally_by_frame=rally_by_frame,
        cnn=cnn, threshold=threshold,
        candidate_mode=candidate_mode,
    )

    _persist_events(conn, events)
    logger.info(
        "bounce_detector: persisted %d bounce events for task %s "
        "(model_source=%s threshold=%.2f)",
        len(events), task_id,
        "bounce_detector_v2" if cnn.weights_loaded else "STOPGAP-untrained",
        threshold,
    )
    return events


# ---------------------------------------------------------------------------
# Offline / bench entry point — no DB writes
# ---------------------------------------------------------------------------

def detect_bounces_offline(
    *,
    task_id: str,
    fps: float,
    ball_rows: list,
    wrists_by_frame: dict[int, list],
    rally_by_frame: dict[int, str],
    weights_path: Optional[str] = None,
    threshold_override: Optional[float] = None,
    candidate_mode: Optional[str] = None,
) -> List[BounceEvent]:
    """In-memory detection for bench / replay / the Batch bronze stage. No DB writes.

    threshold_override: lets the bench drop below UNTRAINED_THRESHOLD to
    see what the (untrained) scoring layer produces for a sanity check.
    Default = UNTRAINED_THRESHOLD when weights absent, TRAINED_THRESHOLD
    when present.
    candidate_mode: explicit 'gravity_residual'|'is_bounce' (else env).
    """
    cnn = BounceCNNWrapper()
    cnn.load_weights(weights_path)
    threshold = threshold_override if threshold_override is not None else (
        TRAINED_THRESHOLD if cnn.weights_loaded else UNTRAINED_THRESHOLD
    )
    return _run_pipeline(
        task_id=task_id, fps=fps,
        ball_rows=ball_rows,
        wrists_by_frame=wrists_by_frame,
        rally_by_frame=rally_by_frame,
        cnn=cnn, threshold=threshold,
        candidate_mode=candidate_mode,
    )
