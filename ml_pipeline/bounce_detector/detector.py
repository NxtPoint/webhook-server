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
    """All ball detections for a task, ordered by frame_idx.

    Lighter version of the serve_detector's _load_ball_rows — we don't
    need the ROI merge here in v0 (the bounce model is trained on bronze
    ball_detections; ROI bounces from extract_roi_bounces are a separate
    signal pathway that v1+ can fold in).
    """
    rows = conn.execute(sql_text("""
        SELECT frame_idx, x, y, court_x, court_y, is_bounce, speed_kmh
        FROM ml_analysis.ball_detections
        WHERE job_id = :tid
        ORDER BY frame_idx
    """), {"tid": task_id}).mappings().all()
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

    v1+ can add candidates from a sliding-window peak detector on the
    gravity-residual feature so we catch bounces TrackNet missed; for v0
    plumbing the raw set is sufficient.
    """
    return [int(r["frame_idx"]) for r in ball_rows if r.get("is_bounce")]


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
) -> List[BounceEvent]:
    """Shared in-memory bounce-detection pipeline.

    Iterates candidates -> pre-gates -> feature window -> CNN score ->
    NMS. Mirrors `serve_detector.detector._run_pipeline` shape.
    """
    ball_by_frame: dict[int, dict] = {int(r["frame_idx"]): r for r in ball_rows}
    candidates = _candidate_frames_from_raw_bounces(ball_rows)

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

        ts = fi / fps
        source = (
            SignalSource.BOUNCE_DETECTOR_V1
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
    threshold = (
        TRAINED_THRESHOLD if cnn.weights_loaded else UNTRAINED_THRESHOLD
    )

    if conn is None:
        from db_init import engine
        with engine.begin() as managed_conn:
            return _detect_with_conn(
                conn=managed_conn, task_id=task_id, replace=replace,
                cnn=cnn, threshold=threshold,
            )
    return _detect_with_conn(
        conn=conn, task_id=task_id, replace=replace,
        cnn=cnn, threshold=threshold,
    )


def _detect_with_conn(
    *, conn, task_id: str, replace: bool,
    cnn: BounceCNNWrapper, threshold: float,
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
    )

    _persist_events(conn, events)
    logger.info(
        "bounce_detector: persisted %d bounce events for task %s "
        "(model_source=%s threshold=%.2f)",
        len(events), task_id,
        "bounce_detector_v1" if cnn.weights_loaded else "STOPGAP-untrained",
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
) -> List[BounceEvent]:
    """In-memory detection for bench / replay. No DB writes.

    threshold_override: lets the bench drop below UNTRAINED_THRESHOLD to
    see what the (untrained) scoring layer produces for a sanity check.
    Default = UNTRAINED_THRESHOLD when weights absent, TRAINED_THRESHOLD
    when present.
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
    )
