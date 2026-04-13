"""
CourtDetector — CNN for 14 tennis court keypoints + homography.
Uses the yastrebksv/TennisCourtDetector architecture (TrackNet-style encoder-decoder).
Runs every COURT_DETECTION_INTERVAL frames; returns cached result between runs.
Falls back to Hough line detection if confidence is below threshold.
"""

import logging
import numpy as np
import cv2
import torch
import torch.nn as nn
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

from ml_pipeline.config import (
    COURT_DETECTOR_WEIGHTS,
    COURT_NUM_KEYPOINTS,
    COURT_DETECTION_INTERVAL,
    COURT_CALIBRATION_FRAMES,
    COURT_CONFIDENCE_THRESHOLD,
    COURT_REFERENCE_KEYPOINTS,
    COURT_LENGTH_M,
    COURT_WIDTH_DOUBLES_M,
    COURT_WIDTH_SINGLES_M,
    TRACKNET_INPUT_WIDTH,
    TRACKNET_INPUT_HEIGHT,
    HOUGH_RHO,
    HOUGH_THETA_DIVISOR,
    HOUGH_THRESHOLD,
    HOUGH_MIN_LINE_LENGTH,
    HOUGH_MAX_LINE_GAP,
)


# ── Court keypoint CNN (same ConvBlock arch as TrackNet, different I/O) ─────

class _ConvBlock(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_size=3, pad=1, stride=1, bias=True):
        super().__init__()
        self.block = nn.Sequential(
            nn.Conv2d(in_ch, out_ch, kernel_size, stride=stride, padding=pad, bias=bias),
            nn.ReLU(),
            nn.BatchNorm2d(out_ch),
        )

    def forward(self, x):
        return self.block(x)


class CourtKeypointNet(nn.Module):
    """Same architecture as BallTrackerNet but with 3-channel input (single frame)
    and 15 output channels (14 keypoints + 1 court center)."""

    def __init__(self, in_channels=3, out_channels=15):
        super().__init__()
        self.out_channels = out_channels
        self.conv1 = _ConvBlock(in_channels, 64)
        self.conv2 = _ConvBlock(64, 64)
        self.pool1 = nn.MaxPool2d(2, 2)
        self.conv3 = _ConvBlock(64, 128)
        self.conv4 = _ConvBlock(128, 128)
        self.pool2 = nn.MaxPool2d(2, 2)
        self.conv5 = _ConvBlock(128, 256)
        self.conv6 = _ConvBlock(256, 256)
        self.conv7 = _ConvBlock(256, 256)
        self.pool3 = nn.MaxPool2d(2, 2)
        self.conv8 = _ConvBlock(256, 512)
        self.conv9 = _ConvBlock(512, 512)
        self.conv10 = _ConvBlock(512, 512)
        self.ups1 = nn.Upsample(scale_factor=2)
        self.conv11 = _ConvBlock(512, 256)
        self.conv12 = _ConvBlock(256, 256)
        self.conv13 = _ConvBlock(256, 256)
        self.ups2 = nn.Upsample(scale_factor=2)
        self.conv14 = _ConvBlock(256, 128)
        self.conv15 = _ConvBlock(128, 128)
        self.ups3 = nn.Upsample(scale_factor=2)
        self.conv16 = _ConvBlock(128, 64)
        self.conv17 = _ConvBlock(64, 64)
        self.conv18 = _ConvBlock(64, out_channels)
        self.softmax = nn.Softmax(dim=1)

    def forward(self, x, testing=False):
        b = x.size(0)
        x = self.conv1(x); x = self.conv2(x); x = self.pool1(x)
        x = self.conv3(x); x = self.conv4(x); x = self.pool2(x)
        x = self.conv5(x); x = self.conv6(x); x = self.conv7(x); x = self.pool3(x)
        x = self.conv8(x); x = self.conv9(x); x = self.conv10(x)
        x = self.ups1(x); x = self.conv11(x); x = self.conv12(x); x = self.conv13(x)
        x = self.ups2(x); x = self.conv14(x); x = self.conv15(x)
        x = self.ups3(x); x = self.conv16(x); x = self.conv17(x); x = self.conv18(x)
        out = x.reshape(b, self.out_channels, -1)
        if testing:
            out = self.softmax(out)
        return out


# ── Data class ──────────────────────────────────────────────────────────────

