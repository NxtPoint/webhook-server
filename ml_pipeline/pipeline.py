"""
TennisAnalysisPipeline — Orchestrates all three ML models frame-by-frame.
Produces a structured AnalysisResult with detections + aggregate stats.
"""

import time
import logging
import numpy as np
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
)
from ml_pipeline.video_preprocessor import VideoPreprocessor, VideoMetadata
from ml_pipeline.court_detector import CourtDetector
from ml_pipeline.ball_tracker import BallTracker, BallDetection
from ml_pipeline.player_tracker import PlayerTracker, PlayerDetection

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
        self.ball_tracker = BallTracker(device=self.device)
        self.player_tracker = PlayerTracker(device=self.device)

        # Apply practice-mode intervals
        if practice:
            from ml_pipeline import config
            self.court_detector._detect_interval = COURT_DETECTION_INTERVAL_PRACTICE
            self.player_tracker._detect_interval = PLAYER_DETECTION_INTERVAL_PRACTICE

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

        result.total_frames_processed = frame_idx
        logger.info(f"Frame processing complete: {frame_idx} frames, {result.frame_errors} errors")

        # Post-processing
        self._report_progress("computing_analytics")
        self._postprocess(result)

        result.processing_time_sec = time.time() - t0
        result.ms_per_frame = (result.processing_time_sec * 1000 / frame_idx) if frame_idx > 0 else 0
        logger.info(
            f"Pipeline complete in {result.processing_time_sec:.1f}s "
            f"({result.ms_per_frame:.1f} ms/frame)"
        )
        return result

    def _process_frame(self, frame: np.ndarray, frame_idx: int):
        """Process a single frame through all three models."""
        # 1. Court detection (runs every N frames, cached otherwise)
        court = self.court_detector.detect(frame, frame_idx)

        # 2. Ball tracking
        self.ball_tracker.detect_frame(frame, frame_idx)

        # 3. Player tracking
        court_bbox = self.court_detector.get_court_bbox_pixels()
        self.player_tracker.detect_frame(frame, frame_idx, court_bbox=court_bbox)

    def _postprocess(self, result: AnalysisResult):
        """Run interpolation, bounce detection, speed calc, and aggregate stats."""
        # Ball post-processing
        self.ball_tracker.interpolate_gaps()
        self.ball_tracker.detect_bounces(court_detector=self.court_detector)
        self.ball_tracker.compute_speeds(court_detector=self.court_detector)
        result.ball_detections = self.ball_tracker.detections

        # Player post-processing
        self.player_tracker.map_to_court(self.court_detector)
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

        speeds = [d.speed_kmh for d in result.ball_detections if d.speed_kmh is not None and d.speed_kmh > 0]
        result.max_speed_kmh = max(speeds) if speeds else 0
        result.avg_speed_kmh = float(np.mean(speeds)) if speeds else 0

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

        # Player stats
        pids = set(d.player_id for d in result.player_detections)
        result.player_count = len(pids)

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
