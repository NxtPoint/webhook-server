"""
PlayerTracker — YOLOv8-pose person detection + court filtering + IoU tracking.
Assigns consistent player_id (0 = near-side, 1 = far-side) across frames.
Extracts 17 COCO body keypoints per player for stroke classification.
"""

import logging
import os
import numpy as np
from ultralytics import YOLO
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Callable, Tuple

import cv2

from ml_pipeline.config import (
    YOLO_WEIGHTS,
    YOLO_POSE_WEIGHTS,
    YOLO_POSE_WEIGHTS_FALLBACK,
    YOLO_CONFIDENCE,
    YOLO_COURT_CROP_CONFIDENCE,
    YOLO_IMGSZ,
    YOLO_COURT_CROP_INFERENCE,
    YOLO_COURT_CROP_MARGIN_PX,
    YOLO_PERSON_CLASS_ID,
    PLAYER_IOU_THRESHOLD,
    PLAYER_MAX_CENTER_DRIFT_PX,
    PLAYER_TRACK_TIMEOUT_FRAMES,
    PLAYER_COURT_MARGIN_PX,
    PLAYER_OUTSIDE_COURT_MARGIN_PX,
    PLAYER_DETECTION_INTERVAL,
    DEBUG_FRAME_INTERVAL,
    MOG2_MIN_MOTION_RATIO,
    MOG2_MOTION_SCORE_WEIGHT,
    SAHI_ENABLED,
    SAHI_SLICE_HEIGHT,
    SAHI_SLICE_WIDTH,
    SAHI_OVERLAP_RATIO,
    SAHI_CONFIDENCE,
    SAHI_POSTPROCESS_TYPE,
    SAHI_POSTPROCESS_MATCH_THRESHOLD,
    COURT_LENGTH_M,
    COURT_WIDTH_DOUBLES_M,
    COURT_WIDTH_SINGLES_M,
    SERVICE_BOX_DEPTH_M,
)

# SAHI — lazy import to avoid startup cost when disabled
_sahi_detection_model = None

DEBUG_FRAMES_DIR = "/tmp/debug_frames"

logger = logging.getLogger(__name__)

# COCO keypoint indices (17 keypoints)
KP_NOSE = 0
KP_LEFT_EYE = 1; KP_RIGHT_EYE = 2
KP_LEFT_EAR = 3; KP_RIGHT_EAR = 4
KP_LEFT_SHOULDER = 5; KP_RIGHT_SHOULDER = 6
KP_LEFT_ELBOW = 7; KP_RIGHT_ELBOW = 8
KP_LEFT_WRIST = 9; KP_RIGHT_WRIST = 10
KP_LEFT_HIP = 11; KP_RIGHT_HIP = 12
KP_LEFT_KNEE = 13; KP_RIGHT_KNEE = 14
KP_LEFT_ANKLE = 15; KP_RIGHT_ANKLE = 16


@dataclass
class PlayerDetection:
    frame_idx: int
    player_id: int          # 0 = near-side player, 1 = far-side player
    bbox: tuple             # (x1, y1, x2, y2) pixel coordinates
    center: tuple           # (cx, cy) pixel center
    court_x: Optional[float] = None  # metres
    court_y: Optional[float] = None  # metres
    keypoints: Optional[np.ndarray] = field(default=None, repr=False)
    # keypoints: (17, 3) array — x, y, confidence per COCO keypoint
    stroke_class: Optional[str] = None  # optical flow classification for far player
    # Which detection path produced this row. Lets SQL distinguish pose-
    # carrying full-frame YOLO detections from SAHI fragments or detection-
    # only fallback. Values: "yolo_pose" | "yolo_det" | "sahi". None = legacy.
    detection_source: Optional[str] = None