@dataclass
class CourtDetection:
    keypoints: np.ndarray             # (14, 2) pixel coordinates
    homography: Optional[np.ndarray]  # 3x3 matrix, pixel→court ref
    confidence: float                 # 0-1, fraction of keypoints detected
    used_fallback: bool


# ── CourtDetector ───────────────────────────────────────────────────────────

class CourtDetector:
    def __init__(self, weights_path: str = COURT_DETECTOR_WEIGHTS, device: str = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = self._load_model(weights_path)
        self.ref_keypoints = np.array(COURT_REFERENCE_KEYPOINTS, dtype=np.float32)
        self._last_detection: Optional[CourtDetection] = None
        self._last_good_detection: Optional[CourtDetection] = None  # last with valid homography
        self._detect_interval: int = COURT_DETECTION_INTERVAL
        self._last_frame_idx: int = -COURT_DETECTION_INTERVAL
        # Calibration lock — fixed camera means court geometry is constant.
        # During the first COURT_CALIBRATION_FRAMES frames, run CNN normally
        # and track the best detection (highest inlier count). After that,
        # lock the best and stop re-computing. Eliminates the constant
        # "REJECTED bad scale" cascade and wasted GPU cycles.
        self._locked_detection: Optional[CourtDetection] = None
        self._best_calibration_inliers: int = 0

    def _load_model(self, weights_path: str) -> CourtKeypointNet:
        model = CourtKeypointNet(in_channels=3, out_channels=15)
        state = torch.load(weights_path, map_location=self.device, weights_only=True)
        model.load_state_dict(state)
        model.to(self.device)
        model.eval()
        return model

    def detect(self, frame: np.ndarray, frame_idx: int = 0) -> CourtDetection:
        """Detect court keypoints with calibration-lock strategy.

        Fixed indoor camera = court geometry is constant across the video.
        Strategy:
        1. First COURT_CALIBRATION_FRAMES: run CNN, track best detection
        2. After calibration: LOCK the best detection, stop running CNN
        3. All subsequent frames reuse the locked homography (zero cost)
        """
        # If locked, always return locked detection (post-calibration)
        if self._locked_detection is not None:
            return self._locked_detection

        # Within detection interval, return cached
        if (frame_idx - self._last_frame_idx) < self._detect_interval and self._last_detection is not None:
            return self._last_detection

        detection = self._detect_cnn(frame)
        # During calibration, always TRY Hough as well — the CNN often fails
        # on far-baseline keypoints while Hough can find all 4 baselines.
        # Keep whichever has more valid keypoints (= better homography).
        hough_det = self._detect_hough(frame)
        if hough_det is not None:
            hough_valid = int((hough_det.keypoints[:, 0] >= 0).sum())
            cnn_valid = int((detection.keypoints[:, 0] >= 0).sum()) if detection.homography is not None else 0
            if detection.confidence < COURT_CONFIDENCE_THRESHOLD or hough_valid > cnn_valid:
                logger.info(
                    "court_detect: preferring hough (valid=%d) over cnn (valid=%d, conf=%.2f)",
                    hough_valid, cnn_valid, detection.confidence,
                )
                detection = hough_det

        self._last_detection = detection
        if detection.homography is not None:
            self._last_good_detection = detection
            # Track best calibration detection by inlier count
            valid_mask = detection.keypoints[:, 0] >= 0
            n_inliers = int(valid_mask.sum())
            if n_inliers > self._best_calibration_inliers:
                self._best_calibration_inliers = n_inliers
                logger.info(
                    "court_calibration: new best detection at frame=%d "
                    "inliers=%d confidence=%.2f",
                    frame_idx, n_inliers, detection.confidence,
                )
        self._last_frame_idx = frame_idx

        # Check if calibration period is over
        if frame_idx >= COURT_CALIBRATION_FRAMES and self._locked_detection is None:
            if self._last_good_detection is not None:
                self._locked_detection = self._last_good_detection
                logger.info(
                    "court_calibration: LOCKED best detection after %d frames "
                    "(inliers=%d). No more CNN runs for remaining video.",
                    frame_idx, self._best_calibration_inliers,
                )
            else:
                logger.warning(
                    "court_calibration: no valid detection found in %d frames, "
                    "continuing CNN attempts",
                    frame_idx,
                )

        return detection

    def _detect_cnn(self, frame: np.ndarray) -> CourtDetection:
        """Detect court keypoints from CNN heatmaps.

        Follows yastrebksv/TennisProject reference implementation:
        1. Run CNN → 15-channel heatmap (14 keypoints + center)
        2. For each channel: threshold at 170, find peak via Hough circles
        3. Refine keypoints by cropping around peak, finding lines, snapping
           to their intersection (sub-pixel accuracy)
        4. Compute homography from detected keypoints
        """
        h, w = frame.shape[:2]
        resized = cv2.resize(frame, (TRACKNET_INPUT_WIDTH, TRACKNET_INPUT_HEIGHT))
        tensor = torch.from_numpy(
            resized.astype(np.float32) / 255.0
        ).permute(2, 0, 1).unsqueeze(0).to(self.device)

        with torch.no_grad():
            output = self.model(tensor, testing=True)

        # Output: (1, 15, H*W) — 15 heatmaps. Extract peak (x,y) from each.
        heatmaps = output.squeeze(0).cpu().numpy()  # (15, H*W)
        heatmaps = heatmaps.reshape(15, TRACKNET_INPUT_HEIGHT, TRACKNET_INPUT_WIDTH)

        scale_x = w / TRACKNET_INPUT_WIDTH
        scale_y = h / TRACKNET_INPUT_HEIGHT

        keypoints = np.zeros((COURT_NUM_KEYPOINTS, 2), dtype=np.float32)
        detected_count = 0
        for i in range(COURT_NUM_KEYPOINTS):  # first 14 channels
            hm = heatmaps[i]
            # Convert to uint8 for Hough detection — reference uses threshold 170
            hm_uint8 = (hm * 255).clip(0, 255).astype(np.uint8)
            peak_val = int(hm_uint8.max())

            if peak_val < 170:
                keypoints[i] = [-1, -1]
                continue

            # Threshold + Hough circles (reference: minRadius=10, maxRadius=25)
            _, binary = cv2.threshold(hm_uint8, 170, 255, cv2.THRESH_BINARY)
            circles = cv2.HoughCircles(
                binary, cv2.HOUGH_GRADIENT,
                dp=1, minDist=20,
                param1=50, param2=2,
                minRadius=10, maxRadius=25,
            )

            if circles is not None and len(circles) > 0 and len(circles[0]) > 0:
                cx, cy = float(circles[0][0][0]), float(circles[0][0][1])
            else:
                # Fallback: argmax if Hough fails but peak is above threshold
                peak_idx = np.unravel_index(hm_uint8.argmax(), hm_uint8.shape)
                cx, cy = float(peak_idx[1]), float(peak_idx[0])

            # Refine keypoint via line intersection in a local crop
            refined = self._refine_kp(resized, cx, cy)
            if refined is not None:
                cx, cy = refined

            keypoints[i] = [cx * scale_x, cy * scale_y]
            detected_count += 1

        confidence = detected_count / COURT_NUM_KEYPOINTS

        # Filter to valid keypoints for homography
        valid_mask = keypoints[:, 0] >= 0
        homography = None
        if valid_mask.sum() >= 4 and confidence >= COURT_CONFIDENCE_THRESHOLD:
            homography = self._compute_homography(keypoints, valid_mask)

        return CourtDetection(
            keypoints=keypoints,
            homography=homography,
            confidence=confidence,
            used_fallback=False,
        )

    @staticmethod
    def _refine_kp(frame: np.ndarray, cx: float, cy: float,
                   crop_size: int = 40) -> Optional[tuple]:
        """Refine a keypoint by finding line intersections in a local crop.

        Reference: yastrebksv/TennisCourtDetector postprocess.py::refine_kps()
        Court keypoints sit at line intersections. Crop around the initial
        detection, find lines via Hough, compute their intersection for
        sub-pixel accuracy.
        """
        h, w = frame.shape[:2]
        x1 = max(0, int(cx - crop_size))
        y1 = max(0, int(cy - crop_size))
        x2 = min(w, int(cx + crop_size))
        y2 = min(h, int(cy + crop_size))
        if x2 - x1 < 10 or y2 - y1 < 10:
            return None

        crop = frame[y1:y2, x1:x2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        lines = cv2.HoughLinesP(
            binary, 1, np.pi / 180, 20,
            minLineLength=10, maxLineGap=5,
        )
        if lines is None or len(lines) < 2:
            return None

        # Find two dominant lines (most different angles)
        line_list = [tuple(l[0]) for l in lines]
        if len(line_list) < 2:
            return None

        # Pick the two lines with the most different angles
        best_pair = None
        best_angle_diff = 0
        for i in range(len(line_list)):
            a1 = np.arctan2(line_list[i][3] - line_list[i][1],
                            line_list[i][2] - line_list[i][0])
            for j in range(i + 1, len(line_list)):
                a2 = np.arctan2(line_list[j][3] - line_list[j][1],
                                line_list[j][2] - line_list[j][0])
                diff = abs(a1 - a2) % np.pi
                if diff > best_angle_diff:
                    best_angle_diff = diff
                    best_pair = (line_list[i], line_list[j])

        if best_pair is None or best_angle_diff < np.pi / 6:  # need >30° angle
            return None

        # Compute intersection
        l1, l2 = best_pair
        denom = (l1[0] - l1[2]) * (l2[1] - l2[3]) - (l1[1] - l1[3]) * (l2[0] - l2[2])
        if abs(denom) < 1e-10:
            return None
        t = ((l1[0] - l2[0]) * (l2[1] - l2[3]) - (l1[1] - l2[1]) * (l2[0] - l2[2])) / denom
        ix = l1[0] + t * (l1[2] - l1[0]) + x1
        iy = l1[1] + t * (l1[3] - l1[1]) + y1

        # Sanity: refined point should be close to original
        if abs(ix - cx) > crop_size or abs(iy - cy) > crop_size:
            return None

        return (float(ix), float(iy))

    def _compute_homography(self, detected_kps: np.ndarray, valid_mask: np.ndarray = None) -> Optional[np.ndarray]:
        if valid_mask is None:
            valid_mask = detected_kps[:, 0] >= 0
        n_valid = int(valid_mask.sum())
        if n_valid < 4:
            logger.warning("_compute_homography: only %d valid keypoints (need 4)", n_valid)
            return None
        src = detected_kps[valid_mask].astype(np.float32)
        dst = self.ref_keypoints[valid_mask[:len(self.ref_keypoints)]].astype(np.float32)
        n = min(len(src), len(dst))
        if n < 4:
            logger.warning("_compute_homography: src/dst mismatch — src=%d dst=%d", len(src), len(dst))
            return None
        H, mask = cv2.findHomography(src[:n], dst[:n], cv2.RANSAC, 5.0)
        if H is None:
            logger.warning("_compute_homography: findHomography returned None with %d points", n)
            return None

        # ── Validation strategy ──
        # 1. RANSAC inlier count: need at least MIN_INLIERS good points
        # 2. Scale factor sanity: |H[0][0]| and |H[1][1]| should be < 50
        #    (A reasonable image-to-reference court mapping has scale ~0.5-10)
        # 3. Inlier-only mean reprojection error: should be small by RANSAC
        #    construction, but double-check it's < 10px
        MIN_INLIERS = 4  # mathematical minimum for findHomography
        MAX_SCALE = 20.0  # reasonable image-to-reference court mapping is 1-10
        MAX_INLIER_ERR_PX = 15.0

        n_inliers = int(mask.sum()) if mask is not None else 0

        if n_inliers < MIN_INLIERS:
            logger.warning(
                "_compute_homography: REJECTED low inliers n_inliers=%d (need %d) H_diag=[%.2f, %.2f]",
                n_inliers, MIN_INLIERS, H[0, 0], H[1, 1],
            )
            return None

        if abs(H[0, 0]) > MAX_SCALE or abs(H[1, 1]) > MAX_SCALE or not np.isfinite(H).all():
            logger.warning(
                "_compute_homography: REJECTED bad scale H_diag=[%.2f, %.2f] (max %.0f)",
                H[0, 0], H[1, 1], MAX_SCALE,
            )
            return None

        # Inlier-only reprojection check
        try:
            inlier_mask = mask.flatten().astype(bool)
            in_src = src[:n][inlier_mask]
            in_dst = dst[:n][inlier_mask]
            in_src_h = np.hstack([in_src, np.ones((len(in_src), 1), dtype=np.float32)])
            projected = (H @ in_src_h.T).T
            w = projected[:, 2:3]
            w = np.where(np.abs(w) < 1e-10, 1.0, w)
            projected_2d = projected[:, :2] / w
            inlier_errs = np.linalg.norm(projected_2d - in_dst, axis=1)
            inlier_mean_err = float(np.mean(inlier_errs))
        except Exception as e:
            logger.warning("_compute_homography: inlier reproj check failed: %s", e)
            inlier_mean_err = 0.0

        if inlier_mean_err > MAX_INLIER_ERR_PX:
            logger.warning(
                "_compute_homography: REJECTED inlier_err=%.1f H_diag=[%.2f, %.2f]",
                inlier_mean_err, H[0, 0], H[1, 1],
            )
            return None

        logger.info(
            "_compute_homography: OK inliers=%d/%d inlier_err=%.1f H_diag=[%.2f, %.2f]",
            n_inliers, n, inlier_mean_err, H[0, 0], H[1, 1],
        )
        return H

    def _detect_hough(self, frame: np.ndarray) -> Optional[CourtDetection]:
        """Robust fallback: detect white court lines via color mask + Hough.

        Strategy for a fixed indoor camera on a blue court:
        1. Extract white pixels (court lines are white on blue/green surface)
        2. Find line segments via HoughLinesP
        3. Cluster lines by angle: near-horizontal and near-vertical
        4. Cluster horizontal lines by y-position → identify up to 4 lines
           (far baseline, far service line, near service line, near baseline)
        5. Cluster vertical lines by x-position → identify sidelines + center
        6. Compute intersections to get keypoint positions
        7. Build homography from all identified keypoints
        """
        h, w = frame.shape[:2]

        # Step 1: White line mask — court lines are bright white
        # Use HSV: low saturation + high value = white
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        # Also use grayscale brightness as a second signal
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # White: S < 50, V > 180 (generous for indoor lighting)
        white_mask = cv2.inRange(hsv, (0, 0, 180), (180, 50, 255))
        # Also include very bright pixels from grayscale
        _, bright_mask = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        combined = cv2.bitwise_or(white_mask, bright_mask)
        # Morphological close to connect broken line segments
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 2))
        combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel)

        # Step 2: Hough lines on the white mask
        lines = cv2.HoughLinesP(
            combined, HOUGH_RHO, np.pi / HOUGH_THETA_DIVISOR,
            HOUGH_THRESHOLD, minLineLength=HOUGH_MIN_LINE_LENGTH,
            maxLineGap=HOUGH_MAX_LINE_GAP,
        )
        if lines is None or len(lines) < 4:
            return None

        # Step 3: Classify by angle — perspective means "horizontal" lines
        # aren't truly horizontal, so use a wider angle tolerance
        h_lines, v_lines = [], []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            angle = np.arctan2(y2 - y1, x2 - x1) * 180 / np.pi
            abs_angle = abs(angle)
            length = np.hypot(x2 - x1, y2 - y1)
            if length < 50:
                continue  # skip short fragments
            if abs_angle < 45 or abs_angle > 135:
                h_lines.append(line[0])
            elif 45 <= abs_angle <= 135:
                v_lines.append(line[0])

        if len(h_lines) < 2 or len(v_lines) < 2:
            return None

        # Step 4: Cluster horizontal lines by average y-position
        h_clusters = self._cluster_lines_by_position(
            h_lines, axis="y", min_separation=h * 0.03,
        )
        # Step 5: Cluster vertical lines by average x-position
        v_clusters = self._cluster_lines_by_position(
            v_lines, axis="x", min_separation=w * 0.03,
        )

        if len(h_clusters) < 2 or len(v_clusters) < 2:
            return None

        # Sort horizontal clusters top-to-bottom (by avg y)
        h_clusters.sort(key=lambda c: np.mean([(l[1] + l[3]) / 2 for l in c]))
        # Sort vertical clusters left-to-right (by avg x)
        v_clusters.sort(key=lambda c: np.mean([(l[0] + l[2]) / 2 for l in c]))

        # Step 6: Build representative lines from clusters (average endpoints)
        h_reps = [self._cluster_representative(c) for c in h_clusters]
        v_reps = [self._cluster_representative(c) for c in v_clusters]

        # We need at least the 2 baselines and 2 outer sidelines
        top_baseline = h_reps[0]
        bot_baseline = h_reps[-1]
        left_sideline = v_reps[0]
        right_sideline = v_reps[-1]

        # Compute the 4 corner intersections (baseline × sideline)
        kp0 = self._line_intersection(top_baseline, left_sideline)   # top-left
        kp1 = self._line_intersection(top_baseline, right_sideline)  # top-right
        kp2 = self._line_intersection(bot_baseline, left_sideline)   # bottom-left
        kp3 = self._line_intersection(bot_baseline, right_sideline)  # bottom-right

        if any(p is None for p in [kp0, kp1, kp2, kp3]):
            return None

        keypoints = np.full((COURT_NUM_KEYPOINTS, 2), -1.0, dtype=np.float32)
        keypoints[0] = kp0  # baseline top L
        keypoints[1] = kp1  # baseline top R
        keypoints[2] = kp2  # baseline bottom L
        keypoints[3] = kp3  # baseline bottom R

        # Helper: assign intersection result to keypoint only if non-None
        def _set_kp(idx, line_a, line_b):
            pt = self._line_intersection(line_a, line_b)
            if pt is not None:
                keypoints[idx] = pt

        # Try to identify inner sidelines (singles lines) if we have 4+ vertical clusters
        if len(v_reps) >= 4:
            _set_kp(4, top_baseline, v_reps[1])   # left inner top
            _set_kp(5, bot_baseline, v_reps[1])   # left inner bot
            _set_kp(6, top_baseline, v_reps[-2])  # right inner top
            _set_kp(7, bot_baseline, v_reps[-2])  # right inner bot

        # Try to identify service lines if we have 4+ horizontal clusters
        if len(h_reps) >= 4:
            # 4 horizontal: far baseline, far service, near service, near baseline
            far_svc = h_reps[1]
            near_svc = h_reps[-2]
            _set_kp(8,  far_svc,  left_sideline)
            _set_kp(9,  far_svc,  right_sideline)
            _set_kp(10, near_svc, left_sideline)
            _set_kp(11, near_svc, right_sideline)

            # Center service line — if we have a middle vertical line
            if len(v_reps) >= 3:
                center_v = v_reps[len(v_reps) // 2]
                _set_kp(12, far_svc,  center_v)
                _set_kp(13, near_svc, center_v)

        valid_mask = keypoints[:, 0] >= 0
        n_valid = int(valid_mask.sum())
        confidence = n_valid / COURT_NUM_KEYPOINTS
        logger.info(
            "_detect_hough: found %d/%d keypoints from %d h_clusters × %d v_clusters",
            n_valid, COURT_NUM_KEYPOINTS, len(h_clusters), len(v_clusters),
        )

        homography = None
        if n_valid >= 4:
            homography = self._compute_homography(keypoints, valid_mask)

        if homography is None:
            return None

        return CourtDetection(
            keypoints=keypoints,
            homography=homography,
            confidence=confidence,
            used_fallback=True,
        )

    @staticmethod
    def _cluster_lines_by_position(lines: list, axis: str, min_separation: float) -> list:
        """Cluster line segments by their average position on the given axis.

        Groups lines whose average position differs by less than min_separation.
        Returns a list of clusters, each cluster being a list of line segments.
        """
        if not lines:
            return []

        def avg_pos(line):
            x1, y1, x2, y2 = line
            if axis == "y":
                return (y1 + y2) / 2
            return (x1 + x2) / 2

        sorted_lines = sorted(lines, key=avg_pos)
        clusters = [[sorted_lines[0]]]
        for line in sorted_lines[1:]:
            pos = avg_pos(line)
            cluster_pos = np.mean([avg_pos(l) for l in clusters[-1]])
            if abs(pos - cluster_pos) < min_separation:
                clusters[-1].append(line)
            else:
                clusters.append([line])
        return clusters

    @staticmethod
    def _cluster_representative(cluster: list) -> tuple:
        """Compute a representative line from a cluster of line segments.

        Returns (x1, y1, x2, y2) — the average of all segment endpoints,
        extended to span the full range of the cluster.
        """
        all_points = []
        for x1, y1, x2, y2 in cluster:
            all_points.append((x1, y1))
            all_points.append((x2, y2))
        pts = np.array(all_points, dtype=np.float32)
        # Fit a line through all points using least squares
        if len(pts) < 2:
            return tuple(cluster[0])
        # Sort by x for near-horizontal, by y for near-vertical
        x_range = pts[:, 0].max() - pts[:, 0].min()
        y_range = pts[:, 1].max() - pts[:, 1].min()
        if x_range >= y_range:
            # Near-horizontal: parameterize by x
            sorted_pts = pts[pts[:, 0].argsort()]
            x_min, x_max = sorted_pts[0, 0], sorted_pts[-1, 0]
            if x_max - x_min < 1:
                return tuple(cluster[0])
            coeffs = np.polyfit(pts[:, 0], pts[:, 1], 1)
            y_min = np.polyval(coeffs, x_min)
            y_max = np.polyval(coeffs, x_max)
            return (float(x_min), float(y_min), float(x_max), float(y_max))
        else:
            # Near-vertical: parameterize by y
            sorted_pts = pts[pts[:, 1].argsort()]
            y_min, y_max = sorted_pts[0, 1], sorted_pts[-1, 1]
            if y_max - y_min < 1:
                return tuple(cluster[0])
            coeffs = np.polyfit(pts[:, 1], pts[:, 0], 1)
            x_min = np.polyval(coeffs, y_min)
            x_max = np.polyval(coeffs, y_max)
            return (float(x_min), float(y_min), float(x_max), float(y_max))

    @staticmethod
    def _line_intersection(line1, line2):
        x1, y1, x2, y2 = line1
        x3, y3, x4, y4 = line2
        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-10:
            return None
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        px = x1 + t * (x2 - x1)
        py = y1 + t * (y2 - y1)
        return np.array([px, py], dtype=np.float32)

    _coord_log_count = 0

    def to_court_coords(self, pixel_x: float, pixel_y: float) -> Optional[tuple]:
        """Convert pixel coordinates to real-world court coordinates (metres).

        Uses the most recent detection with a valid homography — not necessarily
        _last_detection, which may have a failed homography on the final frame.
        """
        # Prefer _last_detection if it has homography, fall back to _last_good_detection
        det = self._last_detection
        if det is None or det.homography is None:
            det = self._last_good_detection
        if det is None or det.homography is None:
            if self._coord_log_count < 3:
                logger.warning(
                    "to_court_coords: returning None — no valid homography available",
                )
                self._coord_log_count += 1
            return None
        H = det.homography
        pt = np.array([pixel_x, pixel_y, 1.0])
        court_pt = H @ pt
        if abs(court_pt[2]) < 1e-10:
            return None
        court_pt = court_pt[:2] / court_pt[2]

        ref = self.ref_keypoints
        ref_w = ref[1][0] - ref[0][0]
        ref_h = ref[2][1] - ref[0][1]
        if ref_w == 0 or ref_h == 0:
            return None

        mx = (court_pt[0] - ref[0][0]) / ref_w * COURT_WIDTH_DOUBLES_M
        my = (court_pt[1] - ref[0][1]) / ref_h * COURT_LENGTH_M

        # Sanity check: court is 10.97m x 23.77m. Allow ±5m of slop for balls
        # outside the lines, but reject anything wildly outside (e.g. -50, 300).
        # This catches the case where the homography passed validation but produces
        # garbage on certain pixel inputs.
        if not (-5.0 <= mx <= COURT_WIDTH_DOUBLES_M + 5.0 and
                -5.0 <= my <= COURT_LENGTH_M + 5.0):
            return None

        return (float(mx), float(my))

    def get_court_corners_pixels(self) -> Optional[list]:
        """Return the 4 baseline corner keypoints as pixel coordinates.

        Returns [(x,y), (x,y), (x,y), (x,y)] in order:
          [0] far baseline left   (top-left in image)
          [1] far baseline right  (top-right)
          [2] near baseline left  (bottom-left)
          [3] near baseline right (bottom-right)

        Returns None if fewer than 4 corners are detected.
        """
        det = self._last_detection
        if det is None or det.homography is None:
            det = self._last_good_detection
        if det is None:
            return None
        kps = det.keypoints
        # Keypoints 0-3 are the 4 baseline corners
        corners = []
        for i in range(4):
            if kps[i][0] >= 0 and kps[i][1] >= 0:
                corners.append((float(kps[i][0]), float(kps[i][1])))
            else:
                return None  # need all 4
        return corners

    def get_court_bbox_pixels(self) -> Optional[tuple]:
        """Return (x_min, y_min, x_max, y_max) bounding box of detected court.

        Prefers the last detection with a VALID homography — falls back to
        _last_good_detection if the most recent detection had bad/no homography.
        This prevents bad keypoints from one frame poisoning the player filter
        on subsequent frames.
        """
        det = self._last_detection
        if det is None or det.homography is None:
            det = self._last_good_detection
        if det is None:
            return None
        kps = det.keypoints
        valid = kps[kps[:, 0] >= 0]
        if len(valid) == 0:
            return None
        return (
            float(valid[:, 0].min()),
            float(valid[:, 1].min()),
            float(valid[:, 0].max()),
            float(valid[:, 1].max()),
        )
