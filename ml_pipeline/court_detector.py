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

from ml_pipeline.camera_calibration import (
    CalibrationResult,
    evaluate_calibration,
    fit_calibration,
    project_metres_to_pixel,
    project_pixel_to_metres,
)

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

# ── Calibration-lock policy (2026-05-28 frame-selection fix) ────────────────
# Root cause of the silent-degeneracy failure (match 4 / f11eed2c): the lock
# fired at COURT_CALIBRATION_FRAMES on whatever "best" detection existed —
# including an ANY-BEST Hough fabrication — when the opening window was
# unrepresentative (pre-match / setup / panning). The CNN actually nails
# 12-13/14 keypoints seconds later on rally footage. Fix:
#   • lock ONLY on a geometry-VALIDATED, non-degenerate detection
#   • keep sampling the CNN PAST the initial window until we have enough
#     validated observations (or hit the safety ceiling)
#   • never lock an ANY-BEST/Hough detection; fail-loud if the whole search
#     budget yields nothing validated.
COURT_MIN_CALIB_OBS = 12           # validated observations required before the FIRST lock attempt
COURT_LOCK_RETRY_OBS_STEP = 4      # re-attempt the fit only after this many more obs (bounds fit cost)
COURT_LOCK_HARD_CAP_FRAMES = 36000  # safety ceiling (~24 min @25fps) → forced decision / fail-loud
COURT_DEGEN_COND_MAX = 1.0e10      # homography condition-number ceiling (loose backstop)
COURT_DEGEN_CORNER_MARGIN = 0.5    # reprojected court corners may fall at most this×dim outside frame
COURT_DEGEN_MIN_AREA_FRAC = 0.02   # reprojected court quad must cover ≥ this fraction of frame area
# Positive projection self-test (2026-05-29 match-4 fix): a locked calibration
# must actually project a grid of court-region pixels into the court. Catches
# overfit/degenerate fits (clustered obs → piecewise rms~0 → 0% coverage) that
# pass every geometric gate but project nothing — the silent gap that let
# match 4 lock a "VALIDATED" but unusable calibration. Healthy MATCHi/club
# courts project well above this floor; an overfit collapses below it.
COURT_MIN_PROJECTION_COVERAGE = 0.35
# …and the projection must spread across the court, not collapse to a line
# (match 4's piecewise fit landed 80% in-band while pinning every pixel to
# y≈23.77). Robust p90−p10 spans, in court metres. Full court is 10.97×23.77 m;
# these floors admit half-court framings while rejecting a degenerate collapse.
COURT_MIN_PROJECTION_Y_SPAN = 6.0
COURT_MIN_PROJECTION_X_SPAN = 3.0
# Lock the BEST fit, not the first above the floor (2026-05-29 prod-vs-local
# gap: prod locked the first floor-passing fit at 43% coverage while a tighter
# ~86% fit was reachable a few observations later). Keep searching until a fit
# reaches this "great" bar (early-exit), else lock the best-so-far at the cap.
COURT_GREAT_COVERAGE = 0.70


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
        self._best_detection: Optional[CourtDetection] = None            # any homography, by inliers
        self._best_validated_detection: Optional[CourtDetection] = None  # homography that passes geometry
        self._best_calibration_inliers: int = 0
        self._best_validated_inliers: int = 0
        self._calibration: Optional[CalibrationResult] = None
        self._calibration_observations: list[np.ndarray] = []
        self._geom_fail_log_count: int = 0
        # next observation count at which to (re)attempt a lock fit — grows by
        # COURT_LOCK_RETRY_OBS_STEP each failed attempt so we don't re-fit every
        # frame while waiting for more/spread observations.
        self._next_lock_attempt_obs: int = COURT_MIN_CALIB_OBS
        # best floor-passing calibration seen across attempts (lock-best, not
        # lock-first): (calibration, detection, coverage, y_span, x_span).
        self._best_lock: Optional[tuple] = None
        self._best_lock_cov: float = -1.0

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
        # Prefer CNN in almost all cases — Hough is only a fallback for when
        # the CNN fully fails (zero valid keypoints). Previously we switched
        # to Hough whenever it had MORE keypoints, but Hough systematically
        # mis-labels keypoints on wide-angle indoor footage (picks sponsor
        # banner / logo edges as "baselines"), producing a high keypoint
        # count that's actually all wrong.
        hough_det = self._detect_hough(frame)
        cnn_kps_count = int((detection.keypoints[:, 0] >= 0).sum())
        if hough_det is not None and cnn_kps_count == 0:
            hough_valid = int((hough_det.keypoints[:, 0] >= 0).sum())
            logger.info(
                "court_detect: using hough fallback (valid=%d) because CNN returned 0 keypoints",
                hough_valid,
            )
            detection = hough_det

        self._last_detection = detection
        valid_mask = detection.keypoints[:, 0] >= 0
        n_kps = int(valid_mask.sum())

        # A frame counts as a calibration observation / lock candidate only if
        # its keypoints pass the perspective sanity check (geom_ok) AND its
        # homography is non-degenerate (Fix B). Gating observations on
        # non-degeneracy keeps the radial calibration and the locked single-H
        # fallback consistent — a "validated" window can never be a Hough
        # fabrication, so enough validated observations always implies a real
        # best-validated detection exists.
        validated = False
        if n_kps >= 8:
            geom_ok = self._validate_homography_geometry(
                detection.keypoints, valid_mask,
                frame_h=frame.shape[0], frame_idx=frame_idx,
            )
            if geom_ok and detection.homography is not None:
                degen, _dreason = self._homography_degenerate(
                    detection.homography, frame.shape[1], frame.shape[0])
                validated = not degen
                if degen and self._geom_fail_log_count < 20:
                    logger.info(
                        "court_calibration: frame=%d geometry-pass but degenerate H (%s)",
                        frame_idx, _dreason)
                    self._geom_fail_log_count += 1
        else:
            geom_ok = False
        if validated:
            self._calibration_observations.append(detection.keypoints.copy())

        if detection.homography is not None:
            self._last_good_detection = detection

            # Track best-any (highest inliers regardless of geometry)
            if n_kps > self._best_calibration_inliers:
                self._best_calibration_inliers = n_kps
                self._best_detection = detection
                logger.info(
                    "court_calibration: new best-ANY at frame=%d "
                    "inliers=%d confidence=%.2f geometry=%s",
                    frame_idx, n_kps, detection.confidence,
                    "PASS" if geom_ok else "FAIL",
                )
            # Track best-validated (geometry-valid AND non-degenerate — Fix B/G)
            if validated and n_kps > self._best_validated_inliers:
                self._best_validated_inliers = n_kps
                self._best_validated_detection = detection
                logger.info(
                    "court_calibration: new best-VALIDATED at frame=%d "
                    "inliers=%d confidence=%.2f",
                    frame_idx, n_kps, detection.confidence,
                )
        self._last_frame_idx = frame_idx

        # ── Lock decision (Fix G frame-selection + projection self-test) ──
        # Lock ONLY on a geometry-validated detection whose fitted calibration
        # ACTUALLY PROJECTS the court (the 2026-05-29 match-4 fix). Clustered or
        # too-few observations yield an overfit/singular lens fit that passes
        # every geometric gate but projects ~0% coverage (match 4 locked a
        # "VALIDATED" piecewise fit on 8 clustered obs → 0/23,795 court coords).
        # We reject those via _projection_coverage and keep sampling the CNN for
        # more / better-spread observations rather than freezing an unusable
        # calibration. Re-attempt only as obs grow (bounds fit cost); force a
        # decision at the hard cap (fail-loud, never lock garbage).
        n_obs = len(self._calibration_observations)
        have_validated = self._best_validated_detection is not None
        past_window = frame_idx >= COURT_CALIBRATION_FRAMES
        hit_cap = frame_idx >= COURT_LOCK_HARD_CAP_FRAMES

        if self._locked_detection is None and (
            (past_window and have_validated and n_obs >= self._next_lock_attempt_obs)
            or hit_cap
        ):
            self._next_lock_attempt_obs = n_obs + COURT_LOCK_RETRY_OBS_STEP
            cal = None
            if self._calibration_observations:
                try:
                    cal = fit_calibration(
                        self._calibration_observations,
                        img_shape=frame.shape[:2],
                        rms_threshold_px=10.0,
                    )
                except Exception as e:  # e.g. singular matrix on clustered obs
                    logger.info(
                        "court_calibration: fit raised (%s) — treating as no fit", e)
                    cal = None
            best = self._best_validated_detection
            cov, y_span, x_span = self._projection_quality(cal, frame.shape[:2])
            passed = (cal is not None and best is not None
                      and cov >= COURT_MIN_PROJECTION_COVERAGE
                      and y_span >= COURT_MIN_PROJECTION_Y_SPAN
                      and x_span >= COURT_MIN_PROJECTION_X_SPAN)

            # Track the BEST floor-passing fit across attempts (lock-best, not
            # lock-first — closes the prod 43%-vs-local-86% gap: prod froze the
            # first fit above the floor while a tighter one was a few obs away).
            if passed and cov > self._best_lock_cov:
                self._best_lock_cov = cov
                self._best_lock = (cal, best, cov, y_span, x_span)

            if passed and cov >= COURT_GREAT_COVERAGE:
                # Great fit — early-exit, lock it now.
                lock_cal, lock_det, lcov, lys, lxs = cal, best, cov, y_span, x_span
            elif hit_cap and self._best_lock is not None:
                # Search budget exhausted — lock the best fit found so far (it
                # cleared the floor; it just never reached the 'great' bar).
                lock_cal, lock_det, lcov, lys, lxs = self._best_lock
                logger.info(
                    "court_calibration: hard cap at frame=%d — locking BEST-so-far "
                    "cov=%.0f%% (great bar %.0f%% never reached).",
                    frame_idx, 100.0 * lcov, 100.0 * COURT_GREAT_COVERAGE)
            elif hit_cap:
                # Nothing ever projected the court → fail-loud.
                logger.warning(
                    "court_calibration: FAIL-LOUD after %d frames — no calibration "
                    "projected the court (latest cov=%.0f%% y_span=%.1fm x_span=%.1fm, "
                    "obs=%d). Refusing to lock an unusable calibration.",
                    frame_idx, 100.0 * cov, y_span, x_span, n_obs,
                )
                raise RuntimeError(
                    f"court_calibration: FAILED after {frame_idx} frames — no "
                    f"calibration that projects the court (best "
                    f"cov={100.0 * max(cov, self._best_lock_cov):.0f}%)."
                )
            else:
                # Passing-but-not-great, or below floor — keep sampling for a
                # better fit. Do NOT freeze a mediocre/collapsed calibration.
                logger.info(
                    "court_calibration: candidate at frame=%d cov=%.0f%% y_span=%.1fm "
                    "x_span=%.1fm (mode=%s, obs=%d, best=%.0f%%) — below great bar "
                    "%.0f%%; keep searching.",
                    frame_idx, 100.0 * cov, y_span, x_span,
                    None if cal is None else cal.mode, n_obs,
                    100.0 * max(self._best_lock_cov, 0.0), 100.0 * COURT_GREAT_COVERAGE,
                )
                return detection

            # Commit the chosen lock.
            self._calibration = lock_cal
            self._locked_detection = lock_det
            cov, y_span, x_span = lcov, lys, lxs
            locked_inliers = int((lock_det.keypoints[:, 0] >= 0).sum())
            logger.info(
                "court_calibration: LOCKED after %d frames — mode=%s rms=%.3fpx obs=%d "
                "cov=%.0f%% y_span=%.1fm x_span=%.1fm (inliers=%d, conf=%.2f). No more CNN runs.",
                frame_idx, lock_cal.mode, lock_cal.rms_px, n_obs, 100.0 * cov, y_span, x_span,
                locked_inliers, lock_det.confidence,
            )
            if self._calibration is not None:
                logger.info(
                    "court_calibration: lens calibration locked — mode=%s rms=%.4f px "
                    "from %d observations",
                    self._calibration.mode, self._calibration.rms_px,
                    len(self._calibration_observations),
                )
                errors_m = evaluate_calibration(
                    self._calibration, self._calibration_observations,
                )
                valid = ~np.isnan(errors_m)
                if valid.any():
                    mean_err = float(np.nanmean(errors_m))
                    max_err = float(np.nanmax(errors_m))
                    p90_err = float(np.nanpercentile(errors_m[valid], 90))
                    logger.info(
                        "court_calibration: per-keypoint errors (metres): "
                        "mean=%.3f p90=%.3f max=%.3f",
                        mean_err, p90_err, max_err,
                    )
                    kp_names = [
                        "bl_top_L", "bl_top_R", "bl_bot_L", "bl_bot_R",
                        "sg_top_L", "sg_bot_L", "sg_top_R", "sg_bot_R",
                        "sv_top_L", "sv_top_R", "sv_bot_L", "sv_bot_R",
                        "ctr_top",  "ctr_bot",
                    ]
                    for i, name in enumerate(kp_names):
                        if valid[i]:
                            fn = logger.warning if errors_m[i] > 0.5 else logger.info
                            fn("court_kp_err[%02d] %s err=%.3fm", i, name, errors_m[i])
                    if max_err > 0.5:
                        logger.warning(
                            "court_calibration: max keypoint error %.3fm exceeds 0.5m "
                            "— calibration quality SUSPECT",
                            max_err,
                        )
            else:
                logger.warning(
                    "court_calibration: lens calibration failed (both Option A and C) "
                    "from %d observations — falling back to single homography",
                    len(self._calibration_observations),
                )
            if self._locked_detection is not None:
                # Log the detected pixel positions of every keypoint so we
                # can diagnose mis-labeled keypoints (e.g. net mistaken for
                # far baseline). Pair each with its reference position for
                # side-by-side sanity.
                kp_names = [
                    "bl_top_L", "bl_top_R", "bl_bot_L", "bl_bot_R",
                    "sg_top_L", "sg_bot_L", "sg_top_R", "sg_bot_R",
                    "sv_top_L", "sv_top_R", "sv_bot_L", "sv_bot_R",
                    "ctr_top",  "ctr_bot",
                ]
                for i, name in enumerate(kp_names):
                    px = self._locked_detection.keypoints[i]
                    ref = self.ref_keypoints[i]
                    det_str = f"({px[0]:.0f},{px[1]:.0f})" if px[0] >= 0 else "(MISSING)"
                    logger.info(
                        "court_kps[%02d] %s detected=%s ref=(%d,%d)",
                        i, name, det_str, ref[0], ref[1],
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

    def _validate_homography_geometry(self, keypoints: np.ndarray,
                                       valid_mask: np.ndarray,
                                       frame_h: int = 0,
                                       frame_idx: int = -1) -> bool:
        """Sanity-check detected keypoints for valid tennis-court perspective.

        Guards against two known failure modes:
          A. "Net mis-detected as far baseline" — CNN labels net-line pixels
             as baseline keypoints → compressed homography.
          B. "Banner + logo mis-detected as baselines" — Hough fallback
             finds horizontal clusters at the scoreboard top and the
             MATCHI TV logo bottom → baselines span 90%+ of frame height.

        Rules (pixel-space):
          1. Far baseline (kps 0-1) must sit ABOVE near baseline (kps 2-3)
          2. Vertical separation must be meaningful (>= 150 pixels @ 1080p)
          3. If all 4 baseline corners detected: far baseline must be
             narrower than near baseline (perspective). 0.75 threshold
             catches the "net line as far baseline" case.
          4. Baseline span must be 25-70% of frame height (when frame_h
             given). Real tennis courts don't fill the whole image.
          5. Far baseline must not be in the top 8% (scoreboard/banner).
          6. Near baseline must not be in the bottom 5% (logo/border).

        Returns True if geometry looks valid OR we don't have enough
        baseline keypoints to judge. False only if geometry is clearly wrong.
        """
        far_detected = [i for i in (0, 1) if i < len(valid_mask) and valid_mask[i]]
        near_detected = [i for i in (2, 3) if i < len(valid_mask) and valid_mask[i]]
        if not far_detected or not near_detected:
            return True  # insufficient evidence — don't reject on geometry

        far_y = float(np.mean([keypoints[i, 1] for i in far_detected]))
        near_y = float(np.mean([keypoints[i, 1] for i in near_detected]))

        def _reject(reason: str) -> bool:
            if frame_idx >= 0 and self._geom_fail_log_count < 20:
                logger.info("geom_fail frame=%d: %s", frame_idx, reason)
                self._geom_fail_log_count += 1
            return False

        if far_y >= near_y:
            return _reject(f"far_y({far_y:.0f}) >= near_y({near_y:.0f})")
        if (near_y - far_y) < 150:
            return _reject(f"span({near_y - far_y:.0f}px) < 150")

        if len(far_detected) == 2 and len(near_detected) == 2:
            far_w = abs(keypoints[1, 0] - keypoints[0, 0])
            near_w = abs(keypoints[3, 0] - keypoints[2, 0])
            # 0.85 threshold: wide-angle indoor courts can have a cropped far
            # baseline where far_w is closer to near_w than on broadcast TV
            # tennis. 0.75 was too strict — rejected real courts.
            if far_w >= near_w * 0.85:
                return _reject(f"perspective far_w={far_w:.0f} >= 0.85*near_w={near_w * 0.85:.0f}")

        if frame_h > 0:
            span = near_y - far_y
            # 90% threshold: MATCHI-style wide-angle indoor cameras frame
            # the court to fill most of the vertical extent — legitimate
            # span is often 80-88%. 70% was calibrated for broadcast tennis
            # and rejected every valid detection on club footage.
            if span > frame_h * 0.90:
                return _reject(f"span {span:.0f}px > 90% of frame_h {frame_h}")
            if span < frame_h * 0.25:
                return _reject(f"span {span:.0f}px < 25% of frame_h {frame_h}")
            if far_y < frame_h * 0.04:
                return _reject(f"far_y {far_y:.0f} in top 4% ({int(frame_h * 0.04)})")
            if near_y > frame_h * 0.98:
                return _reject(f"near_y {near_y:.0f} in bottom 2% ({int(frame_h * 0.98)})")

        return True

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
        # 2. Inlier-only mean reprojection error < MAX_INLIER_ERR_PX
        # 3. Sanity-only finite-values check on H (filters NaN/Inf blowups)
        # Previously had a MAX_SCALE=20 diagonal check, removed because
        # wide-angle indoor cameras legitimately produce H_diag > 20 and it
        # was rejecting valid CNN detections; reprojection error is the
        # correct quality gate.
        MIN_INLIERS = 4
        MAX_INLIER_ERR_PX = 15.0

        n_inliers = int(mask.sum()) if mask is not None else 0

        if n_inliers < MIN_INLIERS:
            logger.warning(
                "_compute_homography: REJECTED low inliers n_inliers=%d (need %d) H_diag=[%.2f, %.2f]",
                n_inliers, MIN_INLIERS, H[0, 0], H[1, 1],
            )
            return None

        if not np.isfinite(H).all():
            logger.warning(
                "_compute_homography: REJECTED non-finite H (NaN or Inf values)",
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

    def _homography_degenerate(self, H: Optional[np.ndarray],
                                frame_w: int, frame_h: int) -> tuple:
        """Geometric degeneracy gate (Fix B, 2026-05-28).

        Reprojects the four doubles-court corners (ref keypoints 0-3) BACK to
        image pixels via H⁻¹ and rejects the homography if the result is not a
        sane, in-frame, convex quad. This catches the degenerate Hough
        fabrications (e.g. H_diag=[21.43, 0.05]) that pass the inlier /
        reprojection checks but collapse the court to a line or fling the
        corners off-screen.

        IMPORTANT: we deliberately do NOT gate on the H diagonal / scale.
        Legitimate wide-angle indoor courts (MATCHi) carry H_diag up to
        ~1142 yet project perfectly — an H-diag range gate would reject the
        bench-fixture court. The geometric quad test is scale-agnostic.

        Returns (is_degenerate: bool, reason: str).
        """
        if H is None or not np.isfinite(H).all():
            return True, "non-finite"
        try:
            cond = float(np.linalg.cond(H))
        except Exception:
            return True, "cond-failed"
        if not np.isfinite(cond) or cond > COURT_DEGEN_COND_MAX:
            return True, f"cond={cond:.1e}"
        try:
            Hinv = np.linalg.inv(H)
        except np.linalg.LinAlgError:
            return True, "singular"
        # ref corners 0=top-L, 1=top-R, 2=bot-L, 3=bot-R (court ref pixel space)
        corners_ref = self.ref_keypoints[:4].astype(np.float64)
        pts = np.hstack([corners_ref, np.ones((4, 1))])
        proj = (Hinv @ pts.T).T
        w = proj[:, 2:3]
        if np.any(np.abs(w) < 1e-9):
            return True, "w~0"
        img = proj[:, :2] / w  # (4,2) image-pixel corners
        mx, my = frame_w * COURT_DEGEN_CORNER_MARGIN, frame_h * COURT_DEGEN_CORNER_MARGIN
        if (np.any(img[:, 0] < -mx) or np.any(img[:, 0] > frame_w + mx) or
                np.any(img[:, 1] < -my) or np.any(img[:, 1] > frame_h + my)):
            return True, "corner-out-of-frame"
        # Order as polygon tl,tr,br,bl (ref order is tl,tr,bl,br → indices 0,1,3,2)
        quad = img[[0, 1, 3, 2]]
        x, y = quad[:, 0], quad[:, 1]
        area = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
        if area < COURT_DEGEN_MIN_AREA_FRAC * frame_w * frame_h:
            return True, f"area-small({area:.0f})"
        # Convexity: all cross-products of consecutive edges share one sign
        d = np.diff(np.vstack([quad, quad[0]]), axis=0)
        cross = d[:, 0] * np.roll(d[:, 1], -1) - d[:, 1] * np.roll(d[:, 0], -1)
        if not (np.all(cross > 0) or np.all(cross < 0)):
            return True, "non-convex"
        return False, "ok"

    def _projection_quality(self, calibration, frame_shape) -> tuple:
        """Project a court-region pixel grid via a candidate calibration and
        return (coverage, y_span, x_span) in court metres. The positive
        output-validation gate for locking (Fix, 2026-05-29):

          • coverage — fraction landing in the ±5 m court band; an overfit/
            singular fit that projects nothing scores ~0.
          • y_span / x_span — robust (p90−p10) spread of the projected coords.
            A DEGENERATE fit can still land "in band" while collapsing the
            court to a line (match 4's piecewise fit mapped every pixel to
            y≈23.77 → 80% coverage but y_span≈0). Requiring real 2-D spread
            catches that — a healthy fit maps the frame's vertical extent to
            most of the court length (y_span ≫ 0) and its width to the court
            width (x_span ≫ 0).

        Does NOT mutate detector state (operates on the passed calibration).
        """
        if calibration is None:
            return 0.0, 0.0, 0.0
        h, w = frame_shape[:2]
        fxs = np.linspace(0.15, 0.85, 8)
        fys = np.linspace(0.30, 0.92, 8)
        mxs, mys = [], []
        total = inb = 0
        for fy in fys:
            for fx in fxs:
                total += 1
                r = project_pixel_to_metres(w * fx, h * fy, calibration)
                if r is None:
                    continue
                mx, my = r
                if (-5.0 <= mx <= COURT_WIDTH_DOUBLES_M + 5.0 and
                        -5.0 <= my <= COURT_LENGTH_M + 5.0):
                    inb += 1
                    mxs.append(mx)
                    mys.append(my)
        cov = inb / total if total else 0.0
        if len(mxs) >= 4:
            x_span = float(np.percentile(mxs, 90) - np.percentile(mxs, 10))
            y_span = float(np.percentile(mys, 90) - np.percentile(mys, 10))
        else:
            x_span = y_span = 0.0
        return cov, y_span, x_span

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

        # Degeneracy gate (Fix B): reject Hough fabrications whose corner
        # reprojection isn't a sane in-frame convex quad. This is the source
        # of the match-4-class silent degeneracy — the Hough fallback picks
        # banner/logo edges as baselines and produces a homography that passes
        # inlier checks but collapses the court.
        degen, reason = self._homography_degenerate(homography, w, h)
        if degen:
            logger.info("_detect_hough: REJECTED degenerate homography (%s)", reason)
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

    def to_court_coords(self, pixel_x: float, pixel_y: float,
                         strict: bool = True) -> Optional[tuple]:
        """Convert pixel coordinates to real-world court coordinates (metres).

        Priority: _locked_detection (post-calibration, deterministic) →
        _last_detection (if homography valid) → _last_good_detection.

        strict=True (default) applies a ±5m sanity bounds check. Pipeline
        downstream (player scoring, bounce classification, speed) uses strict.
        Debug annotations pass strict=False so we still see the numeric court_y
        for off-court candidates, which is diagnostically useful.
        """
        if self._calibration is not None:
            result = project_pixel_to_metres(pixel_x, pixel_y, self._calibration)
            if result is None:
                return None
            mx, my = result
            if strict and not (-5.0 <= mx <= COURT_WIDTH_DOUBLES_M + 5.0 and
                               -5.0 <= my <= COURT_LENGTH_M + 5.0):
                return None
            return (float(mx), float(my))

        det = self._locked_detection
        if det is None or det.homography is None:
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

        if strict and not (-5.0 <= mx <= COURT_WIDTH_DOUBLES_M + 5.0 and
                           -5.0 <= my <= COURT_LENGTH_M + 5.0):
            return None

        return (float(mx), float(my))

    def to_pixel_coords(self, metric_x: float, metric_y: float) -> Optional[tuple]:
        """Inverse of to_court_coords — metric → raw pixel. Used for
        drawing projected court lines on debug frames. Returns None if
        calibration is not available.
        """
        if self._calibration is None:
            return None
        return project_metres_to_pixel(metric_x, metric_y, self._calibration)

    def get_court_corners_pixels(self) -> Optional[list]:
        """Return the 4 baseline corner keypoints as pixel coordinates.

        Returns [(x,y), (x,y), (x,y), (x,y)] in order:
          [0] far baseline left   (top-left in image)
          [1] far baseline right  (top-right)
          [2] near baseline left  (bottom-left)
          [3] near baseline right (bottom-right)

        When lens calibration is available, project the metric court
        corners (0,0 — 10.97,0 — 10.97,23.77 — 0,23.77) through the
        calibration. This gives a pixel polygon that matches the actual
        court lines, instead of relying on raw CNN keypoints — some of
        which were dropped during calibration refinement because they
        were collapsed / systematically wrong.

        Falls back to raw keypoint positions if no calibration exists.
        Returns None if fewer than 4 corners can be produced.
        """
        if self._calibration is not None:
            metric_corners = [
                (0.0, 0.0),                        # far-left
                (COURT_WIDTH_DOUBLES_M, 0.0),      # far-right
                (0.0, COURT_LENGTH_M),             # near-left
                (COURT_WIDTH_DOUBLES_M, COURT_LENGTH_M),  # near-right
            ]
            corners = []
            for mx, my in metric_corners:
                pt = project_metres_to_pixel(mx, my, self._calibration)
                if pt is None:
                    break
                corners.append((float(pt[0]), float(pt[1])))
            if len(corners) == 4:
                return corners

        det = self._last_detection
        if det is None or det.homography is None:
            det = self._last_good_detection
        if det is None:
            return None
        kps = det.keypoints
        corners = []
        for i in range(4):
            if kps[i][0] >= 0 and kps[i][1] >= 0:
                corners.append((float(kps[i][0]), float(kps[i][1])))
            else:
                return None
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
