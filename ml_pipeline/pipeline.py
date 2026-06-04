"""
TennisAnalysisPipeline — Orchestrates all three ML models frame-by-frame.
Produces a structured AnalysisResult with detections + aggregate stats.
"""

import os
import time
import logging
import numpy as np
import cv2
from dataclasses import dataclass, field
from typing import List, Optional, Callable

from ml_pipeline.config import (
    PROGRESS_LOG_INTERVAL,
    FRAME_SAMPLE_FPS,
    FRAME_SAMPLE_FPS_PRACTICE,
    COURT_LENGTH_M,
    COURT_WIDTH_SINGLES_M,
    BOUNCE_MIN_DIRECTION_CHANGE,
    COURT_DETECTION_INTERVAL_PRACTICE,
    PLAYER_DETECTION_INTERVAL_PRACTICE,
    MOG2_HISTORY,
    MOG2_VAR_THRESHOLD,
    MOG2_DETECT_SHADOWS,
    MOG2_LEARNING_RATE,
    MOG2_DOWNSCALE,
    BALL_TRACKER,
    PIPELINE_STAGE_OVERLAP,
)
from ml_pipeline.video_preprocessor import VideoPreprocessor, VideoMetadata
from ml_pipeline.court_detector import CourtDetector
from ml_pipeline.ball_tracker import BallTracker, BallDetection
from ml_pipeline.player_tracker import PlayerTracker, PlayerDetection


def _make_ball_tracker(device: str):
    """Pick the ball tracker class based on the BALL_TRACKER env var.

    Both classes expose the same interface (detect_frame, interpolate_gaps,
    _filter_outliers, detect_bounces, compute_speeds, assign_peak_flight_speeds,
    log_diagnostics, reset, self.detections). WASB was validated as a drop-in
    on the ball-bench (see ml_pipeline/diag/bench_ball_baseline.json) before
    being made selectable here.
    """
    name = BALL_TRACKER
    if name == "wasb":
        from ml_pipeline.wasb_ball_tracker import WASBBallTracker
        logger.info("Ball tracker: WASB (BALL_TRACKER=%s)", name)
        return WASBBallTracker(device=device)
    if name not in ("tracknet_v2", "tracknet", ""):
        logger.warning(
            "Unknown BALL_TRACKER=%r — falling back to tracknet_v2",
            name,
        )
    logger.info("Ball tracker: TrackNetV2 (BALL_TRACKER=%s)", name or "tracknet_v2")
    return BallTracker(device=device)

logger = logging.getLogger(__name__)

# Pipeline stages with approximate progress percentages
PIPELINE_STAGES = [
    ("downloading",          5),
    ("extracting_frames",   10),
    ("detecting_court",     20),
    ("tracking_ball",       50),
    ("tracking_players",    70),
    ("computing_analytics", 80),
    ("generating_heatmaps", 85),
    ("transcoding",         90),
    ("saving_results",      95),
    ("complete",           100),
]


@dataclass
class AnalysisResult:
    """Structured output from the full pipeline."""
    # Metadata
    video_path: str = ""
    video_metadata: Optional[VideoMetadata] = None
    total_frames_processed: int = 0
    processing_time_sec: float = 0.0
    ms_per_frame: float = 0.0

    # Court
    court_detected: bool = False
    court_confidence: float = 0.0
    court_used_fallback: bool = False

    # Detections (raw)
    ball_detections: List[BallDetection] = field(default_factory=list)
    player_detections: List[PlayerDetection] = field(default_factory=list)

    # Ball aggregate stats
    ball_detection_rate: float = 0.0       # fraction of frames with ball detected
    bounce_count: int = 0
    bounces_in: int = 0
    bounces_out: int = 0
    max_speed_kmh: float = 0.0
    avg_speed_kmh: float = 0.0

    # Rally / serve analysis
    rally_count: int = 0
    avg_rally_length: float = 0.0          # average bounces per rally
    serve_count: int = 0
    first_serve_pct: float = 0.0

    # Player stats
    player_count: int = 0                  # how many distinct players detected

    # Errors
    frame_errors: int = 0


