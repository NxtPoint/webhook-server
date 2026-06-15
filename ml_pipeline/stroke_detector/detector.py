"""Stroke detector orchestrator.

Two entry points:
  - detect_strokes_for_task(conn, task_id) — production: reads pose rows
    from ml_analysis.player_detections, persists StrokeEvent rows to
    ml_analysis.stroke_events.
  - detect_strokes_offline(pose_rows, ...) — validation / local testing:
    consumes in-memory data, returns a list of StrokeEvent objects with
    no DB writes.

Both entry points share the same in-memory pipeline (`_run_pipeline`) so
offline numbers always match prod — same drift-avoidance pattern as
serve_detector.

Algorithm summary (full detail in velocity_signal.py + __init__.py):
  1. Compute per-frame max wrist velocity, per player + globally.
  2. Smooth (rolling mean window=3).
  3. Find local-max peaks above MIN_VELOCITY with MIN_GAP_FRAMES separation.
  4. Apply +OFFSET to map peak → predicted_hit_frame (the probe found
     velocity peaks fire 4-6 frames before SA's truth contact frame).
  5. Apply deceleration filter: reject peaks where post-peak mean velocity
     hasn't dropped to ≤ DECEL_RATIO_MAX × peak (filters pickup/walk
     motions whose velocity plateaus rather than falling).
  6. Build StrokeEvent records, attributed to the player_id whose wrist
     contributed the global-max velocity at the peak frame.

Confidence is a normalised function of peak velocity magnitude over the
threshold band — peaks just above MIN_VELOCITY score ~0.4, peaks at 2×
threshold or higher saturate at 1.0.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from sqlalchemy import text as sql_text

from ml_pipeline.config import FRAME_SAMPLE_FPS
from ml_pipeline.stroke_detector.hit_location import assemble_hit_locations
from ml_pipeline.stroke_detector.models import StrokeEvent
from ml_pipeline.stroke_detector.schema import (
    delete_strokes_for_task,
    init_stroke_events_schema,
)
from ml_pipeline.stroke_detector.velocity_signal import (
    DEFAULT_MAX_GAP_FRAMES,
    DEFAULT_MIN_GAP_FRAMES,
    DEFAULT_MIN_KP_CONF,
    DEFAULT_MIN_VELOCITY_PX_PER_FRAME,
    DEFAULT_NORMALIZE_BODY_SCALE,
    DEFAULT_PEAK_TO_CONTACT_OFFSET,
    DEFAULT_SMOOTH_WINDOW,
    DEFAULT_SWING_PATH_WINDOW,
    DEFAULT_DECEL_RATIO_MAX,
    compute_global_max_velocity,
    compute_per_player_velocity,
    compute_player_scale_factors,
    detect_velocity_peaks,
    median_body_scales,
    post_peak_velocity_at,
    pre_peak_velocity_at,
    smooth_velocity,
    swing_path_torsos,
    velocity_at,
)

# Min wrist swing path (torso-lengths) for a peak attributed to the reference
# (near, dense-pose) player to count as a real stroke. Brings near active from
# ~108 to ~SA's 43 on Match 1 while leaving the far player (sparse pose) ungated.
# Robust, not knife-edge: near lands 39-44 across the whole 0.70-0.85 band, so
# the result doesn't hinge on the exact value. PROVISIONAL — calibrated on ONE
# match (no 2nd video / training corpus yet); the durable fix is the trained
# stroke classifier (Q1-D). Re-validate the threshold when more SA truth exists.
DEFAULT_NEAR_MIN_SWING_PATH_TORSOS = 0.75

logger = logging.getLogger(__name__)


def _confidence_from_peak(peak_v: float, min_v: float) -> float:
    """Map peak velocity to a 0..1 confidence.

    Floor at 0.4 (any accepted peak is at least min_v), saturating at 1.0
    when peak_v ≥ 2 × min_v. Linear in between.
    """
    if peak_v <= min_v:
        return 0.4
    span = min_v  # i.e. from min_v to 2*min_v
    return min(1.0, 0.4 + 0.6 * ((peak_v - min_v) / max(span, 1.0)))


def _run_pipeline(
    *,
    task_id: str,
    fps: float,
    poses: Sequence[Tuple[int, int, list]],
    min_velocity: float,
    min_gap_frames: int,
    smooth_window: int,
    min_kp_conf: float,
    max_gap_frames: int,
    peak_to_contact_offset: int,
    decel_ratio_max: float,
    normalize_by_body_scale: bool = DEFAULT_NORMALIZE_BODY_SCALE,
    near_min_swing_path_torsos: float = 0.0,
    swing_path_window: int = DEFAULT_SWING_PATH_WINDOW,
) -> List[StrokeEvent]:
    """In-memory detection pipeline shared by prod + offline paths.

    SINGLE SOURCE OF TRUTH for stroke-detection logic. Mirrors
    serve_detector._run_pipeline's role: every behaviour change goes here
    so prod and offline harness numbers don't drift.
    """
    scale_factors = None
    if normalize_by_body_scale:
        scale_factors = compute_player_scale_factors(poses, min_kp_conf=min_kp_conf)
        if scale_factors:
            logger.info(
                "stroke_detector: body-scale velocity factors %s "
                "(>1 boosts the smaller/far player so attribution isn't near-biased)",
                {pid: round(f, 2) for pid, f in sorted(scale_factors.items())},
            )

    # Swing-path gate setup (reference/near player only — see module docstring).
    # Build the per-player pose index + median body scale once, up front.
    med_scales: Dict[int, float] = {}
    per_player_pose: Dict[int, List[Tuple[int, list]]] = {}
    player_frames: Dict[int, List[int]] = {}
    gate_on = near_min_swing_path_torsos > 0.0 and bool(scale_factors)
    if gate_on:
        med_scales = median_body_scales(poses, min_kp_conf=min_kp_conf)
        for frame, pid, kps in poses:
            per_player_pose.setdefault(pid, []).append((int(frame), kps))
        for pid in per_player_pose:
            per_player_pose[pid].sort(key=lambda r: r[0])
            player_frames[pid] = [r[0] for r in per_player_pose[pid]]
    swing_rejected = 0
    per_player = compute_per_player_velocity(
        poses,
        min_kp_conf=min_kp_conf,
        max_gap_frames=max_gap_frames,
        scale_factors=scale_factors,
    )
    global_vel, attribution = compute_global_max_velocity(per_player)
    smoothed = smooth_velocity(global_vel, window=smooth_window)

    raw_peaks = detect_velocity_peaks(
        smoothed,
        min_velocity=min_velocity,
        min_gap_frames=min_gap_frames,
    )

    events: List[StrokeEvent] = []
    for peak_frame in raw_peaks:
        peak_v = velocity_at(smoothed, peak_frame)
        if peak_v is None:
            continue

        pre_v = pre_peak_velocity_at(smoothed, peak_frame, offset=3)
        post_v = post_peak_velocity_at(smoothed, peak_frame, offset=3)

        # Deceleration filter — pickup spec: `v[i+3] > peak * threshold` →
        # reject. A genuine swing's velocity drops fast past contact
        # (racquet decelerates, follow-through dissipates the kinetic
        # energy). A pickup/walk motion plateaus. When post_v is missing
        # (peak at end of pose sequence, or pose dropout in the follow-
        # through window), we keep the peak — better to admit a likely-
        # real stroke than reject for missing-data reasons.
        #
        # Single-frame check (not a mean over [i+1, i+3]) because the mean
        # runs much higher than v[i+3] alone (frames i+1 / i+2 are still
        # close to peak velocity) and zaps 100% of peaks on real video.
        decel_ratio: Optional[float] = None
        if post_v is not None and peak_v > 0:
            decel_ratio = post_v / peak_v
            if decel_ratio > decel_ratio_max:
                logger.debug(
                    "stroke_detector: peak @ frame=%d REJECTED "
                    "(decel_ratio=%.2f > %.2f, peak_v=%.1f, post_v=%.1f)",
                    peak_frame, decel_ratio, decel_ratio_max, peak_v, post_v,
                )
                continue

        predicted_hit_frame = peak_frame + peak_to_contact_offset
        ts = predicted_hit_frame / fps if fps > 0 else 0.0
        player_id = attribution.get(peak_frame, 0)
        confidence = _confidence_from_peak(peak_v, min_velocity)

        # Swing-path precision gate — reference (near, dense-pose) player only.
        # A real groundstroke sweeps a large wrist arc; a recovery/fidget peak
        # barely moves. The far player's pose is too sparse for path length to
        # be reliable (its real strokes measure low), so it is left ungated.
        if gate_on and scale_factors.get(player_id, 1.0) <= 1.0 + 1e-9:
            rows = per_player_pose.get(player_id)
            sc = med_scales.get(player_id)
            if rows and sc:
                path = swing_path_torsos(
                    rows, player_frames[player_id], peak_frame, sc,
                    window=swing_path_window, max_gap_frames=max_gap_frames,
                )
                if path is not None and path < near_min_swing_path_torsos:
                    swing_rejected += 1
                    continue

        events.append(StrokeEvent(
            task_id=task_id,
            frame_idx=peak_frame,
            ts=ts,
            predicted_hit_frame=predicted_hit_frame,
            player_id=int(player_id),
            confidence=confidence,
            peak_velocity_px_per_frame=peak_v,
            pre_peak_v=pre_v,
            post_peak_v=post_v,
            decel_ratio=decel_ratio,
            diagnostics={"smoothed_window": smooth_window},
        ))
    if gate_on and swing_rejected:
        logger.info(
            "stroke_detector: swing-path gate rejected %d near peaks "
            "(min_path=%.2f torso-lengths, window=+/-%d)",
            swing_rejected, near_min_swing_path_torsos, swing_path_window,
        )
    events.sort(key=lambda e: e.predicted_hit_frame)
    return events


def _kps_to_array(raw) -> Optional["np.ndarray"]:
    """Compact keypoints to a float32 (17, 3) numpy array. Accepts the DB's
    JSONB nested form, the flat-51 form, or a JSON string; None on malformed.

    Storing as numpy float32 instead of nested Python lists is ~10x smaller
    (~300B vs ~2.9KB per row) — crucial for the Render 512MB main API which
    OOM'd on 70k+ player_detections rows loaded as nested lists (~210MB)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not raw:
        return None
    if isinstance(raw[0], (int, float)):
        if len(raw) < 51:
            return None
        return np.asarray(raw[:51], dtype=np.float32).reshape(17, 3)
    try:
        arr = np.asarray(raw, dtype=np.float32)
    except (ValueError, TypeError):
        return None
    if arr.ndim != 2 or arr.shape[1] != 3 or arr.shape[0] < 11:
        return None
    if arr.shape[0] > 17:
        return arr[:17]
    if arr.shape[0] < 17:
        pad = np.zeros((17 - arr.shape[0], 3), dtype=np.float32)
        return np.vstack([arr, pad])
    return arr


