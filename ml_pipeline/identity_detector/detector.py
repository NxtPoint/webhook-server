"""Identity detector orchestrator (v1, rule-based — no training).

Two entry points (mirroring `serve_detector.detector`):

  - `detect_identity_for_task(conn, task_id)` — production: loads serve_events
    + player_detections + submission_context, runs the v1 algorithm,
    persists IdentitySegment rows to `ml_analysis.player_identity_segments`.
  - `detect_identity_offline(serve_events, pose_rows_by_track, a_starts_near, task_id)`
    — in-memory / validation: pure function, no DB, no side effects.

Algorithm — three steps:

  1. `derive_game_boundaries(serve_events)` — one boundary per server-
     alternation run, with set/tiebreak metadata.
  2. For each inter-game gap, ask `detect_changeover(...)` whether the
     players swapped (driven by ITF expected-changeover + dual-cross
     side-change observation).
  3. Fold in the upload-form initial mapping (`a_starts_near`). The first
     game's IdentitySegment is set entirely from the form (source =
     RULE_V1_INITIAL). Each subsequent segment flips if and only if the
     changeover rule says "swapped"; confidence is the rule's per-game
     confidence value.

This is the spec from ADR-03 §"Build spec v1". It assumes track_id 0 =
near-camera, 1 = far at video start — the convention the rest of the T5
pipeline uses (see `serve_detector/detector.py` pid=0 vs pid=1 comments).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text as sql_text

from ml_pipeline.identity_detector.changeover_rule import (
    ChangeoverDecision,
    detect_changeover,
    is_expected_changeover,
)
from ml_pipeline.identity_detector.db import (
    delete_identity_for_job,
    init_identity_schema,
)
from ml_pipeline.identity_detector.game_boundaries import derive_game_boundaries
from ml_pipeline.identity_detector.models import (
    GameBoundary,
    IdentitySegment,
    IdentitySource,
    Side,
)

logger = logging.getLogger(__name__)

# Tracks we consider for identity (the YOLOv8 tracker is configured for 2 players)
TRACK_NEAR = 0
TRACK_FAR = 1


# ---------------------------------------------------------------------------
# Confidence -> source promotion to NEEDS_REVIEW
# ---------------------------------------------------------------------------
NEEDS_REVIEW_CONF = 0.50


def _maybe_needs_review(seg: IdentitySegment) -> IdentitySegment:
    """If confidence is below NEEDS_REVIEW_CONF, promote source to needs_review
    so the dashboard can surface the row for manual tagging (ADR-03 confidence
    table). Leaves the side labels intact so silver still has a guess."""
    if seg.confidence < NEEDS_REVIEW_CONF and seg.source != IdentitySource.NEEDS_REVIEW:
        seg.source = IdentitySource.NEEDS_REVIEW
    return seg


# ---------------------------------------------------------------------------
# Pure-function offline detector
# ---------------------------------------------------------------------------

def detect_identity_offline(
    *,
    task_id: str,
    serve_events: Sequence[dict],
    pose_rows_by_track: Dict[int, Sequence[Tuple[float, Optional[float]]]],
    a_starts_near: bool = True,
) -> List[IdentitySegment]:
    """In-memory entry point.

    serve_events: each dict has at minimum {ts, player_id}.
    pose_rows_by_track: dict {track_id: [(ts, court_y), ...]} time-ordered.
    a_starts_near: from upload-form. True => Player A is the near track at
                   game 1; False => Player A is far.
    """
    boundaries = derive_game_boundaries(serve_events)
    if not boundaries:
        return []

    # Game 1 — set side mapping straight from the form.
    cur_a_side = Side.NEAR if a_starts_near else Side.FAR
    cur_b_side = Side.FAR if a_starts_near else Side.NEAR
    segments: List[IdentitySegment] = []
    segments.append(_maybe_needs_review(IdentitySegment(
        job_id=task_id,
        game_number=1,
        player_a_side=cur_a_side,
        player_b_side=cur_b_side,
        confidence=1.0,  # form answer is canonical for game 1
        source=IdentitySource.RULE_V1_INITIAL,
        diagnostics={"a_starts_near": a_starts_near,
                     "game_t_start": boundaries[0].t_start,
                     "game_t_end": boundaries[0].t_end,
                     "server_track_id": boundaries[0].server_track_id,
                     "tiebreak": boundaries[0].tiebreak},
    )))

    # Subsequent games — apply the changeover rule across the gap to the
    # previous game.
    pose_a = pose_rows_by_track.get(TRACK_NEAR, [])
    pose_b = pose_rows_by_track.get(TRACK_FAR, [])
    for i in range(1, len(boundaries)):
        prev = boundaries[i - 1]
        cur = boundaries[i]
        expected = is_expected_changeover(prev.game_number)
        decision = detect_changeover(
            pose_rows_track_a=pose_a,
            pose_rows_track_b=pose_b,
            gap_start_s=prev.t_end,
            gap_end_s=cur.t_start,
            expected=expected,
        )
        if decision.swapped:
            cur_a_side = Side.FAR if cur_a_side == Side.NEAR else Side.NEAR
            cur_b_side = Side.FAR if cur_b_side == Side.NEAR else Side.NEAR
        diag = {**decision.diagnostics,
                "game_t_start": cur.t_start,
                "game_t_end": cur.t_end,
                "server_track_id": cur.server_track_id,
                "tiebreak": cur.tiebreak}
        segments.append(_maybe_needs_review(IdentitySegment(
            job_id=task_id,
            game_number=cur.game_number,
            player_a_side=cur_a_side,
            player_b_side=cur_b_side,
            confidence=decision.confidence,
            source=decision.source,
            diagnostics=diag,
        )))
    return segments


# ---------------------------------------------------------------------------
# Production DB-backed entry point
# ---------------------------------------------------------------------------

def _load_serve_events(conn, task_id: str) -> List[dict]:
    rows = conn.execute(sql_text("""
        SELECT ts, player_id
        FROM ml_analysis.serve_events
        WHERE task_id = :tid
        ORDER BY ts
    """), {"tid": task_id}).mappings().all()
    return [dict(r) for r in rows]


def _load_pose_rows_by_track(
    conn,
    task_id: str,
    tracks: Sequence[int] = (TRACK_NEAR, TRACK_FAR),
) -> Dict[int, List[Tuple[float, Optional[float]]]]:
    """Load (ts, court_y) per track from player_detections.

    Notes:
      - We use frame_idx / fps for ts. fps comes from video_analysis_jobs.
      - We only need court_y (the side-of-court signal), so we skip the
        keypoints + bbox payload — keeps memory usage tiny on the Render
        512 MB main API.
    """
    fps = conn.execute(sql_text(
        "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
        "WHERE job_id = :t LIMIT 1"
    ), {"t": task_id}).scalar() or 25.0

    out: Dict[int, List[Tuple[float, Optional[float]]]] = {t: [] for t in tracks}
    for pid in tracks:
        rs = conn.execute(sql_text("""
            SELECT frame_idx, court_y
            FROM ml_analysis.player_detections
            WHERE job_id = :tid AND player_id = :pid
            ORDER BY frame_idx
        """), {"tid": task_id, "pid": pid}).fetchall()
        out[pid] = [(float(frame_idx) / float(fps), court_y)
                    for (frame_idx, court_y) in rs]
    return out


def _load_a_starts_near(conn, task_id: str) -> bool:
    """Read the upload-form field from bronze.submission_context. Defaults
    True (matches the column default) when the row pre-dates the field."""
    row = conn.execute(sql_text("""
        SELECT COALESCE(a_starts_near, TRUE) AS asn
        FROM bronze.submission_context
        WHERE task_id = :tid
    """), {"tid": task_id}).fetchone()
    if row is None:
        # Edge: no submission_context for this task. Be defensive and assume
        # the legacy default ("owner films from near side").
        logger.warning("identity_detector: no submission_context for %s; "
                       "defaulting a_starts_near=True", task_id)
        return True
    return bool(row[0])


def _persist_segments(conn, segments: Sequence[IdentitySegment]) -> None:
    if not segments:
        return
    conn.execute(sql_text("""
        INSERT INTO ml_analysis.player_identity_segments
            (job_id, game_number, player_a_side, player_b_side, confidence, source)
        VALUES
            (:job_id, :game_number, :player_a_side, :player_b_side,
             :confidence, :source)
        ON CONFLICT (job_id, game_number) DO UPDATE SET
            player_a_side = EXCLUDED.player_a_side,
            player_b_side = EXCLUDED.player_b_side,
            confidence    = EXCLUDED.confidence,
            source        = EXCLUDED.source
    """), [seg.to_db_row() for seg in segments])


def detect_identity_for_task(
    conn,
    task_id: str,
    *,
    replace: bool = True,
) -> List[IdentitySegment]:
    """Production entry point. Wipes any prior identity segments for the
    task (when replace=True), runs the v1 detector, and persists results.

    NOTE: this function does NOT call `init_identity_schema()` so that the
    boot-time init can own DDL idempotency (the same pattern as
    `serve_detector`). Callers may wrap a call here in their own init() if
    they want a single-call lifecycle."""
    init_identity_schema(conn)
    if replace:
        deleted = delete_identity_for_job(conn, task_id)
        if deleted:
            logger.info("identity_detector: deleted %d prior segments", deleted)

    serve_events = _load_serve_events(conn, task_id)
    if not serve_events:
        logger.info("identity_detector: no serve_events for %s — skipping", task_id)
        return []
    pose_by_track = _load_pose_rows_by_track(conn, task_id)
    a_starts_near = _load_a_starts_near(conn, task_id)

    segments = detect_identity_offline(
        task_id=task_id,
        serve_events=serve_events,
        pose_rows_by_track=pose_by_track,
        a_starts_near=a_starts_near,
    )
    _persist_segments(conn, segments)
    logger.info(
        "identity_detector: persisted %d identity segments (a_starts_near=%s)",
        len(segments), a_starts_near,
    )
    return segments