class TennisAnalysisPipeline:
    def __init__(self, device: str = None,
                 progress_callback: Callable[[str, int], None] = None,
                 practice: bool = False):
        """
        Args:
            device: 'cuda' or 'cpu'
            progress_callback: optional fn(stage: str, progress_pct: int) called at each stage
            practice: if True, use optimised settings (lower FPS, less frequent detection)
        """
        self.device = device or ("cuda" if __import__("torch").cuda.is_available() else "cpu")
        self._progress_cb = progress_callback
        self.practice = practice
        self.target_fps = FRAME_SAMPLE_FPS_PRACTICE if practice else FRAME_SAMPLE_FPS
        logger.info(f"Initialising pipeline on device: {self.device} (practice={practice}, fps={self.target_fps})")
        self.court_detector = CourtDetector(device=self.device)
        self.ball_tracker = _make_ball_tracker(self.device)
        self.player_tracker = PlayerTracker(device=self.device)

        # MOG2 background subtractor — separates moving players from static
        # spectators. Fed every frame; foreground mask passed to player tracker
        # for motion-based scoring in _choose_two_players.
        self._bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=MOG2_HISTORY,
            varThreshold=MOG2_VAR_THRESHOLD,
            detectShadows=MOG2_DETECT_SHADOWS,
        )

        # TASK 1: CPU/GPU stage overlap. When PIPELINE_STAGE_OVERLAP is on we
        # run the CPU-bound MOG2 motion-mask of frame N on a single bounded
        # worker thread CONCURRENTLY with the GPU-bound court + ball stages of
        # the SAME frame N, then join before the player stage (the only
        # consumer of the mask). One worker = strict frame ordering: exactly
        # one MOG2 apply() is ever in flight, fed in frame order, joined every
        # frame — so the background-subtractor state mutates in the identical
        # sequence as the synchronous path and motion_mask(N) is byte-identical.
        # MOG2's cv2 apply() is C++ and releases the GIL, so the thread overlaps
        # the GPU kernel launch + CUDA work instead of time-slicing under it.
        self._stage_overlap = bool(PIPELINE_STAGE_OVERLAP)
        self._mog2_executor = None
        if self._stage_overlap:
            from concurrent.futures import ThreadPoolExecutor
            # max_workers=1: the join-every-frame contract already serialises
            # MOG2 calls; a single worker keeps the background-model update
            # order deterministic and avoids any cross-frame race on the
            # subtractor's internal state.
            self._mog2_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="mog2",
            )
            logger.info(
                "Pipeline stage overlap ENABLED (PIPELINE_STAGE_OVERLAP=1): "
                "MOG2 motion-mask runs concurrently with court+ball per frame"
            )

        # Apply practice-mode intervals
        if practice:
            from ml_pipeline import config
            self.court_detector._detect_interval = COURT_DETECTION_INTERVAL_PRACTICE
            self.player_tracker._detect_interval = PLAYER_DETECTION_INTERVAL_PRACTICE

        # Per-stage wall-clock accumulators (B1 instrumentation). Used to
        # figure out which stage is the optimisation bottleneck — B2-B6
        # choose where to cut without guessing. The stage names match the
        # four calls inside _process_frame plus the post-processing block.
        self._stage_seconds: dict = {
            "court": 0.0,
            "ball": 0.0,
            "motion_mask": 0.0,
            "player": 0.0,
            "postprocess": 0.0,
            # TASK 1 observability: true MOG2 compute time. In sequential mode
            # this stays 0 (the inline apply is charged to "motion_mask"). In
            # overlap mode "motion_mask" holds only the residual join wait and
            # this holds the real (overlapped) MOG2 cost — the gap between them
            # is the wall-clock the overlap hid behind the GPU stages.
            "motion_mask_compute": 0.0,
        }

    def _report_progress(self, stage: str, pct: int = None):
        """Report progress to callback and log."""
        if pct is None:
            pct = dict(PIPELINE_STAGES).get(stage, 0)
        logger.info(f"Stage: {stage} ({pct}%)")
        if self._progress_cb:
            try:
                self._progress_cb(stage, pct)
            except Exception as e:
                logger.warning(f"Progress callback failed: {e}")

    def _log_stage_timings(self, frame_idx: int, final: bool = False):
        """Emit per-stage timing summary.

        Interim call (final=False) prints cumulative totals so far and
        ms-per-frame averaged over frames processed. Final call prints the
        same plus the grand total and each stage's share of it — the
        single most useful number for deciding where to optimise next.
        """
        totals = self._stage_seconds
        # motion_mask_compute is an OBSERVABILITY counter (TASK 1) that overlaps
        # the GPU stages in overlap mode — it is NOT a sequential wall-clock
        # contributor, so exclude it from the grand total / share maths to keep
        # the percentages meaningful. It is still printed separately below.
        WALL = ("court", "ball", "motion_mask", "player", "postprocess")
        grand = sum(totals.get(k, 0.0) for k in WALL)
        if grand <= 0 or frame_idx <= 0:
            return
        label = "FINAL" if final else f"@ frame {frame_idx}"
        parts = []
        for name in WALL:
            secs = totals.get(name, 0.0)
            ms_per = secs * 1000 / max(1, frame_idx)
            share = 100 * secs / grand if grand else 0
            parts.append(f"{name}={secs:.1f}s ({ms_per:.1f}ms/fr, {share:.0f}%)")
        logger.info("stage_timings %s [total=%.1fs]  %s", label, grand, "  ".join(parts))

        # TASK 1: surface how much MOG2 work was overlapped away. In overlap
        # mode motion_mask (residual join wait) << motion_mask_compute (true
        # MOG2 cost); the difference is the wall-clock the overlap hid behind
        # court+ball. In sequential mode motion_mask_compute is 0.
        mm_compute = totals.get("motion_mask_compute", 0.0)
        if mm_compute > 0:
            mm_residual = totals.get("motion_mask", 0.0)
            hidden = max(0.0, mm_compute - mm_residual)
            logger.info(
                "stage_overlap %s  mog2_compute=%.1fs  mog2_residual_wait=%.1fs  "
                "overlapped_hidden=%.1fs (%.1fms/fr saved)",
                label, mm_compute, mm_residual, hidden,
                hidden * 1000 / max(1, frame_idx),
            )

        # Player sub-stage breakdown — tells us whether SAHI, full-frame YOLO,
        # or scoring logic is the bottleneck inside the player stage.
        sub = getattr(self.player_tracker, "_sub_seconds", None)
        if sub:
            player_total = (
                sub["full_yolo"] + sub["sahi"] + sub["choose2"] + sub["other"]
            )
            if player_total > 0:
                sub_parts = []
                for name in ("full_yolo", "sahi", "choose2", "other"):
                    secs = sub[name]
                    share = 100 * secs / player_total
                    sub_parts.append(f"{name}={secs:.1f}s ({share:.0f}%)")
                skip = getattr(self.player_tracker, "_sahi_skip_count", 0)
                runs = getattr(self.player_tracker, "_sahi_run_count", 0)
                logger.info(
                    "player_sub %s [player_total=%.1fs  sahi_ran=%d sahi_skipped=%d]  %s",
                    label, player_total, runs, skip, "  ".join(sub_parts),
                )

    def process(self, video_path: str) -> AnalysisResult:
        """Run the full analysis pipeline on a video file."""
        t0 = time.time()
        result = AnalysisResult(video_path=video_path)

        self._report_progress("extracting_frames")

        # Setup video
        preprocessor = VideoPreprocessor(video_path, target_fps=self.target_fps)
        result.video_metadata = preprocessor.metadata
        expected_frames = preprocessor.frame_count_at_target_fps()
        logger.info(
            f"Video: {preprocessor.metadata.duration_sec:.1f}s, "
            f"{preprocessor.metadata.width}x{preprocessor.metadata.height}, "
            f"~{expected_frames} frames at {FRAME_SAMPLE_FPS}fps"
        )

        # Frame-by-frame processing — report stages based on frame progress
        frame_idx = 0
        court_reported = False
        ball_reported = False
        player_reported = False
        for frame in preprocessor.frames():
            try:
                self._process_frame(frame, frame_idx)
            except Exception as e:
                result.frame_errors += 1
                if result.frame_errors <= 5:
                    logger.warning(f"Frame {frame_idx} error: {e}")

            frame_idx += 1

            # Map frame progress to overall pipeline progress (10-80%)
            pct_done = frame_idx / expected_frames if expected_frames > 0 else 0
            overall_pct = 10 + int(pct_done * 70)

            if frame_idx % PROGRESS_LOG_INTERVAL == 0:
                elapsed = time.time() - t0
                fps_actual = frame_idx / elapsed if elapsed > 0 else 0
                self._report_progress("processing", overall_pct)
                logger.info(f"Progress: {frame_idx}/{expected_frames} frames ({fps_actual:.1f} fps)")
                # Per-stage timing snapshot — lets us see which stage is
                # eating wall-clock as the run progresses (e.g. player
                # tracker slowing down once MOG2 background stabilises).
                self._log_stage_timings(frame_idx)

        result.total_frames_processed = frame_idx
        logger.info(f"Frame processing complete: {frame_idx} frames, {result.frame_errors} errors")
        self._log_stage_timings(frame_idx, final=True)

        # TASK 1: tear down the MOG2 worker — the per-frame loop is done and
        # postprocess never touches the motion mask. The single-worker queue is
        # already empty (we joined every frame), so this returns promptly. Guard
        # so a re-run via reset() can re-create it.
        if self._mog2_executor is not None:
            self._mog2_executor.shutdown(wait=True)
            self._mog2_executor = None

        # Drain any batched ball-detection backlog before post-processing reads
        # ball_tracker.detections (no-op unless BALL_BATCH_SIZE>1 on WASB).
        self.ball_tracker.flush()
        # L1: drain the player-stage batch queue too (no-op when
        # PLAYER_BATCH_SIZE==1). Mirrors the ball_tracker pattern — the
        # postprocess block below reads player_tracker.detections, so the
        # partial batch from the end of the loop must land before then.
        self.player_tracker.flush()

        # Post-processing
        self._report_progress("computing_analytics")
        t_post = time.perf_counter()
        self._postprocess(result)
        self._stage_seconds["postprocess"] += time.perf_counter() - t_post

        result.processing_time_sec = time.time() - t0
        result.ms_per_frame = (result.processing_time_sec * 1000 / frame_idx) if frame_idx > 0 else 0
        logger.info(
            f"Pipeline complete in {result.processing_time_sec:.1f}s "
            f"({result.ms_per_frame:.1f} ms/frame)"
        )
        return result

    def _make_motion_mask(self, frame: np.ndarray) -> np.ndarray:
        """Compute the MOG2 foreground mask for one frame.

        Extracted so the overlap path (TASK 1) can run it on the worker thread
        while court + ball occupy the GPU. Identical call to the inline
        sequential version — same subtractor, same learningRate. The single
        worker + join-every-frame contract guarantees frame-ordered state
        mutation, so the returned mask is byte-identical to the synchronous
        path. The compute cost is accumulated into `motion_mask_compute` purely
        for observability — in overlap mode `_stage_seconds["motion_mask"]`
        only captures the residual join wait (the part NOT hidden behind the
        GPU), so this separate counter lets the stage-timing log show how much
        MOG2 work was actually overlapped away.
        """
        import time as _t
        _s = _t.perf_counter()
        mask = self._apply_mog2(frame)
        self._stage_seconds["motion_mask_compute"] += _t.perf_counter() - _s
        return mask

    def _apply_mog2(self, frame: np.ndarray) -> np.ndarray:
        """MOG2 foreground apply, with optional MOG2_DOWNSCALE (pure compute, no
        timing — callers own the stage counters).

        When MOG2_DOWNSCALE > 1 (env-gated, default 1 = full-res = unchanged),
        runs MOG2 on a 1/N-scaled frame then upscales the mask back to full
        resolution with NEAREST so 0/255 stays binary (no grey edges that would
        dilute the foreground fraction near the 0.03 threshold). The mask's only
        consumer reads a downscale-invariant foreground-pixel FRACTION over each
        full-res bbox, so the moving/stationary decision is preserved while
        MOG2.apply() runs on ~1/N^2 the pixels. Every frame uses the same path
        (MOG2_DOWNSCALE is constant), so the background model stays internally
        consistent. Shared by the inline and overlap-worker paths."""
        if MOG2_DOWNSCALE > 1:
            h, w = frame.shape[:2]
            small = cv2.resize(
                frame, (w // MOG2_DOWNSCALE, h // MOG2_DOWNSCALE),
                interpolation=cv2.INTER_AREA,
            )
            small_mask = self._bg_subtractor.apply(
                small, learningRate=MOG2_LEARNING_RATE,
            )
            return cv2.resize(small_mask, (w, h), interpolation=cv2.INTER_NEAREST)
        return self._bg_subtractor.apply(frame, learningRate=MOG2_LEARNING_RATE)

    def _process_frame(self, frame: np.ndarray, frame_idx: int):
        """Process a single frame through all three models."""
        # Keep the raw (distorted) frame. Detectors operate in raw pixel
        # space; projection to metres via court_detector.to_court_coords
        # applies per-point undistortion internally. This keeps all pixel-
        # space geometry (court polygon, debug bboxes, motion masks) aligned.
        pc = time.perf_counter

        # TASK 1 overlap path: dispatch the CPU MOG2 of THIS frame to the
        # worker FIRST, so it runs while court + ball occupy the GPU, then join
        # before the player stage. The motion_mask is identical because the
        # single worker applies exactly one frame at a time in frame order and
        # we join every frame (see _make_motion_mask / __init__ comment).
        mog2_future = None
        if self._stage_overlap:
            mog2_future = self._mog2_executor.submit(self._make_motion_mask, frame)

        try:
            # 1. Court detection (runs every N frames, cached otherwise)
            t = pc()
            court = self.court_detector.detect(frame, frame_idx)
            self._stage_seconds["court"] += pc() - t

            # 2. Ball tracking
            t = pc()
            self.ball_tracker.detect_frame(frame, frame_idx)
            self._stage_seconds["ball"] += pc() - t
        except Exception:
            # If court/ball raises in overlap mode, still DRAIN the MOG2 future
            # so the single-worker queue can't deadlock the next frame's
            # submit(). The mask is discarded (the player stage won't run for
            # this errored frame — same as the sequential path, which skips
            # steps 3-4 on a court/ball exception via the caller's try/except).
            if mog2_future is not None:
                try:
                    mog2_future.result()
                except Exception:
                    pass
            raise

        # 3. MOG2 foreground mask — feed every frame so the background model
        #    learns. The mask is passed to player tracker for motion scoring.
        #
        #    Two stacked, env-gated optimisations (both default OFF = unchanged):
        #    - PIPELINE_STAGE_OVERLAP: the apply() already ran (or is finishing)
        #      on the worker thread; .result() returns it and the accumulated
        #      "motion_mask" time becomes the RESIDUAL join wait (≈0 when MOG2
        #      finished during the GPU stages — that residual is exactly the
        #      wall-clock saving). True compute cost is logged separately in
        #      motion_mask_compute (set inside _make_motion_mask) for observability.
        #    - MOG2_DOWNSCALE: _apply_mog2 runs MOG2 on a 1/N-scaled frame and
        #      upscales the mask (NEAREST, binary-preserving). The mask's only
        #      consumer (_compute_motion_ratio -> _choose_two_players) reads a
        #      downscale-invariant foreground-pixel FRACTION over each full-res
        #      bbox, so the moving/stationary decision is preserved while
        #      MOG2.apply() runs on ~1/N^2 the pixels. Applies on BOTH the worker
        #      (overlap) and inline paths via the shared _apply_mog2 helper.
        t = pc()
        if mog2_future is not None:
            motion_mask = mog2_future.result()
        else:
            motion_mask = self._apply_mog2(frame)
        self._stage_seconds["motion_mask"] += pc() - t

        # 4. Player tracking (with motion mask + court geometry for scoring)
        t = pc()
        court_bbox = self.court_detector.get_court_bbox_pixels()
        court_corners = self.court_detector.get_court_corners_pixels()
        self.player_tracker.detect_frame(
            frame, frame_idx, court_bbox=court_bbox, motion_mask=motion_mask,
            court_corners=court_corners,
            to_court_coords=self.court_detector.to_court_coords,
            to_pixel_coords=self.court_detector.to_pixel_coords,
        )
        self._stage_seconds["player"] += pc() - t

    def _postprocess(self, result: AnalysisResult):
        """Run interpolation, bounce detection, speed calc, and aggregate stats."""
        # Ball post-processing
        self.ball_tracker.log_diagnostics()
        self.ball_tracker.interpolate_gaps()
        self.ball_tracker.detect_bounces(court_detector=self.court_detector)
        self.ball_tracker.compute_speeds(court_detector=self.court_detector, fps=self.target_fps)
        # Replace the per-bounce speed_kmh with peak flight speed in the
        # preceding window, matching SportAI's "ball speed at hit" semantic.
        # Non-bounce detections retain their pairwise frame-to-frame speeds.
        self.ball_tracker.assign_peak_flight_speeds()
        result.ball_detections = self.ball_tracker.detections

        # Player post-processing
        self.player_tracker.log_diagnostics()
        self.player_tracker.map_to_court(self.court_detector)
        # Temporal stability filter: reject "players" whose pixel position
        # barely changes over time (these are ball persons / spectators /
        # fixed objects, not real moving players).
        self._filter_stationary_players()
        result.player_detections = self.player_tracker.detections

        # Court stats
        last_court = self.court_detector._last_detection
        if last_court is not None:
            result.court_detected = last_court.confidence > 0
            result.court_confidence = last_court.confidence
            result.court_used_fallback = last_court.used_fallback

        # Ball stats
        n_frames = result.total_frames_processed
        n_ball = len(result.ball_detections)
        result.ball_detection_rate = n_ball / n_frames if n_frames > 0 else 0

        bounces = [d for d in result.ball_detections if d.is_bounce]
        result.bounce_count = len(bounces)
        result.bounces_in = sum(1 for b in bounces if b.is_in is True)
        result.bounces_out = sum(1 for b in bounces if b.is_in is False)

        # Speed aggregates: exclude slow ball rolls (mis-hits, warmup, post-point
        # ball bouncing, ball rolling on court). A real tennis shot is >= 30 km/h
        # at the slowest (soft drop shots, short approaches). Anything below is
        # almost certainly ball-in-transit-not-in-play and dilutes the average.
        MIN_REAL_SHOT_KMH = 30.0
        real_speeds = [
            d.speed_kmh for d in result.ball_detections
            if d.speed_kmh is not None and d.speed_kmh >= MIN_REAL_SHOT_KMH
        ]
        # Max speed: use all non-zero speeds (max shouldn't be affected by slow balls)
        all_speeds = [d.speed_kmh for d in result.ball_detections if d.speed_kmh is not None and d.speed_kmh > 0]
        result.max_speed_kmh = max(all_speeds) if all_speeds else 0
        result.avg_speed_kmh = float(np.mean(real_speeds)) if real_speeds else 0

        # Rally analysis: a rally = sequence of bounces separated by < BOUNCE_MIN_DIRECTION_CHANGE frames
        if bounces:
            rallies = self._compute_rallies(bounces)
            result.rally_count = len(rallies)
            result.avg_rally_length = float(np.mean([len(r) for r in rallies])) if rallies else 0
            # Serves: first bounce of each rally
            result.serve_count = len(rallies)
            # First serve %: bounces that land in (simplified — first bounce of rally is a serve)
            first_bounces = [r[0] for r in rallies if r]
            serves_in = sum(1 for b in first_bounces if b.is_in is True)
            result.first_serve_pct = (serves_in / len(first_bounces) * 100) if first_bounces else 0

        # Swing-type classification (both players) via optical flow → bronze
        # stroke_class. Silver Pass 1 projects this verbatim into swing_type.
        self._classify_far_player_strokes(result)

        # Player stats
        pids = set(d.player_id for d in result.player_detections)
        result.player_count = len(pids)

    def _filter_stationary_players(self) -> None:
        """Reject 'players' whose pixel position is nearly stationary over time.

        Two-signal rejection — reject if EITHER is true:

        1. STD-DEV filter (original): both std_x and std_y below 50 px. Catches
           objects that are fully planted (scoreboards, fixed cameras).
        2. PATH-LENGTH filter (new): cumulative pixel distance between
           consecutive detections below MIN_PATH_LENGTH_PX. A real tennis
           player accumulates tens of thousands of pixels of travel across
           a match. A bench sitter / ball person accumulates very little
           even if they occasionally stand up or shift (which can spoof
           the std filter since std only captures spread, not total motion).

        The path-length filter exists because of the bench-sitter failure mode
        documented in the player_tracker patch: a pid slot occasionally
        occupied by a non-player can have high std (because the REAL player
        occupies the same slot most of the time) but the *subset* of frames
        that belong to the bench sitter still contributes very little total
        path length. We can't easily partition the slot per-identity without
        a real tracker, so path length is a coarse but directionally correct
        backstop.

        Threshold tuned for 1080p video — a real player's centers vary by
        100s of pixels per frame transition; a stationary person varies by
        < 5 pixels. Over ~5000 detection frames in a 10-min match, a real
        player's path length is typically > 30,000 px.
        """
        from collections import defaultdict
        import numpy as _np

        STATIONARY_STD_PX = 50         # both x and y below this → stationary
        MIN_PATH_LENGTH_PX = 10000     # cumulative path length across match

        if not self.player_tracker.detections:
            return

        # Group by (pid, frame_idx) and keep only unique frames per pid, then
        # sort so path length reflects temporal motion.
        groups = defaultdict(list)
        for d in self.player_tracker.detections:
            cx, cy = d.center
            groups[d.player_id].append((d.frame_idx, float(cx), float(cy)))

        rejected_ids = set()
        for pid, entries in groups.items():
            if len(entries) < 5:
                continue  # not enough samples to judge
            entries.sort(key=lambda e: e[0])
            xs = _np.array([e[1] for e in entries])
            ys = _np.array([e[2] for e in entries])
            std_x = float(_np.std(xs))
            std_y = float(_np.std(ys))
            dx = _np.diff(xs)
            dy = _np.diff(ys)
            path_len = float(_np.sum(_np.sqrt(dx * dx + dy * dy)))

            stationary_by_std = (std_x < STATIONARY_STD_PX and std_y < STATIONARY_STD_PX)
            stationary_by_path = (path_len < MIN_PATH_LENGTH_PX)

            if stationary_by_std or stationary_by_path:
                reason = []
                if stationary_by_std:
                    reason.append("std")
                if stationary_by_path:
                    reason.append("path")
                logger.info(
                    "_filter_stationary_players: REJECT pid=%s n=%d "
                    "std_x=%.1f std_y=%.1f path_len=%.0f reason=%s",
                    pid, len(entries), std_x, std_y, path_len, "+".join(reason),
                )
                rejected_ids.add(pid)
            else:
                logger.info(
                    "_filter_stationary_players: KEEP pid=%s n=%d "
                    "std_x=%.1f std_y=%.1f path_len=%.0f",
                    pid, len(entries), std_x, std_y, path_len,
                )

        if rejected_ids:
            self.player_tracker.detections = [
                d for d in self.player_tracker.detections
                if d.player_id not in rejected_ids
            ]
            logger.info(
                "_filter_stationary_players: removed %d player_ids, %d detections remain",
                len(rejected_ids), len(self.player_tracker.detections),
            )

    def _classify_far_player_strokes(self, result: AnalysisResult):
        """Classify swing type (forehand/backhand/overhead) per hit and write it
        to PlayerDetection.stroke_class — a BRONZE fact consumed by silver Pass 1.

        Delegates to the ADR-02 v2 classifier (SwingTypeR2plus1D, optical-flow
        R(2+1)D-18). Covers BOTH players now (the old path was far-only). All the
        heavy lifting + STOPGAP handling lives in
        stroke_classifier/inference_v2.classify_strokes_v2; this method just
        wires in the pipeline's video, frame-space and device. Gracefully no-ops
        when the v2 weights are absent (silver pose/position heuristic stays the
        live fallback) or no bounces were detected.

        SWING_CLASSIFIER_MIN_CONF (env, default 0.5) gates which predictions are
        written; SWING_CLASSIFIER_ENABLED=0 disables the model entirely (rollback
        without a rebuild — the env_var_rollback_pattern).
        """
        if os.environ.get("SWING_CLASSIFIER_ENABLED", "1") not in ("1", "true", "True"):
            logger.info("SWING_CLASSIFIER_ENABLED=0 — skipping swing classification")
            return
        try:
            from ml_pipeline.stroke_classifier.inference_v2 import classify_strokes_v2
        except ImportError as e:
            logger.info("swing_classifier_v2 import failed (%s) — skipping", e)
            return

        try:
            min_conf = float(os.environ.get("SWING_CLASSIFIER_MIN_CONF", "0.5"))
        except ValueError:
            min_conf = 0.5

        classify_strokes_v2(
            result,
            target_fps=self.target_fps,
            device=self.device,
            min_conf=min_conf,
        )

    def _compute_rallies(self, bounces: List[BallDetection]) -> List[List[BallDetection]]:
        """Split bounces into rallies. A gap > BOUNCE_MIN_DIRECTION_CHANGE frames starts a new rally."""
        rallies = []
        current_rally = [bounces[0]]
        for i in range(1, len(bounces)):
            gap = bounces[i].frame_idx - bounces[i - 1].frame_idx
            if gap > BOUNCE_MIN_DIRECTION_CHANGE:
                rallies.append(current_rally)
                current_rally = [bounces[i]]
            else:
                current_rally.append(bounces[i])
        if current_rally:
            rallies.append(current_rally)
        return rallies

    def reset(self):
        """Reset all trackers for a new video."""
        self.ball_tracker.reset()
        self.player_tracker.reset()
        self.court_detector._last_detection = None
        self.court_detector._last_frame_idx = -30
        self._bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=MOG2_HISTORY,
            varThreshold=MOG2_VAR_THRESHOLD,
            detectShadows=MOG2_DETECT_SHADOWS,
        )
        # TASK 1: re-arm the MOG2 worker for the next process() if overlap is
        # enabled (process() shuts it down at the end of the frame loop). Reset
        # the observability counter too so the per-run log starts clean.
        if self._stage_overlap and self._mog2_executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._mog2_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="mog2",
            )
        self._stage_seconds["motion_mask_compute"] = 0.0