def _load_pose_rows(conn, task_id: str) -> List[Tuple[int, int, "np.ndarray"]]:
    """Load all (frame_idx, player_id, keypoints) rows for a task, streamed
    server-side + compact numpy storage so peak memory fits Render's 512MB
    main API (OOM'd on the bulk-loaded nested-list form for long matches).

    Pose-only — we don't need ball/court coordinates here, just the wrist
    keypoint trajectory. Returns tuples (frame_idx, player_id, kps_array)
    where kps_array is a float32 (17, 3) ndarray.
    """
    def _stream_into(stmt, params, out):
        """Stream rows server-side (yield_per batches release cursor buffer)
        and append compact tuples — never materialises all rows in memory."""
        result = conn.execute(stmt.execution_options(
            stream_results=True, yield_per=5000,
        ), params)
        for row in result:
            kp = _kps_to_array(row[2])
            if kp is None:
                continue
            out.append((int(row[0]), int(row[1]), kp))

    out: List[Tuple[int, int, "np.ndarray"]] = []
    _stream_into(sql_text("""
        SELECT frame_idx, player_id, keypoints
        FROM ml_analysis.player_detections
        WHERE job_id::text = :tid AND keypoints IS NOT NULL
        ORDER BY player_id, frame_idx
    """), {"tid": task_id}, out)

    # Fallback for the legacy schema where job_id is an int FK
    if not out:
        _stream_into(sql_text("""
            SELECT pd.frame_idx, pd.player_id, pd.keypoints
            FROM ml_analysis.player_detections pd
            JOIN ml_analysis.video_analysis_jobs vaj
              ON pd.job_id = vaj.id::text
            WHERE vaj.task_id::text = :tid AND pd.keypoints IS NOT NULL
            ORDER BY pd.player_id, pd.frame_idx
        """), {"tid": task_id}, out)

    # ---- Merge far ViTPose pose from ml_analysis.player_detections_roi ----
    # The main table samples the far player sparsely (every PLAYER_DETECTION_
    # INTERVAL=5 frames → gap > max_gap_frames, so far wrist velocity is rarely
    # sampled) and its full-frame pid=1 rows are unreliable (often a static
    # chair-umpire misclassification). extract_far_pose writes denser, cleaner
    # far keypoints (every 2 frames, source='far_vitpose', player_id=1) that
    # only the serve_detector currently reads. Merge them the same way: ROI
    # wins wholesale for the far player (pid=1); ROI-only frames are added.
    # Existence is checked via information_schema BEFORE selecting so a missing
    # table can't poison the txn (memory feedback_postgres_missing_table).
    #
    # NOTE: far full-frame wrist velocity is small (the far body is ~30-40px),
    # so this lifts far *coverage* but does not by itself fix the near-biased
    # global-max attribution — that needs size-normalised velocity, tracked as
    # follow-on. Harmless to live output: stroke_events feed only the gated-off
    # stroke-driven silver path (T5_STROKE_DRIVEN_SILVER).
    roi_present = conn.execute(sql_text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'ml_analysis' AND table_name = 'player_detections_roi'
        LIMIT 1
    """)).scalar()
    if roi_present:
        roi_out: List[Tuple[int, int, "np.ndarray"]] = []
        _stream_into(sql_text("""
            SELECT frame_idx, player_id, keypoints
            FROM ml_analysis.player_detections_roi
            WHERE job_id::text = :tid AND keypoints IS NOT NULL
            ORDER BY frame_idx
        """), {"tid": task_id}, roi_out)
        if roi_out:
            merged = {(pid, f): (f, pid, kp) for f, pid, kp in out}
            won = added = 0
            for f, pid, kp in roi_out:
                key = (pid, f)
                if pid == 1 or key not in merged:   # ROI wins wholesale for far
                    won += key in merged
                    added += key not in merged
                    merged[key] = (f, pid, kp)
            out = sorted(merged.values(), key=lambda r: (r[1], r[0]))
            logger.info(
                "stroke_detector: merged %d far ViTPose ROI pose rows "
                "(override=%d add=%d)", len(roi_out), won, added,
            )

    return out


def _persist_events(conn, events: List[StrokeEvent]) -> None:
    if not events:
        return
    rows = [e.to_db_row() for e in events]
    conn.execute(sql_text("""
        INSERT INTO ml_analysis.stroke_events
            (task_id, frame_idx, ts, predicted_hit_frame, player_id,
             confidence, peak_velocity_px_per_frame,
             pre_peak_v, post_peak_v, decel_ratio,
             ball_hit_location_x, ball_hit_location_y, hitter_side_near, volley)
        VALUES
            (:task_id, :frame_idx, :ts, :predicted_hit_frame, :player_id,
             :confidence, :peak_velocity_px_per_frame,
             :pre_peak_v, :post_peak_v, :decel_ratio,
             :ball_hit_location_x, :ball_hit_location_y, :hitter_side_near, :volley)
        ON CONFLICT (task_id, predicted_hit_frame, player_id) DO NOTHING
    """), rows)


def detect_strokes_for_task(
    conn,
    task_id: str,
    *,
    replace: bool = True,
    min_velocity: float = DEFAULT_MIN_VELOCITY_PX_PER_FRAME,
    min_gap_frames: int = DEFAULT_MIN_GAP_FRAMES,
    smooth_window: int = DEFAULT_SMOOTH_WINDOW,
    min_kp_conf: float = DEFAULT_MIN_KP_CONF,
    max_gap_frames: int = DEFAULT_MAX_GAP_FRAMES,
    peak_to_contact_offset: int = DEFAULT_PEAK_TO_CONTACT_OFFSET,
    decel_ratio_max: float = DEFAULT_DECEL_RATIO_MAX,
    normalize_by_body_scale: bool = DEFAULT_NORMALIZE_BODY_SCALE,
    near_min_swing_path_torsos: float = DEFAULT_NEAR_MIN_SWING_PATH_TORSOS,
    swing_path_window: int = DEFAULT_SWING_PATH_WINDOW,
) -> List[StrokeEvent]:
    """Production entry point. Detects strokes from pose rows + persists
    StrokeEvent rows to ml_analysis.stroke_events.

    Returns the events list for downstream consumption / logging.
    """
    init_stroke_events_schema(conn)
    if replace:
        deleted = delete_strokes_for_task(conn, task_id)
        if deleted:
            logger.info("stroke_detector: deleted %d prior stroke events", deleted)

    # stroke_events.ts is in the SAMPLED frame space: predicted_hit_frame comes
    # from player_detections.frame_idx (sampled at FRAME_SAMPLE_FPS), so ts must
    # divide by the SAMPLED fps, NOT the source video_fps. Using video_fps here
    # was a frame-space bug (feedback_t5_two_frame_spaces) — on a 30fps source it
    # understated ts by 25/30 (~17%; ~58% at 60fps; confirmed on 63a0130d
    # 2026-06-04). Derive the sampled fps exactly like build_silver_match_t5
    # (total_frames is the sampled count / real duration) so ts matches silver;
    # fall back to FRAME_SAMPLE_FPS.
    _jr = conn.execute(sql_text(
        "SELECT total_frames, video_duration_sec FROM ml_analysis.video_analysis_jobs WHERE job_id=:t"
    ), {"t": task_id}).mappings().first()
    if _jr and _jr["total_frames"] and _jr["video_duration_sec"] and _jr["video_duration_sec"] > 0:
        fps = _jr["total_frames"] / _jr["video_duration_sec"]
    else:
        fps = float(FRAME_SAMPLE_FPS)

    poses = _load_pose_rows(conn, task_id)
    if not poses:
        logger.warning("stroke_detector: no pose rows for task %s", task_id)
        return []

    events = _run_pipeline(
        task_id=task_id,
        fps=fps,
        poses=poses,
        min_velocity=min_velocity,
        min_gap_frames=min_gap_frames,
        smooth_window=smooth_window,
        min_kp_conf=min_kp_conf,
        max_gap_frames=max_gap_frames,
        peak_to_contact_offset=peak_to_contact_offset,
        decel_ratio_max=decel_ratio_max,
        normalize_by_body_scale=normalize_by_body_scale,
        near_min_swing_path_torsos=near_min_swing_path_torsos,
        swing_path_window=swing_path_window,
    )

    # Hit-WHERE keystone: the model owns the complete hit fact. Assemble
    # ball_hit_location_x/y + hitter_side_near onto each event from bounce-opposite
    # side + nearest player detection, so silver projects them verbatim (rule #1/#2)
    # instead of reconstructing. Best-effort + NULL on the far-court tail (train-last).
    try:
        locs = assemble_hit_locations(
            conn, task_id, fps,
            [(e.predicted_hit_frame, e.player_id) for e in events],
        )
        for ev, loc in zip(events, locs):
            ev.ball_hit_location_x = loc["ball_hit_location_x"]
            ev.ball_hit_location_y = loc["ball_hit_location_y"]
            ev.hitter_side_near = loc["hitter_side_near"]
            ev.volley = loc["volley"]
    except Exception:
        logger.exception("stroke_detector: hit-location assembly failed (non-fatal); "
                         "events persisted with NULL hit location")

    _persist_events(conn, events)
    logger.info(
        "stroke_detector: persisted %d stroke events for task %s "
        "(min_v=%.1f, min_gap=%d, offset=%d, decel_max=%.2f)",
        len(events), task_id, min_velocity, min_gap_frames,
        peak_to_contact_offset, decel_ratio_max,
    )
    return events


def detect_strokes_offline(
    *,
    task_id: str,
    pose_rows: Sequence[Tuple[int, int, list]],
    fps: float = 25.0,
    min_velocity: float = DEFAULT_MIN_VELOCITY_PX_PER_FRAME,
    min_gap_frames: int = DEFAULT_MIN_GAP_FRAMES,
    smooth_window: int = DEFAULT_SMOOTH_WINDOW,
    min_kp_conf: float = DEFAULT_MIN_KP_CONF,
    max_gap_frames: int = DEFAULT_MAX_GAP_FRAMES,
    peak_to_contact_offset: int = DEFAULT_PEAK_TO_CONTACT_OFFSET,
    decel_ratio_max: float = DEFAULT_DECEL_RATIO_MAX,
    normalize_by_body_scale: bool = DEFAULT_NORMALIZE_BODY_SCALE,
    near_min_swing_path_torsos: float = DEFAULT_NEAR_MIN_SWING_PATH_TORSOS,
    swing_path_window: int = DEFAULT_SWING_PATH_WINDOW,
) -> List[StrokeEvent]:
    """In-memory detection for local validation — shares _run_pipeline
    with the prod entry point so offline numbers always match prod.
    """
    return _run_pipeline(
        task_id=task_id,
        fps=fps,
        poses=list(pose_rows),
        min_velocity=min_velocity,
        min_gap_frames=min_gap_frames,
        smooth_window=smooth_window,
        min_kp_conf=min_kp_conf,
        max_gap_frames=max_gap_frames,
        peak_to_contact_offset=peak_to_contact_offset,
        decel_ratio_max=decel_ratio_max,
        normalize_by_body_scale=normalize_by_body_scale,
        near_min_swing_path_torsos=near_min_swing_path_torsos,
        swing_path_window=swing_path_window,
    )
