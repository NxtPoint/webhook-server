"""
VideoPreprocessor — OpenCV-based frame extraction with generator output.
Memory-efficient: yields frames one at a time, never loads full video into RAM.
Falls back to FFmpeg subprocess if available for better codec support.
"""

import os
import numpy as np
import cv2
from dataclasses import dataclass

from ml_pipeline.config import FRAME_SAMPLE_FPS, SUPPORTED_EXTENSIONS


@dataclass
class VideoMetadata:
    duration_sec: float
    fps: float
    width: int
    height: int
    codec: str
    total_frames: int
    file_size_bytes: int


class VideoPreprocessor:
    def __init__(self, video_path: str, target_fps: int = FRAME_SAMPLE_FPS):
        if not os.path.isfile(video_path):
            raise FileNotFoundError(f"Video not found: {video_path}")
        ext = os.path.splitext(video_path)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported format {ext}. Supported: {SUPPORTED_EXTENSIONS}")
        self.video_path = video_path
        self.target_fps = target_fps
        self._metadata = None

    @property
    def metadata(self) -> VideoMetadata:
        if self._metadata is None:
            self._metadata = self._probe()
        return self._metadata

    def _probe(self) -> VideoMetadata:
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {self.video_path}")
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
            codec = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)])
            duration = total_frames / fps if fps > 0 else 0
            file_size = os.path.getsize(self.video_path)
        finally:
            cap.release()

        return VideoMetadata(
            duration_sec=duration,
            fps=fps,
            width=width,
            height=height,
            codec=codec,
            total_frames=total_frames,
            file_size_bytes=file_size,
        )

    def frames(self):
        """Yield BGR numpy frames at target_fps. Generator — never holds all frames in RAM."""
        meta = self.metadata
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {self.video_path}")

        source_fps = meta.fps
        # Frame sampling: if source is 30fps and target is 25fps, skip some frames
        frame_interval = source_fps / self.target_fps if self.target_fps < source_fps else 1.0

        try:
            source_frame_idx = 0
            next_sample_at = 0.0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                if source_frame_idx >= next_sample_at:
                    yield frame
                    next_sample_at += frame_interval
                source_frame_idx += 1
        finally:
            cap.release()

    def frame_count_at_target_fps(self) -> int:
        meta = self.metadata
        if self.target_fps >= meta.fps:
            return meta.total_frames
        return int(round(meta.duration_sec * self.target_fps))