class PlayerTracker:
    def __init__(self, weights_path: str = None, device: str = None):
        self.device = device or ("cuda:0" if __import__("torch").cuda.is_available() else "cpu")
        # Prefer the larger YOLOv8x-pose model, then fall back to yolov8m-pose,
        # then to plain yolov8m (detection-only).
        if weights_path is None:
            if os.path.exists(YOLO_POSE_WEIGHTS):
                weights_path = YOLO_POSE_WEIGHTS
                self.has_pose = True
                logger.info("Using YOLO pose model (preferred): %s", weights_path)
            elif os.path.exists(YOLO_POSE_WEIGHTS_FALLBACK):
                weights_path = YOLO_POSE_WEIGHTS_FALLBACK
                self.has_pose = True
                logger.info("Using YOLO pose model (fallback): %s", weights_path)
            else:
                weights_path = YOLO_WEIGHTS
                self.has_pose = False
                logger.info("No pose model found, using detection-only: %s", weights_path)
        else:
            self.has_pose = "pose" in weights_path
        self.model = YOLO(weights_path)
        # Detection-only model for far-baseline Pass 3. YOLOv8-pose suppresses
        # small detections (~30-40px) because keypoints can't be resolved at
        # that size. The detection-only model reliably detects people down to
        # ~20-30px. We only need bbox (not keypoints) for far-player assignment.
        self._det_model = None
        if os.path.exists(YOLO_WEIGHTS):
            self._det_model = YOLO(YOLO_WEIGHTS)
            logger.info("Loaded detection-only model for far-baseline pass: %s", YOLO_WEIGHTS)

        # SAHI tiled inference model — lazy init on first use.
        # Uses the detection-only model (yolov8m) for systematic small-object
        # detection via overlapping tiles. Replaces manual 3-pass when enabled.
        self._sahi_model = None
        if SAHI_ENABLED and os.path.exists(YOLO_WEIGHTS):
            try:
                from sahi import AutoDetectionModel
                self._sahi_model = AutoDetectionModel.from_pretrained(
                    model_type="yolov8",
                    model_path=YOLO_WEIGHTS,
                    confidence_threshold=SAHI_CONFIDENCE,
                    device=self.device,
                )
                logger.info("SAHI tiled inference enabled with %s", YOLO_WEIGHTS)
            except ImportError:
                logger.warning("SAHI not installed (pip install sahi), falling back to 3-pass")
            except Exception as e:
                logger.warning("SAHI init failed: %s, falling back to 3-pass", e)
        # player_id → (bbox, last-refreshed frame_idx). Storing the frame
        # lets us expire stale entries — without this a 10-second-old
        # bbox can silently match a new false positive.
        self._prev_players: Dict[int, Tuple[tuple, int]] = {}
        self.detections: List[PlayerDetection] = []
        self._last_result: List[PlayerDetection] = []
        self._detect_interval: int = PLAYER_DETECTION_INTERVAL
        self._last_detect_frame: int = -PLAYER_DETECTION_INTERVAL

        # Debug frame upload context — set by __main__.py for live S3 streaming
        self._debug_s3_client = None
        self._debug_s3_bucket = None
        self._debug_job_id = None

        # Diagnostics — counters only, reported via log_diagnostics(). Used to
        # diagnose the ball-boy / bench-sitter mis-mapping where a non-player
        # near the net gets locked into pid=1 because YOLO occasionally misses
        # the real far player. See commit log for detailed analysis.
        self._diag = {
            "frames_yolo_ran": 0,
            "candidates_total": 0,
            "candidates_hist": [0] * 7,          # buckets: 0, 1, 2, 3, 4, 5, 6+
            "choose2_kept_2": 0,
            "choose2_kept_1_span_fail": 0,       # 2+ cands, span too small → dropped top
            "choose2_kept_1_single": 0,          # only 1 candidate to begin with
            "choose2_kept_0": 0,
            "choose2_dropped_middle": 0,         # 3+ cands, dropped middle ones
        }
        # B1: wall-clock accumulators for detect_frame sub-stages. The
        # pipeline reads this dict in its stage-timing summary so we know
        # whether full-frame YOLO, SAHI, or the downstream scoring logic
        # dominates the player-stage budget. B3/B4 target SAHI specifically,
        # so this is the switch that tells us which is worth the work.
        self._sub_seconds: Dict[str, float] = {
            "full_yolo": 0.0,
            "sahi": 0.0,
            "choose2": 0.0,
            "other": 0.0,
        }
        self._sahi_skip_count: int = 0
        self._sahi_run_count: int = 0
        # B4+ diagnostics: track WHICH predicate fired the skip, and how often
        # each would-fire independently. Lets us tune the rule post-deploy
        # without needing to re-run the pipeline. A = spatial-coverage pose
        # test, B = metric far-baseline test (original rule, relaxed).
        self._sahi_skip_by_rule: Dict[str, int] = {
            "A_spatial_pose": 0,   # skipped because 2 pose-carrying candidates span both halves
            "B_metric_far":   0,   # skipped because a candidate projected near the far baseline
            "would_skip_A_only": 0,
            "would_skip_B_only": 0,
            "would_skip_both":   0,
        }

    def set_debug_upload_context(self, s3_client, s3_bucket: str, job_id: str) -> None:
        """Enable live debug frame upload to S3 (called from __main__.py)."""
        self._debug_s3_client = s3_client
        self._debug_s3_bucket = s3_bucket
        self._debug_job_id = job_id

    def detect_frame(
        self,
        frame: np.ndarray,
        frame_idx: int,
        court_bbox: Optional[tuple] = None,
        motion_mask: Optional[np.ndarray] = None,
        court_corners: Optional[list] = None,
        to_court_coords: Optional[Callable] = None,
        to_pixel_coords: Optional[Callable] = None,
    ) -> List[PlayerDetection]:
        """Detect players. Runs YOLO every N frames, reuses last result otherwise.

        Args:
            motion_mask: MOG2 foreground mask (same size as frame). 255 = foreground
                (moving), 0 = background (static). Used in _choose_two_players to
                prefer moving candidates over stationary ones in the far half.
            court_corners: 4 baseline corner pixel coords [(x,y),...] from court
                detector. Used for three-tier court-geometry scoring.
            to_court_coords: callable (pixel_x, pixel_y) -> (court_x, court_y)
                in metres, or None. When provided, candidate scoring uses
                court metres for the zone test instead of pixel-space
                approximations.
        """
        if (frame_idx - self._last_detect_frame) < self._detect_interval and self._last_result:
            # Reuse last detection with updated frame_idx
            reused = []
            for d in self._last_result:
                reused.append(PlayerDetection(
                    frame_idx=frame_idx, player_id=d.player_id,
                    bbox=d.bbox, center=d.center, keypoints=d.keypoints,
                ))
            self.detections.extend(reused)
            return reused
        self._last_detect_frame = frame_idx

        import time as _time
        _pc = _time.perf_counter
        _t_detect_start = _pc()
        _sub_before = (
            self._sub_seconds["full_yolo"]
            + self._sub_seconds["sahi"]
            + self._sub_seconds["choose2"]
        )

        # ── Detection strategy: SAHI (systematic) or manual 3-pass (legacy) ──
        if SAHI_ENABLED and self._sahi_model is not None:
            # Full-frame YOLO first — catches the near player (large, ~400px)
            # easily and occasionally the far player when they're visible.
            _t = _pc()
            full_boxes_list, full_kps_list = self._run_yolo(frame)
            self._sub_seconds["full_yolo"] += _pc() - _t

            # B4+ tightened skip rule — two independent predicates. Skip SAHI
            # if EITHER fires. Both are positive signals that full-frame YOLO
            # has already found both players (or at least the far player).
            #
            # Rule A — SPATIAL COVERAGE via pose: full-frame YOLOv8x-pose has
            # ≥1 pose-carrying candidate clearly in the near pixel half AND
            # ≥1 pose-carrying candidate clearly in the far pixel half. Pose
            # presence distinguishes real persons from tiny fragments. YOLOv8x-
            # pose has a ~60-80 px keypoint-resolution floor, so if it DID find
            # a far-half person with pose, the far player is definitely visible
            # and SAHI's ~300ms of tiled compute is redundant. Size gates (40 px
            # near, 20 px far) filter fragments. A ±5% vertical dead zone around
            # the midline prevents mid-court recovery positions from either half
            # spoofing the test.
            #
            # Rule B — METRIC FAR-BASELINE (prior rule, relaxed): calibration
            # projection of any candidate's feet lands near the far baseline.
            # Two changes from the prior implementation:
            #   1. Pass `strict=False` so candidates with projected y ≤ -5 m
            #      (calibration-extrapolation bias on the far image edge — the
            #      same widening A0 applies in tier-2 scoring) aren't silently
            #      dropped here. Without this the predicate almost never fired
            #      — exactly the symptom we're fixing.
            #   2. Widen the far-baseline band to [-10, 5] m, matching the
            #      tier-2 `behind_baseline` widening in _choose_two_players.
            skip_A = False   # spatial coverage via pose
            skip_B = False   # metric far-baseline
            frame_h = frame.shape[0]
            midline_y = frame_h / 2
            dead_zone = frame_h * 0.05  # ±5% of frame height around midline

            # Predicate A — count pose-carrying candidates per half with size gate
            has_near_pose = False
            has_far_pose = False
            for box, kp in zip(full_boxes_list, full_kps_list):
                if kp is None:
                    continue
                cy = (box[1] + box[3]) / 2
                bbox_h = box[3] - box[1]
                if cy > midline_y + dead_zone and bbox_h >= 40:
                    has_near_pose = True
                elif cy < midline_y - dead_zone and bbox_h >= 20:
                    has_far_pose = True
                if has_near_pose and has_far_pose:
                    break
            skip_A = has_near_pose and has_far_pose

            # Predicate B — metric far-baseline with strict=False
            if to_court_coords is not None:
                for box in full_boxes_list:
                    cx = (box[0] + box[2]) / 2
                    y2 = box[3]  # feet position
                    try:
                        pt = to_court_coords(cx, y2, strict=False)
                    except TypeError:
                        # Older CourtDetector without strict kwarg
                        try:
                            pt = to_court_coords(cx, y2)
                        except Exception:
                            pt = None
                    except Exception:
                        pt = None
                    if pt is not None and -10.0 <= pt[1] <= 5.0:
                        skip_B = True
                        break

            # Would-skip accounting (for post-run tuning diagnostics)
            if skip_A and skip_B:
                self._sahi_skip_by_rule["would_skip_both"] += 1
            elif skip_A:
                self._sahi_skip_by_rule["would_skip_A_only"] += 1
            elif skip_B:
                self._sahi_skip_by_rule["would_skip_B_only"] += 1

            skip_sahi = skip_A or skip_B

            if skip_sahi:
                sahi_boxes, sahi_kps = [], []
                self._sahi_skip_count += 1
                # Record which predicate fired (prefer A as primary since it's
                # the stronger signal — pose-confirmed both players visible).
                if skip_A:
                    self._sahi_skip_by_rule["A_spatial_pose"] += 1
                    reason = "pose-spanning-halves"
                else:
                    self._sahi_skip_by_rule["B_metric_far"] += 1
                    reason = "metric-far-baseline"
                if frame_idx % 150 == 0:
                    logger.info(
                        "sahi_skipped frame=%d reason=%s (A=%s B=%s)",
                        frame_idx, reason, skip_A, skip_B,
                    )
            else:
                # SAHI on court ROI only — crowd stands never contain players
                _t = _pc()
                sahi_boxes, sahi_kps = self._run_sahi(frame, court_bbox=court_bbox)
                self._sub_seconds["sahi"] += _pc() - _t
                self._sahi_run_count += 1

            all_boxes = full_boxes_list + sahi_boxes
            all_kps = full_kps_list + sahi_kps
        else:
            # ── Pass 1: Full-frame YOLO ──
            full_boxes_list, full_kps_list = self._run_yolo(frame)

            # ── Pass 2: Court-cropped + upscaled YOLO ──
            crop_boxes_list, crop_kps_list = [], []
            if YOLO_COURT_CROP_INFERENCE and court_bbox is not None:
                try:
                    crop_boxes_list, crop_kps_list = self._run_yolo_court_crop(frame, court_bbox)
                except Exception as e:
                    logger.warning("court-crop YOLO pass failed: %s", e)

            # ── Pass 3: Far-baseline dedicated crop ──
            far_boxes_list, far_kps_list = [], []
            try:
                far_boxes_list, far_kps_list = self._run_yolo_far_baseline(frame)
            except Exception as e:
                logger.warning("far-baseline YOLO pass failed: %s", e)

            all_boxes = full_boxes_list + crop_boxes_list + far_boxes_list
            all_kps = full_kps_list + crop_kps_list + far_kps_list

        # ── Deduplicate via IoU ──
        deduped_boxes, deduped_kps = self._dedupe_iou(all_boxes, all_kps, iou_thresh=0.5)
        n_yolo_boxes = len(deduped_boxes)
        # Log dedup details every 150 frames to diagnose far-player loss.
        # crop_boxes_list / far_boxes_list only exist in the legacy 3-pass
        # code path. When SAHI is enabled we have sahi_boxes instead.
        if frame_idx % 150 == 0:
            crop_n = len(crop_boxes_list) if 'crop_boxes_list' in locals() else 0
            far_n = len(far_boxes_list) if 'far_boxes_list' in locals() else 0
            sahi_n = len(sahi_boxes) if 'sahi_boxes' in locals() else 0
            logger.info(
                "dedup_detail frame=%d: full=%d sahi=%d crop=%d far=%d → deduped=%d",
                frame_idx, len(full_boxes_list), sahi_n, crop_n, far_n, len(deduped_boxes),
            )
            for bi, (bx1, by1, bx2, by2) in enumerate(deduped_boxes):
                cy = (by1 + by2) / 2
                logger.info("  deduped[%d] box=(%.0f,%.0f,%.0f,%.0f) cy=%.0f",
                           bi, bx1, by1, bx2, by2, cy)

        # ── Court area filter ──
        # Reject detections far from the court (ball persons, spectators, umpires).
        # Only applies when court_bbox is reliable (we now use _last_good_detection).
        candidates = []
        candidate_kps = []
        n_filtered_out = 0
        skip_court_filter = (court_bbox is None) or (PLAYER_OUTSIDE_COURT_MARGIN_PX >= 1000)
        for bi, (x1, y1, x2, y2) in enumerate(deduped_boxes):
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            if not skip_court_filter:
                cb_x1, cb_y1, cb_x2, cb_y2 = court_bbox
                margin = PLAYER_OUTSIDE_COURT_MARGIN_PX
                if not (cb_x1 - margin <= cx <= cb_x2 + margin and
                        cb_y1 - margin <= cy <= cb_y2 + margin):
                    n_filtered_out += 1
                    continue
            candidates.append((float(x1), float(y1), float(x2), float(y2)))
            candidate_kps.append(deduped_kps[bi])

        # Diagnostic logging — every 30 frames.
        # crop_boxes_list only exists in the legacy 3-pass code path; in the
        # SAHI path we have sahi_boxes instead. Fall back to 0 / sahi count.
        if frame_idx % 30 == 0:
            alt_n = (len(sahi_boxes) if 'sahi_boxes' in locals()
                     else len(crop_boxes_list) if 'crop_boxes_list' in locals()
                     else 0)
            alt_label = "sahi" if 'sahi_boxes' in locals() else "crop"
            logger.info(
                "player_tracker frame=%d full=%d %s=%d deduped=%d filtered_out=%d kept=%d",
                frame_idx, len(full_boxes_list), alt_label, alt_n,
                n_yolo_boxes, n_filtered_out, len(candidates),
            )

        # Diagnostic accounting — one bucket per frame, by candidate count.
        self._diag["frames_yolo_ran"] += 1
        self._diag["candidates_total"] += len(candidates)
        self._diag["candidates_hist"][min(len(candidates), 6)] += 1

        if not candidates:
            self._diag["choose2_kept_0"] += 1
            return []

        # Always run _choose_two_players — previously gated on len>2, but the
        # 2-candidate case is exactly the bench-sitter mis-mapping failure
        # mode: when YOLO misses the real far player, {real_near, bench_sitter}
        # pass straight through to _assign_ids and bench_sitter gets locked
        # into pid=1. _choose_two_players now enforces a y-span check to
        # reject this case.
        _t = _pc()
        candidates, candidate_kps = self._choose_two_players(
            candidates, candidate_kps, court_bbox, frame.shape[:2],
            motion_mask=motion_mask, court_corners=court_corners,
            to_court_coords=to_court_coords,
        )
        self._sub_seconds["choose2"] += _pc() - _t

        # Debug frame export AFTER _choose_two_players so the image shows
        # the true final kept set (bench sitter rejected, real players kept).
        # Previously drawn before the span check — misleadingly showed
        # rejected candidates as KEPT.
        # EARLY-RUN BIAS: dense sampling in first 600 frames so user can
        # verify mid-job and cancel bad runs without waiting full 35min.
        should_save = False
        if frame_idx > 0:
            if frame_idx <= 600 and frame_idx % 50 == 0:
                should_save = True
            elif DEBUG_FRAME_INTERVAL > 0 and frame_idx % DEBUG_FRAME_INTERVAL == 0:
                should_save = True

        if should_save:
            try:
                self._save_debug_frame_v2(
                    frame, frame_idx, deduped_boxes, candidates,
                    to_court_coords=to_court_coords,
                    to_pixel_coords=to_pixel_coords,
                )
            except Exception as e:
                logger.warning("debug frame save failed: %s", e)

            # DIAGNOSTIC: log per-candidate court_y for far-half candidates.
            # Needed to diagnose why far-baseline player (should be at y=0-5)
            # is never detected there. Runs only on debug-frame intervals so
            # doesn't spam logs.
            if to_court_coords is not None:
                midline_y_px = frame.shape[0] / 2
                far_half_diag = []
                for bi, box in enumerate(deduped_boxes):
                    cx = (box[0] + box[2]) / 2
                    cy = (box[1] + box[3]) / 2
                    y2 = box[3]
                    if cy >= midline_y_px:
                        continue  # near half — skip
                    try:
                        pt = to_court_coords(cx, y2)
                    except Exception:
                        pt = None
                    in_kept = any(
                        abs(box[0] - kb[0]) < 1 and abs(box[1] - kb[1]) < 1
                        for kb in candidates
                    )
                    court_y = pt[1] if pt else None
                    far_half_diag.append({
                        "bbox": [round(v, 0) for v in box],
                        "y2_px": round(y2, 0),
                        "court_y": round(court_y, 2) if court_y is not None else None,
                        "kept": in_kept,
                    })
                if far_half_diag:
                    logger.info(
                        "far_diag frame=%d far_half_candidates=%d: %s",
                        frame_idx, len(far_half_diag), far_half_diag,
                    )

        # Assign player_id by semantic frame half (pid 0 = near, pid 1 = far).
        # Pass frame height so the midline is computed correctly for the video.
        frame_detections = self._assign_ids(
            candidates, frame_idx, candidate_kps, frame_height=frame.shape[0],
        )
        self.detections.extend(frame_detections)
        self._last_result = frame_detections

        # "other" = this frame's detect_frame total minus its sub-stage time
        # (dedup, court filter, _assign_ids, debug-frame bookkeeping).
        frame_total_s = _pc() - _t_detect_start
        sub_after = (
            self._sub_seconds["full_yolo"]
            + self._sub_seconds["sahi"]
            + self._sub_seconds["choose2"]
        )
        self._sub_seconds["other"] += max(0.0, frame_total_s - (sub_after - _sub_before))
        return frame_detections

    def _run_yolo(self, frame: np.ndarray, conf: float = None):
        """Run YOLO on a full frame. Returns (boxes_list, kps_list).

        boxes_list: list of (x1, y1, x2, y2) tuples in frame coordinates
        kps_list: list of (17, 3) numpy arrays or None per detection
        """
        confidence = conf or YOLO_CONFIDENCE
        if self.has_pose:
            results = self.model.predict(
                frame, conf=confidence, imgsz=YOLO_IMGSZ, verbose=False,
            )
        else:
            results = self.model.predict(
                frame, conf=confidence, imgsz=YOLO_IMGSZ,
                classes=[YOLO_PERSON_CLASS_ID], verbose=False,
            )
        boxes = results[0].boxes if results else []
        kps_data = results[0].keypoints if (results and self.has_pose) else None

        out_boxes = []
        out_kps = []
        for bi, box in enumerate(boxes):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            out_boxes.append((float(x1), float(y1), float(x2), float(y2)))
            if kps_data is not None and bi < len(kps_data.data):
                out_kps.append(kps_data.data[bi].cpu().numpy())
            else:
                out_kps.append(None)
        return out_boxes, out_kps

    def _run_yolo_court_crop(self, frame: np.ndarray, court_bbox: tuple):
        """Run YOLO on the court-cropped region. Returns (boxes_list, kps_list)
        with coordinates translated back to the FULL frame.

        Cropping focuses YOLO on the court area and effectively upscales
        distant players (since YOLO resizes the smaller crop to imgsz=1280
        instead of the full 1920x1080 frame).

        IMPORTANT: y1 is clamped to at most 10% of frame height. The court
        keypoint model often misses far-baseline keypoints (too small at
        camera distance), causing court_bbox to only cover the near court.
        Without this clamp, the crop excludes the far baseline entirely —
        the far player is never seen by the crop pass. The 10% floor
        ensures the far baseline area is always included.
        """
        cb_x1, cb_y1, cb_x2, cb_y2 = court_bbox
        h, w = frame.shape[:2]
        margin = YOLO_COURT_CROP_MARGIN_PX
        x1 = max(0, int(cb_x1 - margin))
        # Clamp y1 to at most 10% from top — guarantees far baseline is in crop
        y1_from_bbox = max(0, int(cb_y1 - margin))
        y1 = min(y1_from_bbox, int(h * 0.10))
        x2 = min(w, int(cb_x2 + margin))
        y2 = min(h, int(cb_y2 + margin))

        if x2 <= x1 or y2 <= y1:
            return [], []

        cropped = frame[y1:y2, x1:x2]
        if cropped.size == 0:
            return [], []

        crop_boxes, crop_kps = self._run_yolo(cropped, conf=YOLO_COURT_CROP_CONFIDENCE)

        # Translate crop coords → full frame coords
        out_boxes = []
        out_kps = []
        for (cx1, cy1, cx2, cy2), kp in zip(crop_boxes, crop_kps):
            out_boxes.append((cx1 + x1, cy1 + y1, cx2 + x1, cy2 + y1))
            if kp is not None:
                kp_shifted = kp.copy()
                kp_shifted[:, 0] += x1
                kp_shifted[:, 1] += y1
                out_kps.append(kp_shifted)
            else:
                out_kps.append(None)
        return out_boxes, out_kps

    def _run_yolo_far_baseline(self, frame: np.ndarray):
        """Run DETECTION-ONLY YOLO on a tight crop of the far-baseline area.

        KEY INSIGHT (from research): YOLOv8-pose SUPPRESSES small detections
        (~30-40px) because the pose NMS requires resolvable keypoints. The
        far player is 30-40px — well above the detection-only floor (~20px)
        but below the pose floor (~60-80px). Using yolov8m (detection-only)
        instead of yolov8x-pose for this pass is the fix.

        Crop: top 28% height × central 70% width, conf=0.15.
        Confidence raised from 0.05 to 0.15 because the detection-only model
        produces higher-confidence detections on small people (no keypoint
        suppression). This reduces false positives from the aggressive 0.05.
        """
        if self._det_model is None:
            return [], []

        h, w = frame.shape[:2]
        y1 = 0
        y2 = int(h * 0.28)
        x1 = int(w * 0.15)
        x2 = int(w * 0.85)

        if y2 <= y1 or x2 <= x1:
            return [], []

        cropped = frame[y1:y2, x1:x2]
        if cropped.size == 0:
            return [], []

        # Use detection-only model (yolov8m) — NOT the pose model
        results = self._det_model.predict(
            cropped, conf=0.15, imgsz=YOLO_IMGSZ,
            classes=[YOLO_PERSON_CLASS_ID], verbose=False,
        )
        boxes = results[0].boxes if results else []
        crop_boxes = []
        crop_kps = []
        for box in boxes:
            bx1, by1, bx2, by2 = box.xyxy[0].cpu().numpy()
            crop_boxes.append((float(bx1), float(by1), float(bx2), float(by2)))
            crop_kps.append(None)  # no keypoints from detection-only model
        # Log every detection with coordinates — need to see whether the
        # far player is detected but lost in dedup, or genuinely not seen.
        for bi, (bx1, by1, bx2, by2) in enumerate(crop_boxes):
            bw, bh = bx2 - bx1, by2 - by1
            logger.info(
                "far_baseline_pass: det[%d] crop_box=(%.0f,%.0f,%.0f,%.0f) "
                "size=%.0fx%.0f frame_box=(%.0f,%.0f,%.0f,%.0f)",
                bi, bx1, by1, bx2, by2, bw, bh,
                bx1 + x1, by1 + y1, bx2 + x1, by2 + y1,
            )
        logger.info(
            "far_baseline_pass: crop=%dx%d found=%d conf=0.05",
            x2 - x1, y2, len(crop_boxes),
        )

        # Translate crop coords → full frame coords
        out_boxes = []
        out_kps = []
        for (cx1, cy1, cx2, cy2), kp in zip(crop_boxes, crop_kps):
            out_boxes.append((cx1 + x1, cy1 + y1, cx2 + x1, cy2 + y1))
            if kp is not None:
                kp_shifted = kp.copy()
                kp_shifted[:, 0] += x1
                kp_shifted[:, 1] += y1
                out_kps.append(kp_shifted)
            else:
                out_kps.append(None)
        return out_boxes, out_kps

    def _run_sahi(self, frame: np.ndarray, court_bbox=None):
        """Run SAHI tiled inference for systematic small-object person detection.

        SAHI slices the frame into overlapping 416×416 tiles, runs YOLO on each
        tile independently, then merges results via NMS. This gives the far
        player (~30-40px in 1080p) much higher resolution within its tile than
        full-frame inference provides.

        Optimisation: when a court_bbox is provided, we only tile the court
        region (with a generous margin) rather than the full frame. The crowd
        stands never contain players, so tiling them is wasted compute.
        Tile count drops ~40-60% depending on camera framing.

        Returns (boxes_list, kps_list) in full-frame coordinates.
        """
        if self._sahi_model is None:
            return [], []

        try:
            from sahi.predict import get_sliced_prediction

            # Crop to court region with 30% margin. The court_bbox comes
            # from raw CNN keypoints which on wide-angle footage often put
            # the "far baseline" at a pixel y much LOWER than the real far
            # baseline (CNN collapses baseline + service line into the
            # same visual feature). With a 10% margin, SAHI's crop started
            # at pixel y~230 while the real far player was at pixel y~150-200,
            # entirely outside the crop. 30% covers the gap.
            roi_x, roi_y = 0, 0
            target = frame
            if court_bbox is not None:
                h, w = frame.shape[:2]
                cx1, cy1, cx2, cy2 = court_bbox
                margin_x = int((cx2 - cx1) * 0.30)
                margin_y = int((cy2 - cy1) * 0.30)
                roi_x = max(0, int(cx1) - margin_x)
                roi_y = max(0, int(cy1) - margin_y)
                roi_x2 = min(w, int(cx2) + margin_x)
                roi_y2 = min(h, int(cy2) + margin_y)
                target = frame[roi_y:roi_y2, roi_x:roi_x2]

            result = get_sliced_prediction(
                target,
                self._sahi_model,
                slice_height=SAHI_SLICE_HEIGHT,
                slice_width=SAHI_SLICE_WIDTH,
                overlap_height_ratio=SAHI_OVERLAP_RATIO,
                overlap_width_ratio=SAHI_OVERLAP_RATIO,
                postprocess_type=SAHI_POSTPROCESS_TYPE,
                postprocess_match_threshold=SAHI_POSTPROCESS_MATCH_THRESHOLD,
                verbose=0,
            )

            boxes = []
            kps = []
            for pred in result.object_prediction_list:
                # Filter to person class only (COCO class 0)
                if pred.category.id != YOLO_PERSON_CLASS_ID:
                    continue
                bbox = pred.bbox
                # Offset back to full-frame coords (no-op when no cropping)
                x1, y1 = bbox.minx + roi_x, bbox.miny + roi_y
                x2, y2 = bbox.maxx + roi_x, bbox.maxy + roi_y
                boxes.append((float(x1), float(y1), float(x2), float(y2)))
                kps.append(None)  # SAHI uses detection-only, no keypoints

            logger.debug("sahi_pass: found %d persons in %d tiles",
                        len(boxes), len(result.object_prediction_list))
            return boxes, kps

        except Exception as e:
            logger.warning("SAHI inference failed: %s", e)
            return [], []

    @staticmethod
    def _compute_motion_ratio(box: tuple, motion_mask: np.ndarray) -> float:
        """Compute fraction of bbox pixels that are foreground (moving) in the MOG2 mask.

        Returns 0.0-1.0. A moving player typically scores 0.05-0.15;
        a seated spectator scores 0.00-0.01.
        """
        mask_h, mask_w = motion_mask.shape[:2]
        x1 = max(0, int(box[0]))
        y1 = max(0, int(box[1]))
        x2 = min(mask_w, int(box[2]))
        y2 = min(mask_h, int(box[3]))
        if x2 <= x1 or y2 <= y1:
            return 0.0
        roi = motion_mask[y1:y2, x1:x2]
        total_pixels = roi.size
        if total_pixels == 0:
            return 0.0
        # MOG2 mask: 255 = foreground, 0 = background
        fg_pixels = int((roi > 127).sum())
        return fg_pixels / total_pixels

    def _dedupe_iou(self, boxes_list, kps_list, iou_thresh: float = 0.5):
        """Remove overlapping boxes via greedy IoU deduplication.

        When the full-frame and crop passes both detect the same player, we
        get duplicates. Keeps the FIRST occurrence, drops subsequent boxes
        with IoU > iou_thresh.
        """
        if not boxes_list:
            return [], []
        kept_boxes = []
        kept_kps = []
        for box, kp in zip(boxes_list, kps_list):
            duplicate = False
            for existing in kept_boxes:
                if self._compute_iou(box, existing) > iou_thresh:
                    duplicate = True
                    break
            if not duplicate:
                kept_boxes.append(box)
                kept_kps.append(kp)
        return kept_boxes, kept_kps

    def _save_debug_frame_v2(self, frame, frame_idx: int, all_boxes, kept_boxes,
                              to_court_coords=None, to_pixel_coords=None) -> None:
        """Save a frame with YOLO bboxes drawn on it. Uploads to S3 immediately
        if upload context is set, so the user can inspect mid-run.

        Draws ALL detections (red = filtered, green = kept). When
        to_court_coords is provided, also annotates each bbox with its
        projected court_x/y value so we can diagnose projection issues.

        When to_pixel_coords is provided (post-calibration-lock), overlays
        the real court lines (baselines, net, service lines, sidelines)
        projected from metric space back to pixel space. Misalignment vs
        the actual court in the image = calibration off by exactly that
        much.
        """
        os.makedirs(DEBUG_FRAMES_DIR, exist_ok=True)
        img = frame.copy()

        if to_pixel_coords is not None:
            self._draw_metric_grid(img, to_pixel_coords)

        kept_set = set(
            (round(b[0], 1), round(b[1], 1), round(b[2], 1), round(b[3], 1))
            for b in kept_boxes
        )
        for box in all_boxes:
            x1, y1, x2, y2 = [int(v) for v in box]
            key = (round(box[0], 1), round(box[1], 1), round(box[2], 1), round(box[3], 1))
            color = (0, 255, 0) if key in kept_set else (0, 0, 255)
            label = "KEPT" if key in kept_set else "FILTER"
            # Compute court_y if homography available — visualise on bbox
            court_label = ""
            if to_court_coords is not None:
                cx = (box[0] + box[2]) / 2
                try:
                    pt = to_court_coords(cx, box[3], strict=False)
                except TypeError:
                    pt = to_court_coords(cx, box[3])
                except Exception:
                    pt = None
                if pt is not None:
                    court_label = f" x={pt[0]:.1f} y={pt[1]:.1f}"
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 3)
            cv2.putText(
                img, label + court_label, (x1, y1 - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2,
            )
        header = (f"frame={frame_idx} all={len(all_boxes)} kept={len(kept_boxes)} "
                  f"crop_inf={YOLO_COURT_CROP_INFERENCE} imgsz={YOLO_IMGSZ}")
        cv2.putText(
            img, header, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2,
        )
        fname = f"frame_{frame_idx:06d}_n{len(kept_boxes)}.jpg"
        out_path = os.path.join(DEBUG_FRAMES_DIR, fname)
        cv2.imwrite(out_path, img)
        logger.info("debug frame saved: %s (kept=%d/%d)",
                     out_path, len(kept_boxes), len(all_boxes))

        # Upload directly to S3 if context is set (LIVE upload, no waiting
        # for post-processing). User can inspect mid-run and cancel bad jobs.
        if (self._debug_s3_client is not None and
                self._debug_s3_bucket and self._debug_job_id):
            try:
                s3_key = f"debug/{self._debug_job_id}/{fname}"
                self._debug_s3_client.upload_file(
                    out_path, self._debug_s3_bucket, s3_key,
                    ExtraArgs={"ContentType": "image/jpeg"},
                )
                logger.info("debug frame uploaded: s3://%s/%s",
                             self._debug_s3_bucket, s3_key)
            except Exception as e:
                logger.warning("debug frame S3 upload failed: %s", e)

    def _draw_metric_grid(self, img, to_pixel_coords: Callable) -> None:
        """Overlay real court lines projected from metric space using the
        active lens calibration. Misalignment vs the court lines visible
        in the image indicates calibration error by exactly that amount.

        Colours:
          yellow = baselines + net + sidelines (outer court)
          cyan   = service lines + centre service (inner lines)
        """
        # (x_start, y_start, x_end, y_end) in metres; colour
        CL = COURT_LENGTH_M
        CW = COURT_WIDTH_DOUBLES_M
        SINGLES_OFFSET = (CW - COURT_WIDTH_SINGLES_M) / 2  # 1.37m
        SVC_DIST = SERVICE_BOX_DEPTH_M                      # 6.40m from net
        NET_Y = CL / 2                                      # 11.885
        outer = (0, 255, 255)   # yellow (BGR)
        inner = (255, 255, 0)   # cyan   (BGR)
        lines = [
            (0.0, 0.0, CW, 0.0, outer),                       # far baseline
            (0.0, CL, CW, CL, outer),                         # near baseline
            (0.0, NET_Y, CW, NET_Y, outer),                   # net
            (0.0, 0.0, 0.0, CL, outer),                       # doubles sideline L
            (CW, 0.0, CW, CL, outer),                         # doubles sideline R
            (SINGLES_OFFSET, 0.0, SINGLES_OFFSET, CL, outer), # singles L
            (CW - SINGLES_OFFSET, 0.0, CW - SINGLES_OFFSET, CL, outer),  # singles R
            (SINGLES_OFFSET, NET_Y - SVC_DIST, CW - SINGLES_OFFSET, NET_Y - SVC_DIST, inner),  # far svc
            (SINGLES_OFFSET, NET_Y + SVC_DIST, CW - SINGLES_OFFSET, NET_Y + SVC_DIST, inner),  # near svc
            (CW / 2, NET_Y - SVC_DIST, CW / 2, NET_Y + SVC_DIST, inner),  # centre svc
        ]
        h, w = img.shape[:2]
        for (x1, y1, x2, y2, colour) in lines:
            prev = None
            for t in np.linspace(0.0, 1.0, 80):
                mx = x1 + t * (x2 - x1)
                my = y1 + t * (y2 - y1)
                pt = to_pixel_coords(mx, my)
                if pt is None:
                    prev = None
                    continue
                px, py = int(round(pt[0])), int(round(pt[1]))
                if prev is not None:
                    # Draw segment only if both endpoints are inside the frame
                    # or close to it (allow 50px margin for lines that exit).
                    if (-50 <= prev[0] < w + 50 and -50 <= prev[1] < h + 50 and
                            -50 <= px < w + 50 and -50 <= py < h + 50):
                        cv2.line(img, prev, (px, py), colour, 1, cv2.LINE_AA)
                prev = (px, py)

    def _save_debug_frame(self, frame, frame_idx: int, boxes) -> None:
        """Save a frame with YOLO bboxes drawn on it for visual debugging.

        Output: /tmp/debug_frames/frame_{idx:06d}_n{count}.jpg
        Uploaded to S3 by __main__.py post-processing.
        """
        os.makedirs(DEBUG_FRAMES_DIR, exist_ok=True)
        img = frame.copy()
        n_boxes = len(boxes)
        for box in boxes:
            try:
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                conf = float(box.conf[0].cpu().numpy())
                # Color: green if conf >= 0.5, yellow otherwise
                color = (0, 255, 0) if conf >= 0.5 else (0, 255, 255)
                cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), color, 3)
                cv2.putText(
                    img, f"{conf:.2f}", (int(x1), int(y1) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2,
                )
            except Exception:
                continue
        # Header banner
        header = f"frame={frame_idx} yolo_boxes={n_boxes} conf>={YOLO_CONFIDENCE} imgsz={YOLO_IMGSZ}"
        cv2.putText(
            img, header, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2,
        )
        out_path = os.path.join(
            DEBUG_FRAMES_DIR, f"frame_{frame_idx:06d}_n{n_boxes}.jpg"
        )
        cv2.imwrite(out_path, img)
        logger.info("debug frame saved: %s (n_boxes=%d)", out_path, n_boxes)

    def _choose_two_players(self, candidates: list, candidate_kps: list,
                            court_bbox, frame_shape,
                            motion_mask: Optional[np.ndarray] = None,
                            court_corners: Optional[list] = None,
                            to_court_coords: Optional[Callable] = None) -> tuple:
        """Select up to 2 players — one per half — using court-metre zoning.

        THREE-TIER PRIORITY (per half, when to_court_coords is available):
          Priority 1 (3000): INSIDE doubles court
              0 <= x <= 10.97, 0 <= y <= 23.77
          Priority 2 (2000): BEHIND own baseline — max 4m deep, 3m off each
              doubles sideline. Covers serving stance + baseline recovery.
              -3 <= x <= 13.97, (-4 <= y < 0) OR (23.77 < y <= 27.77)
          Priority 3 (1000): WIDE ALLEY corridor — 1m off each doubles
              sideline, baseline-to-baseline. Real players run wide like
              this 1-2x per match; anything wider is noise.
              (-1 <= x < 0 OR 10.97 < x <= 11.97) AND 0 <= y <= 23.77
          Tier 0: Everything else (umpire, spectator, coach, bench sitter)

        Baseline-closeness tiebreaker (0-500 pts within any tier): candidates
        whose feet are near a baseline score higher. A candidate at the net
        (far from both baselines) scores the lowest. This rewards the correct
        entity — real players hug baselines during serves/rallies, while
        umpires and commentators sit at net level.

        MOG2 motion adds +500 bonus (moving > stationary). Bbox area adds
        0-200 (larger bbox = closer to camera = more likely a real player).

        Falls back to the legacy pixel-space geometry when to_court_coords
        is unavailable (pre-calibration frames). One candidate selected from
        each half (far/near) of the frame.
        """
        MIN_Y_SEPARATION_RATIO = 0.35

        if len(candidates) == 0:
            self._diag["choose2_kept_0"] += 1
            return [], []

        if len(candidates) == 1:
            self._diag["choose2_kept_1_single"] += 1
            return candidates, candidate_kps

        frame_h = frame_shape[0]
        frame_w = frame_shape[1] if len(frame_shape) > 1 else 1920
        min_span_px = MIN_Y_SEPARATION_RATIO * frame_h
        midline_y = frame_h / 2

        # Build court polygon and geometry if corners are available
        court_poly = None       # cv2 polygon for point-in-court test
        far_baseline_y = None   # pixel y of far baseline (top of court)
        near_baseline_y = None  # pixel y of near baseline (bottom of court)
        left_sideline_x = None  # left boundary
        right_sideline_x = None # right boundary
        court_center_x = frame_w / 2  # fallback

        if court_corners is not None and len(court_corners) == 4:
            # corners: [far_left, far_right, near_left, near_right].
            # These are DOUBLES baseline corners (keypoints 0-3 of the
            # court keypoint model), so court_poly covers singles + alleys.
            fl, fr, nl, nr = court_corners
            court_poly = np.array([fl, fr, nr, nl], dtype=np.float32)
            far_baseline_y = (fl[1] + fr[1]) / 2
            near_baseline_y = (nl[1] + nr[1]) / 2
            left_sideline_x = min(fl[0], nl[0])
            right_sideline_x = max(fr[0], nr[0])
            court_center_x = (left_sideline_x + right_sideline_x) / 2
            court_depth = abs(near_baseline_y - far_baseline_y)
            court_width_px = right_sideline_x - left_sideline_x
            # 3m behind baseline (core serve position zone). 3m / 23.77m court
            # length ≈ 12.6% of court depth.
            baseline_margin = court_depth * (3.0 / COURT_LENGTH_M)
            # Wide-of-doubles tolerance for tier-3: up to 2m off the doubles
            # sideline, and only when within the baseline-to-baseline band.
            # Real players run wide like this maybe 1-2× per match.
            wide_margin = court_width_px * (2.0 / COURT_WIDTH_DOUBLES_M)

        scored = []
        for box, kps in zip(candidates, candidate_kps):
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[3]) / 2
            y2 = box[3]  # bottom of bbox = feet
            half = "far" if cy < midline_y else "near"

            # Compute motion ratio from MOG2 foreground mask
            motion_ratio = 0.0
            if motion_mask is not None:
                motion_ratio = self._compute_motion_ratio(box, motion_mask)
            motion_bonus = 500 if motion_ratio >= MOG2_MIN_MOTION_RATIO else 0

            # ── Court-metre scoring (preferred when homography available) ──
            # Rule 1 (3000): 0<=x<=10.97, 0<=y<=23.77 — inside doubles court
            # Rule 2 (2000): -3<=x<=13.97, -4<=y<=27.77 (and outside Rule 1) —
            #                the "considerable" extended zone
            # Tier 0:        Outside extended zone — spectator/umpire/bench
            # Baseline-closeness tiebreaker (0-500): prefer feet near any
            # baseline; net position (y=11.88) scores lowest.
            court_xy = None
            if to_court_coords is not None:
                try:
                    # strict=False so we get metric coords even beyond the
                    # ±5m court-sanity bounds. The far player on MATCHI
                    # wide-angle projects to y≈-7.8 (tier-2 widened to -10m
                    # in A0 to accept this). With strict=True the
                    # projection returns None for y<-5, which short-
                    # circuits into the "minimal score" branch below and
                    # tier-2's widening never gets consulted. A0's fix
                    # only works in combination with strict=False here.
                    court_xy = to_court_coords(cx, y2, strict=False)
                except Exception:
                    court_xy = None

            if court_xy is not None:
                court_x_m, court_y_m = court_xy
                NET_Y = COURT_LENGTH_M / 2  # 11.885

                # Priority 1 (3000): inside doubles court
                in_court = (
                    0.0 <= court_x_m <= COURT_WIDTH_DOUBLES_M
                    and 0.0 <= court_y_m <= COURT_LENGTH_M
                )
                # Priority 2 (2000): nearest player behind baseline, up
                # to 10m either side. Observed on MATCHI wide-angle that
                # a far player physically ~4m behind the baseline projects
                # to metric y ~-6/-7 — calibration extrapolation on the
                # far edge is over-negative. Expanding both sides to 10m
                # catches the real player; tier 0 + MIN_SELECTABLE_SCORE
                # still rejects spectators on the back wall.
                # TODO: investigate far-side extrapolation bias (physical
                # ~4m → measured -6/-7m suggests k1/k2 fit leaves residual
                # distortion near the image top). Tightening this range
                # depends on that fix.
                behind_baseline = (
                    -3.0 <= court_x_m <= COURT_WIDTH_DOUBLES_M + 3.0
                    and (
                        -10.0 <= court_y_m < 0.0
                        or COURT_LENGTH_M < court_y_m <= COURT_LENGTH_M + 10.0
                    )
                )
                # Priority 3 (1000): wide-alley corridor — 1m off each
                # doubles sideline, baseline-to-baseline. Real players run
                # this wide maybe 1-2x per match; anything wider is noise.
                wide_alley = (
                    (
                        -1.0 <= court_x_m < 0.0
                        or COURT_WIDTH_DOUBLES_M < court_x_m <= COURT_WIDTH_DOUBLES_M + 1.0
                    )
                    and 0.0 <= court_y_m <= COURT_LENGTH_M
                )

                if in_court:
                    tier = 3000
                elif behind_baseline:
                    tier = 2000
                elif wide_alley:
                    tier = 1000
                elif kps is not None:
                    # Net-zone / mid-court with pose keypoints. This is where
                    # real players end up during rally (approach shots, net
                    # play, recovery). Before this tier existed, these
                    # pose-carrying full-frame YOLO detections were rejected
                    # as tier-0 and silently dropped, creating a minute-long
                    # "pose gap" in the stored detections exactly when the
                    # near player was most active.
                    tier = 500
                else:
                    tier = 0  # off-court (umpire, spectator, coach)

                # Pixel-space sanity gate (Fix B): even if the metric zone
                # says "in court", a candidate whose pixel feet are far
                # outside the detected court polygon is a spectator whose
                # wrong-homography projection happens to land inside. Cap
                # tier based on how far the feet sit outside the polygon.
                #   inside polygon                 → keep metric tier
                #   within 50 px of edge           → keep metric tier
                #   > 300 px outside               → tier = 0
                # Previously 150 px but the polygon is a 4-corner straight-
                # edge quadrilateral while the real court baseline curves
                # down at the edges on wide-angle cameras. A player with
                # feet at the baseline corner can be 200+ px below the
                # straight polygon edge even though they're ON the court.
                # With correct lens calibration, metric tier scoring
                # already rejects off-court candidates cleanly; the pixel
                # gate is just a safety net for extreme outliers.
                if court_poly is not None:
                    pixel_dist = cv2.pointPolygonTest(
                        court_poly, (float(cx), float(y2)), True,
                    )
                    if pixel_dist < -300.0:
                        tier = 0

                # Baseline-closeness: distance to nearer baseline, in metres.
                # At y=0 or y=23.77 → dist=0 → full 500 points.
                # At y=11.88 (net) → dist=11.88 → 0 points.
                # Normalise by NET_Y so net gives exactly 0.
                dist_to_nearest_baseline = min(
                    abs(court_y_m - 0.0),
                    abs(court_y_m - COURT_LENGTH_M),
                )
                baseline_closeness = max(
                    0.0, 1.0 - dist_to_nearest_baseline / NET_Y
                ) * 500

                # Bbox area: real players are closer to camera than spectators
                # behind them and thus have larger bboxes. Normalise to 0-200.
                bbox_w = box[2] - box[0]
                bbox_h = box[3] - box[1]
                bbox_score = min(200, (bbox_w * bbox_h) / 25.0)
                # Pose bonus: full-frame YOLOv8x-pose carries 17 COCO keypoints;
                # SAHI tile output and detection-only YOLOv8m don't. Giving
                # pose-carrying detections a +300 nudge prevents small SAHI
                # fragments from stealing player_id=0 when the real near
                # player also exists in the same frame with pose data.
                pose_bonus = 300 if kps is not None else 0

                # Tier 0 = off-court (spectator, umpire, linesperson).
                # Zero the bonuses so they can't accidentally outscore a
                # real player in another frame. If no tier>0 candidate
                # exists for a half, the half is correctly left empty.
                if tier == 0:
                    score = 0.0
                else:
                    score = tier + motion_bonus + baseline_closeness + bbox_score + pose_bonus

            elif to_court_coords is not None:
                # Calibration exists but THIS candidate's projection failed
                # the strict bounds check (court_xy is None). Means the
                # candidate is physically off the real court — a spectator
                # beyond the sidelines, the umpire on a chair past the net,
                # someone in the bleachers. Give them a minimal score so
                # they can't beat a real far-player detection (which always
                # produces a valid court_xy).
                tier = 0
                score = float(motion_bonus)  # 0 or 500 only
            elif court_poly is not None:
                # ── Fallback: legacy pixel-space geometry (no homography) ──
                # Used on frames before court calibration completes.
                in_court = cv2.pointPolygonTest(court_poly, (cx, y2), False) >= 0
                home_baseline_y = far_baseline_y if half == "far" else near_baseline_y
                lateral_slack = court_width_px * (3.0 / COURT_WIDTH_DOUBLES_M)
                in_lateral_band = (left_sideline_x - lateral_slack <= cx
                                   <= right_sideline_x + lateral_slack)
                if half == "far":
                    behind_own_baseline = (
                        y2 < far_baseline_y
                        and (far_baseline_y - y2) <= baseline_margin
                        and in_lateral_band
                    )
                else:
                    behind_own_baseline = (
                        y2 > near_baseline_y
                        and (y2 - near_baseline_y) <= baseline_margin
                        and in_lateral_band
                    )

                if in_court:
                    tier = 3000
                elif behind_own_baseline:
                    tier = 2000
                else:
                    tier = 0

                if tier == 0:
                    score = 0.0
                else:
                    dist_to_baseline_px = abs(y2 - home_baseline_y)
                    baseline_closeness = max(
                        0.0, 1.0 - (dist_to_baseline_px / court_depth)
                    ) * 500
                    bbox_w = box[2] - box[0]
                    bbox_h = box[3] - box[1]
                    bbox_score = min(200, (bbox_w * bbox_h) / 25.0)
                    score = tier + motion_bonus + baseline_closeness + bbox_score

            else:
                # ── Fallback: centering + motion (no court geometry) ────
                x_offset = abs(cx - court_center_x) / frame_w
                centering = max(0.0, 1.0 - x_offset / 0.30) * 500
                score = motion_bonus + centering + y2

            scored.append((score, cy, half, box, kps, motion_ratio))

        # Pick best (highest score) from each half
        far_candidates = [s for s in scored if s[2] == "far"]
        near_candidates = [s for s in scored if s[2] == "near"]
        far_candidates.sort(key=lambda s: s[0], reverse=True)
        near_candidates.sort(key=lambda s: s[0], reverse=True)

        # Require a minimum score to select a player for a half. We lowered
        # this from 1000 (tier-3 floor = wide alley) to 500 (new net-zone
        # pose tier) so mid-court pose-carrying detections aren't rejected.
        # Non-pose mid-court fragments still score tier=0 and fail this gate.
        MIN_SELECTABLE_SCORE = 500.0
        best_far = far_candidates[0] if far_candidates and far_candidates[0][0] >= MIN_SELECTABLE_SCORE else None
        best_near = near_candidates[0] if near_candidates and near_candidates[0][0] >= MIN_SELECTABLE_SCORE else None

        if best_far:
            logger.debug(
                "_choose_two_players: best_far cy=%.0f feet_y=%.0f score=%.0f motion=%.3f",
                best_far[1], best_far[3][3], best_far[0], best_far[5],
            )
            # Log all far candidates with motion ratios every 150 frames
            if len(far_candidates) > 1:
                self._diag["far_multi_candidate_frames"] = self._diag.get("far_multi_candidate_frames", 0) + 1
                # Log top 3 far candidates for debugging
                for i, fc in enumerate(far_candidates[:3]):
                    logger.debug(
                        "  far_cand[%d] cy=%.0f y2=%.0f motion=%.3f score=%.0f",
                        i, fc[1], fc[3][3], fc[5], fc[0],
                    )
        if best_near:
            logger.debug(
                "_choose_two_players: best_near cy=%.0f score=%.0f",
                best_near[1], best_near[0],
            )

        chosen = []
        if best_far:
            chosen.append(best_far)
        if best_near:
            chosen.append(best_near)

        if len(chosen) == 2:
            span = abs(chosen[0][1] - chosen[1][1])
            if span >= min_span_px:
                if len(candidates) > 2:
                    self._diag["choose2_dropped_middle"] += 1
                self._diag["choose2_kept_2"] += 1
                logger.debug(
                    "_choose_two_players: kept 2, far_cy=%.1f near_cy=%.1f "
                    "span=%.1f midline=%.1f far_motion=%.3f (from %d)",
                    best_far[1], best_near[1], span, midline_y,
                    best_far[5], len(candidates),
                )
                return [c[3] for c in chosen], [c[4] for c in chosen]
            else:
                self._diag["choose2_kept_1_span_fail"] += 1
                logger.debug(
                    "_choose_two_players: span=%.1f < min=%.1f, dropping far "
                    "(far_cy=%.1f near_cy=%.1f midline=%.1f, from %d cands)",
                    span, min_span_px, best_far[1], best_near[1],
                    midline_y, len(candidates),
                )
                return [best_near[3]], [best_near[4]]

        # Only one half has candidates
        pick = chosen[0] if chosen else None
        if pick:
            self._diag["choose2_kept_1_single"] += 1
            return [pick[3]], [pick[4]]

        self._diag["choose2_kept_0"] += 1
        return [], []

    def _assign_ids(self, bboxes: list, frame_idx: int,
                    kps_list: list = None,
                    frame_height: Optional[int] = None) -> List[PlayerDetection]:
        """Assign player_id by frame half.

        SEMANTIC ASSIGNMENT: pid 0 = near-side (bbox center cy > midline),
        pid 1 = far-side (bbox center cy <= midline). This is robust to
        tracking timeouts that previously caused identity swaps.

        Why the change (found 2026-04-18):
        The old IoU-based path had a swap-lock bug. When both players were
        lost for PLAYER_TRACK_TIMEOUT_FRAMES (happens during sparse-
        tracking minutes 1-4 on the MATCHI baseline) and the first re-init
        frame saw ONLY the far player, the far player was assigned pid=0
        by the "highest pixel-y first" rule (it was the only bbox). IoU
        matching on subsequent frames then locked that swap in, yielding
        minutes where Player 1 is actually the near-camera player with
        pose (court_y~=23) and Player 0 is the 30px far bbox (court_y~=0).

        Semantic-half assignment sidesteps this entirely: pid is a pure
        function of current bbox pixel position, no state, no possibility
        of swap. Players on a tennis court stay in their respective halves
        in pixel space (near player's feet are always in the lower half of
        the frame because the camera is behind/above the near baseline).

        If MULTIPLE bboxes fall in the same half (e.g. near player +
        umpire), the one with the largest area wins — the upstream
        `_choose_two_players` has already scored by tier / motion / pose
        and should usually emit at most two bboxes, so collisions are
        rare and "biggest wins" is a safe tiebreaker.

        PLAYER_TRACK_TIMEOUT_FRAMES / IoU / distance gates are obsolete
        with this approach and no longer consulted here.
        """
        if kps_list is None:
            kps_list = [None] * len(bboxes)

        # Default to 1080 if not passed (MATCHI 1080p). Callers in this
        # module pass frame.shape[0] when they have the frame.
        fh = frame_height if frame_height is not None else 1080
        midline_y = fh / 2.0

        near_cands = []
        far_cands = []
        for bbox, kps in zip(bboxes, kps_list):
            cy = (bbox[1] + bbox[3]) / 2.0
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            entry = (area, bbox, kps)
            if cy > midline_y:
                near_cands.append(entry)
            else:
                far_cands.append(entry)

        # Biggest bbox per half wins. _choose_two_players already filtered
        # to real player candidates; this is a last-line disambiguator.
        near_cands.sort(key=lambda c: c[0], reverse=True)
        far_cands.sort(key=lambda c: c[0], reverse=True)

        results = []
        for pid, cands in [(0, near_cands[:1]), (1, far_cands[:1])]:
            for _area, bbox, kps in cands:
                cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                results.append(PlayerDetection(
                    frame_idx=frame_idx, player_id=pid,
                    bbox=bbox, center=(cx, cy), keypoints=kps,
                ))
                self._prev_players[pid] = (bbox, frame_idx)

        return results

    @staticmethod
    def _compute_iou(box1: tuple, box2: tuple) -> float:
        x1 = max(box1[0], box2[0])
        y1 = max(box1[1], box2[1])
        x2 = min(box1[2], box2[2])
        y2 = min(box1[3], box2[3])
        inter = max(0, x2 - x1) * max(0, y2 - y1)
        area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
        area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
        union = area1 + area2 - inter
        return inter / union if union > 0 else 0.0

    def map_to_court(self, court_detector):
        """Map all player detections to court coordinates."""
        for det in self.detections:
            coords = court_detector.to_court_coords(det.center[0], det.center[1])
            if coords is not None:
                det.court_x, det.court_y = coords

    def log_diagnostics(self):
        """Dump cumulative player-detection diagnostics. Call once post-inference.

        What to look for:
        - candidates_hist: if the '2' bucket is huge and '3+' is small, YOLO
          is missing the far player often — which is exactly when the
          bench-sitter mis-mapping triggers.
        - choose2_kept_1_span_fail: how many frames the new y-span check
          rejected a bench-sitter-like scenario. If > 0 on a run that
          previously had ball-boy mis-mapping, the fix is firing.
        - choose2_kept_2 should dominate; kept_0 should be near zero.
        - choose2_dropped_middle shows how often 3+ candidates were culled.
        """
        d = self._diag
        total = d["frames_yolo_ran"]
        if total == 0:
            logger.info("PlayerTracker diagnostics: no frames inferred")
            return

        def pct(n):
            return 100.0 * n / total

        logger.info("=== PlayerTracker diagnostics ===")
        logger.info("frames_yolo_ran: %d", total)
        logger.info("avg candidates/frame: %.2f", d["candidates_total"] / total)
        logger.info("candidates_hist:")
        for i, c in enumerate(d["candidates_hist"]):
            label = str(i) if i < 6 else "6+"
            logger.info("  %s: %6d (%5.1f%%)", label, c, pct(c))
        logger.info("_choose_two_players outcomes:")
        logger.info("  kept_2:                %6d (%5.1f%%)", d["choose2_kept_2"], pct(d["choose2_kept_2"]))
        logger.info("  kept_1_span_fail:      %6d (%5.1f%%)", d["choose2_kept_1_span_fail"], pct(d["choose2_kept_1_span_fail"]))
        logger.info("  kept_1_single_cand:    %6d (%5.1f%%)", d["choose2_kept_1_single"], pct(d["choose2_kept_1_single"]))
        logger.info("  kept_0:                %6d (%5.1f%%)", d["choose2_kept_0"], pct(d["choose2_kept_0"]))
        logger.info("  dropped_middle (3+):   %6d (%5.1f%%)", d["choose2_dropped_middle"], pct(d["choose2_dropped_middle"]))
        logger.info("  far_multi_cand_frames: %6d", d.get("far_multi_candidate_frames", 0))

        # SAHI skip rule breakdown (B4+). Shows which predicate is firing, and
        # how often each would fire independently. If A alone is carrying the
        # skip rate with B adding very little, the metric-far predicate can be
        # retired. Conversely, frames where only B fires are the calibration-
        # extrapolation cases where pose missed the far player.
        skip_total = self._sahi_skip_count
        run_total = self._sahi_run_count
        total_sahi_frames = skip_total + run_total
        if total_sahi_frames > 0:
            logger.info("SAHI skip rule breakdown:")
            logger.info("  skipped/total:         %6d / %6d (%5.1f%%)",
                        skip_total, total_sahi_frames,
                        100.0 * skip_total / total_sahi_frames)
            br = self._sahi_skip_by_rule
            logger.info("  fired_by_A_pose:       %6d", br["A_spatial_pose"])
            logger.info("  fired_by_B_metric:     %6d", br["B_metric_far"])
            logger.info("  would_skip_A_only:     %6d", br["would_skip_A_only"])
            logger.info("  would_skip_B_only:     %6d", br["would_skip_B_only"])
            logger.info("  would_skip_both:       %6d", br["would_skip_both"])

    def reset(self):
        self._prev_players.clear()
        self.detections.clear()
        for k in self._diag:
            if isinstance(self._diag[k], list):
                self._diag[k] = [0] * len(self._diag[k])
            else:
                self._diag[k] = 0
        for k in self._sub_seconds:
            self._sub_seconds[k] = 0.0
        self._sahi_skip_count = 0
        self._sahi_run_count = 0
        for k in self._sahi_skip_by_rule:
            self._sahi_skip_by_rule[k] = 0
        self._last_detect_frame = -self._detect_interval
        self._last_result = []
