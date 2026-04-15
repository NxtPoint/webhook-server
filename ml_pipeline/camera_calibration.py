"""
Camera calibration for tennis ML pipeline.

Corrects barrel distortion from wide-angle MATCHI club cameras.
Two modes:
  - 'radial'    (Option A): cv2.calibrateCamera with Brown-Conrady [k1, k2]
  - 'piecewise' (Option C): 4-zone homographies when radial RMS > threshold
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from ml_pipeline.config import (
    COURT_LENGTH_M,
    COURT_REFERENCE_KEYPOINTS,
    COURT_WIDTH_DOUBLES_M,
)

logger = logging.getLogger(__name__)

# Precomputed reference coordinate extents (canonical top-down pixel space)
_REF_X0 = COURT_REFERENCE_KEYPOINTS[0][0]   # 286
_REF_Y0 = COURT_REFERENCE_KEYPOINTS[0][1]   # 561
_REF_W  = COURT_REFERENCE_KEYPOINTS[1][0] - _REF_X0   # 1093
_REF_H  = COURT_REFERENCE_KEYPOINTS[2][1] - _REF_Y0   # 2374


@dataclass
class CalibrationResult:
    """One of 'radial' (Option A) or 'piecewise' (Option C) will be populated."""
    mode: str                                    # 'radial' | 'piecewise'
    rms_px: float                                # reprojection RMS at calibration time

    # Option A fields (populated when mode == 'radial')
    K: Optional[np.ndarray] = None              # (3,3) camera intrinsics
    dist: Optional[np.ndarray] = None           # (5,) Brown-Conrady [k1,k2,0,0,0]
    new_K: Optional[np.ndarray] = None          # (3,3) optimal for undistort output
    map1: Optional[np.ndarray] = None           # precomputed undistort map (CV_16SC2)
    map2: Optional[np.ndarray] = None
    homography_undistorted: Optional[np.ndarray] = None  # (3,3) pixel_undistorted -> metric
    rvec: Optional[np.ndarray] = None           # (3,1) rotation for metric→pixel projection
    tvec: Optional[np.ndarray] = None           # (3,1) translation

    # Option C fields (populated when mode == 'piecewise')
    zone_homographies: Optional[list] = None    # 4 x (3,3) in order [FL, FR, NL, NR]
    net_y_px: Optional[float] = None
    centre_x_px: Optional[float] = None


def _ref_to_world_3d(ref_idx: int) -> tuple[float, float, float]:
    rx, ry = COURT_REFERENCE_KEYPOINTS[ref_idx]
    mx = (rx - _REF_X0) / _REF_W * COURT_WIDTH_DOUBLES_M
    my = (ry - _REF_Y0) / _REF_H * COURT_LENGTH_M
    return (mx, my, 0.0)


def _build_point_lists(
    keypoint_observations: list[np.ndarray],
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Convert frame observations to calibrateCamera-compatible lists.

    Returns (object_points, image_points) where each element is one frame's
    valid (N,1,3) / (N,1,2) array.  Frames with fewer than 6 valid keypoints
    are skipped — calibrateCamera needs at least 6 for a planar solve.
    """
    object_points, image_points = [], []
    for frame_kps in keypoint_observations:
        pts_img, pts_world = [], []
        for i, (kx, ky) in enumerate(frame_kps):
            if kx >= 0:
                pts_img.append([kx, ky])
                pts_world.append(list(_ref_to_world_3d(i)))
        if len(pts_img) >= 6:
            object_points.append(
                np.array(pts_world, dtype=np.float32).reshape(-1, 1, 3)
            )
            image_points.append(
                np.array(pts_img, dtype=np.float32).reshape(-1, 1, 2)
            )
    return object_points, image_points


def _option_a(
    keypoint_observations: list[np.ndarray],
    img_shape: tuple[int, int],
    rms_threshold_px: float,
) -> Optional[CalibrationResult]:
    """Attempt Brown-Conrady radial calibration via cv2.calibrateCamera.

    Returns CalibrationResult on success (rms <= threshold), None otherwise.
    """
    object_points, image_points = _build_point_lists(keypoint_observations)
    if not object_points:
        logger.warning("Option A: no frames with >=6 keypoints; skipping")
        return None

    h, w = img_shape
    f_init = w * 1.2
    K_init = np.array(
        [[f_init, 0, w / 2], [0, f_init, h / 2], [0, 0, 1]], dtype=np.float64
    )

    flags = (
        cv2.CALIB_FIX_PRINCIPAL_POINT
        | cv2.CALIB_FIX_ASPECT_RATIO
        | cv2.CALIB_ZERO_TANGENT_DIST
        | cv2.CALIB_FIX_K3
        | cv2.CALIB_USE_INTRINSIC_GUESS
    )

    try:
        rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
            object_points,
            image_points,
            (w, h),
            cameraMatrix=K_init.copy(),
            distCoeffs=np.zeros(5),
            flags=flags,
        )
    except cv2.error as exc:
        logger.warning("Option A: cv2.calibrateCamera failed: %s", exc)
        return None

    logger.info("Option A RMS: %.4f px (threshold %.2f)", rms, rms_threshold_px)

    if rms > rms_threshold_px:
        logger.info("Option A RMS exceeds threshold; will fall back to Option C")
        return None

    new_K, _ = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), alpha=0.0)
    map1, map2 = cv2.initUndistortRectifyMap(
        K, dist, None, new_K, (w, h), cv2.CV_16SC2
    )

    # Homography from undistorted pixels → metric court coordinates.
    # Use the frame that contributed the most keypoints so the homography
    # is as well-conditioned as possible.
    best_idx = int(np.argmax([len(p) for p in image_points]))
    img_pts = image_points[best_idx].reshape(-1, 1, 2)
    world_pts_2d = object_points[best_idx].reshape(-1, 3)[:, :2]

    img_pts_undist = cv2.undistortPoints(img_pts, K, dist, P=new_K).reshape(-1, 2)
    H_undist, _ = cv2.findHomography(img_pts_undist, world_pts_2d, cv2.RANSAC, 3.0)
    if H_undist is None:
        logger.warning("Option A: findHomography returned None after undistortion")
        return None

    # rvec/tvec for the best frame so project_metres_to_pixel can invert.
    obj_pts_best = object_points[best_idx].reshape(-1, 1, 3)
    img_pts_best = image_points[best_idx].reshape(-1, 1, 2)
    ok, rvec, tvec = cv2.solvePnP(obj_pts_best, img_pts_best, K, dist)
    if not ok:
        logger.warning("Option A: solvePnP failed for inverse projection; skipping")
        return None

    return CalibrationResult(
        mode="radial",
        rms_px=float(rms),
        K=K,
        dist=dist,
        new_K=new_K,
        map1=map1,
        map2=map2,
        homography_undistorted=H_undist,
        rvec=rvec,
        tvec=tvec,
    )


def _best_frame_kps(keypoint_observations: list[np.ndarray]) -> np.ndarray:
    """Return the single observation array with the most valid keypoints."""
    best = max(keypoint_observations, key=lambda kps: np.sum(kps[:, 0] >= 0))
    return best


def _compute_zone_split(kps: np.ndarray) -> tuple[float, float]:
    """Derive net_y_px and centre_x_px from detected keypoints.

    net_y_px is the midpoint between the two service lines.
    centre_x_px is the horizontal centre derived from centre-service or baseline pts.
    """
    service_top_ys = [kps[i, 1] for i in (8, 9, 12) if kps[i, 0] >= 0]
    service_bot_ys = [kps[i, 1] for i in (10, 11, 13) if kps[i, 0] >= 0]
    net_y_px = float(
        (np.mean(service_top_ys) + np.mean(service_bot_ys)) / 2
    )

    centre_xs = [kps[i, 0] for i in (12, 13) if kps[i, 0] >= 0]
    if centre_xs:
        centre_x_px = float(np.mean(centre_xs))
    else:
        bl_xs = [kps[i, 0] for i in (0, 1, 2, 3) if kps[i, 0] >= 0]
        centre_x_px = float(np.mean(bl_xs))

    return net_y_px, centre_x_px


# Zone indices: 0=FL (far-left), 1=FR (far-right), 2=NL (near-left), 3=NR (near-right)
# "far" = y < net_y_px,  "left" = x < centre_x_px
_ZONE_FL, _ZONE_FR, _ZONE_NL, _ZONE_NR = 0, 1, 2, 3


def _assign_zone(x: float, y: float, net_y_px: float, centre_x_px: float) -> int:
    far = y < net_y_px
    left = x < centre_x_px
    if far and left:
        return _ZONE_FL
    if far and not left:
        return _ZONE_FR
    if not far and left:
        return _ZONE_NL
    return _ZONE_NR


# Which keypoint indices are "shared" boundary points contributed to every zone
# (centre line + net-adjacent service-line intersections).  Adding them helps
# when a corner zone has only 3 exclusive points.
_SHARED_KP_INDICES = frozenset({8, 9, 10, 11, 12, 13})

# Exclusive membership: which zone each keypoint primarily belongs to.
# Baseline corners own their quadrant; inner-line corners live on the boundary
# and are treated as shared above.
_KP_PRIMARY_ZONE = {
    0: _ZONE_FL, 1: _ZONE_FR,   # far baseline L, R
    2: _ZONE_NL, 3: _ZONE_NR,   # near baseline L, R
    4: _ZONE_FL, 5: _ZONE_NL,   # left singles line top, bot
    6: _ZONE_FR, 7: _ZONE_NR,   # right singles line top, bot
    # 8-13 are shared
}


def _fit_zone_homography(
    img_pts: np.ndarray, world_pts: np.ndarray
) -> Optional[np.ndarray]:
    """Fit H (image → metric) for one zone.

    Uses LMEDS for small sets (4-5 pts) where RANSAC has too few inliers to
    be reliable; RANSAC for larger sets.
    """
    n = len(img_pts)
    if n < 4:
        return None
    method = cv2.LMEDS if n <= 5 else cv2.RANSAC
    H, _ = cv2.findHomography(img_pts, world_pts, method, 3.0)
    return H


def _reprojection_rms(H: np.ndarray, img_pts: np.ndarray, world_pts: np.ndarray) -> float:
    """Compute reprojection RMS (pixels) for a homography img → metric.

    We back-project by inverting H so we can compare in pixel space.
    """
    H_inv = np.linalg.inv(H)
    n = len(img_pts)
    errors = []
    for (ix, iy), (wx, wy) in zip(img_pts, world_pts):
        # project metric → pixel
        v = H_inv @ np.array([wx, wy, 1.0])
        px_rep, py_rep = v[0] / v[2], v[1] / v[2]
        errors.append((px_rep - ix) ** 2 + (py_rep - iy) ** 2)
    return float(np.sqrt(np.mean(errors))) if errors else 0.0


def _option_c(
    keypoint_observations: list[np.ndarray],
) -> Optional[CalibrationResult]:
    """4-zone piecewise homography calibration."""
    kps = _best_frame_kps(keypoint_observations)

    try:
        net_y_px, centre_x_px = _compute_zone_split(kps)
    except (ValueError, ZeroDivisionError) as exc:
        logger.warning("Option C: could not compute zone split: %s", exc)
        return None

    # Gather per-zone keypoint sets (exclusive + shared)
    zone_img: list[list] = [[], [], [], []]
    zone_world: list[list] = [[], [], [], []]
    shared_img: list = []
    shared_world: list = []

    for i, (kx, ky) in enumerate(kps):
        if kx < 0:
            continue
        wx, wy, _ = _ref_to_world_3d(i)
        if i in _SHARED_KP_INDICES:
            shared_img.append([kx, ky])
            shared_world.append([wx, wy])
        else:
            z = _KP_PRIMARY_ZONE[i]
            zone_img[z].append([kx, ky])
            zone_world[z].append([wx, wy])

    # Augment each zone with shared points
    for z in range(4):
        zone_img[z].extend(shared_img)
        zone_world[z].extend(shared_world)

    zone_homographies: list[Optional[np.ndarray]] = []
    zone_rms_list: list[float] = []
    fallback_H: Optional[np.ndarray] = None  # single global H, used when a zone is under-determined

    for z in range(4):
        ipts = np.array(zone_img[z], dtype=np.float32)
        wpts = np.array(zone_world[z], dtype=np.float32)
        H = _fit_zone_homography(ipts, wpts)
        if H is None:
            logger.info(
                "Option C zone %d: only %d points, using fallback global H", z, len(ipts)
            )
            # Will be patched with fallback below
            zone_homographies.append(None)
            zone_rms_list.append(0.0)
        else:
            zone_homographies.append(H)
            zone_rms_list.append(_reprojection_rms(H, ipts, wpts))

    # Build global fallback H from all valid keypoints
    all_img, all_world = [], []
    for i, (kx, ky) in enumerate(kps):
        if kx >= 0:
            wx, wy, _ = _ref_to_world_3d(i)
            all_img.append([kx, ky])
            all_world.append([wx, wy])

    if len(all_img) >= 4:
        fallback_H, _ = cv2.findHomography(
            np.array(all_img, dtype=np.float32),
            np.array(all_world, dtype=np.float32),
            cv2.RANSAC,
            3.0,
        )

    for z in range(4):
        if zone_homographies[z] is None:
            zone_homographies[z] = fallback_H

    overall_rms = float(max(zone_rms_list)) if zone_rms_list else 0.0
    logger.info("Option C: zone RMS values %s, overall=%.4f", zone_rms_list, overall_rms)

    return CalibrationResult(
        mode="piecewise",
        rms_px=overall_rms,
        zone_homographies=zone_homographies,
        net_y_px=net_y_px,
        centre_x_px=centre_x_px,
    )


def fit_calibration(
    keypoint_observations: list[np.ndarray],
    img_shape: tuple[int, int],  # (h, w)
    rms_threshold_px: float = 1.5,
) -> Optional[CalibrationResult]:
    """Top-level entry point.

    Tries Option A (radial distortion) first. Falls back to Option C
    (piecewise) if A's RMS exceeds rms_threshold_px or A fails to converge.

    Args:
        keypoint_observations: list of (14, 2) float arrays. Each row is a
            keypoint pixel position or (-1, -1) if missing. Typically accumulated
            across 5-10 calibration frames. At least one observation required.
        img_shape: (h, w) of the input frames.
        rms_threshold_px: A-to-C fallback trigger.

    Returns:
        CalibrationResult or None if both options fail.
    """
    result_a = _option_a(keypoint_observations, img_shape, rms_threshold_px)
    if result_a is not None:
        logger.info("Calibration: using Option A (radial), RMS=%.4f", result_a.rms_px)
        return result_a

    logger.info("Calibration: falling back to Option C (piecewise)")
    result_c = _option_c(keypoint_observations)
    if result_c is not None:
        logger.info("Calibration: using Option C (piecewise), RMS=%.4f", result_c.rms_px)
    else:
        logger.warning("Calibration: both options failed; returning None")
    return result_c


def _apply_homography(H: np.ndarray, px: float, py: float) -> Optional[tuple[float, float]]:
    v = H @ np.array([px, py, 1.0], dtype=np.float64)
    if abs(v[2]) < 1e-9:
        return None
    return float(v[0] / v[2]), float(v[1] / v[2])


def project_pixel_to_metres(
    px: float,
    py: float,
    calib: CalibrationResult,
    blend_px: float = 80.0,
) -> Optional[tuple[float, float]]:
    """Project a pixel coordinate to court-metre space.

    Contract: input pixel is in RAW (distorted, as-captured) space. Radial
    mode undistorts the point via cv2.undistortPoints then applies the
    homography fit on undistorted keypoints. Piecewise mode applies the
    per-zone homography directly to the raw pixel. Downstream code and
    detectors should pass raw-frame pixel coordinates; the pipeline does
    NOT remap frames so the detectors' outputs stay in raw space.

    Returns (mx, my) or None if projection fails.
    """
    if calib.mode == "radial":
        pt = np.array([[[px, py]]], dtype=np.float32)
        undist = cv2.undistortPoints(pt, calib.K, calib.dist, P=calib.new_K)
        ux, uy = float(undist[0, 0, 0]), float(undist[0, 0, 1])
        return _apply_homography(calib.homography_undistorted, ux, uy)

    # Piecewise mode
    net_y = calib.net_y_px
    cx = calib.centre_x_px
    primary_zone = _assign_zone(px, py, net_y, cx)

    def _proj(z: int) -> Optional[tuple[float, float]]:
        H = calib.zone_homographies[z]
        if H is None:
            return None
        return _apply_homography(H, px, py)

    # Determine closeness to each boundary axis
    dx = abs(px - cx)
    dy = abs(py - net_y)
    near_x_boundary = dx < blend_px
    near_y_boundary = dy < blend_px

    if not near_x_boundary and not near_y_boundary:
        return _proj(primary_zone)

    # Collect adjacent zones that share a crossed boundary
    adjacent_zones: list[int] = []
    if near_x_boundary:
        # Neighbour across the vertical centre line (same far/near half)
        far = py < net_y
        if px < cx:
            adjacent_zones.append(_ZONE_FR if far else _ZONE_NR)
        else:
            adjacent_zones.append(_ZONE_FL if far else _ZONE_NL)
    if near_y_boundary:
        # Neighbour across the net line (same left/right half)
        left = px < cx
        if py < net_y:
            adjacent_zones.append(_ZONE_NL if left else _ZONE_NR)
        else:
            adjacent_zones.append(_ZONE_FL if left else _ZONE_FR)

    zones_to_blend = [primary_zone] + adjacent_zones
    results: list[tuple[float, tuple[float, float]]] = []

    for z in zones_to_blend:
        proj = _proj(z)
        if proj is None:
            continue
        # Inverse distance to the zone's boundary
        if z == primary_zone:
            # Primary: distance is how far inside the safe region we are
            dist = min(dx if near_x_boundary else float("inf"),
                       dy if near_y_boundary else float("inf"))
            # Farther from boundary → higher weight
            w = max(blend_px - dist, 1e-6)
        else:
            # Adjacent: weight proportional to closeness to their boundary
            dist = dx if near_x_boundary and z in adjacent_zones[:1 if near_x_boundary else 0] else dy
            w = max(blend_px - dist, 1e-6) if dist < blend_px else 1e-6
        results.append((w, proj))

    if not results:
        return None

    total_w = sum(w for w, _ in results)
    mx = sum(w * m[0] for w, m in results) / total_w
    my = sum(w * m[1] for w, m in results) / total_w
    return float(mx), float(my)


def project_metres_to_pixel(
    mx: float, my: float,
    calib: CalibrationResult,
) -> Optional[tuple[float, float]]:
    """Inverse of project_pixel_to_metres — metric court coord → raw pixel.

    Used for debug overlays (draw the real court lines back onto the image).
    Radial mode uses cv2.projectPoints with the stored rvec/tvec. Piecewise
    mode inverts the zone homography nearest the given metric position.
    """
    if calib.mode == "radial":
        if calib.rvec is None or calib.tvec is None:
            return None
        world_pt = np.array([[[mx, my, 0.0]]], dtype=np.float64)
        pixel, _ = cv2.projectPoints(world_pt, calib.rvec, calib.tvec, calib.K, calib.dist)
        return float(pixel[0, 0, 0]), float(pixel[0, 0, 1])

    # Piecewise: pick the zone by metric position, invert its homography.
    net_metric = COURT_LENGTH_M / 2
    centre_metric = COURT_WIDTH_DOUBLES_M / 2
    far = my < net_metric
    left = mx < centre_metric
    zone = (0 if far else 2) + (0 if left else 1)
    H = calib.zone_homographies[zone]
    if H is None:
        return None
    try:
        H_inv = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return None
    v = H_inv @ np.array([mx, my, 1.0], dtype=np.float64)
    if abs(v[2]) < 1e-9:
        return None
    return float(v[0] / v[2]), float(v[1] / v[2])


def evaluate_calibration(
    calib: CalibrationResult,
    keypoint_observations: list[np.ndarray],
) -> np.ndarray:
    """Project each detected keypoint through the calibration and compare to
    its expected metric position. Returns a (14,) array of errors in metres;
    NaN where the keypoint was not detected in the best-frame observation.
    """
    best = _best_frame_kps(keypoint_observations)
    errors = np.full(14, np.nan, dtype=np.float64)
    for i in range(14):
        if best[i, 0] < 0:
            continue
        projected = project_pixel_to_metres(
            float(best[i, 0]), float(best[i, 1]), calib,
        )
        if projected is None:
            continue
        ex, ey, _ = _ref_to_world_3d(i)
        errors[i] = float(np.hypot(projected[0] - ex, projected[1] - ey))
    return errors


def undistort_frame(frame: np.ndarray, calib: Optional[CalibrationResult]) -> np.ndarray:
    """For Option A, remap a frame through precomputed undistort maps.
    For Option C, return frame unchanged. No-op if calib is None.
    """
    if calib is None or calib.mode != "radial":
        return frame
    return cv2.remap(frame, calib.map1, calib.map2, cv2.INTER_LINEAR)


# ---------------------------------------------------------------------------
# Self-check tests — run via:  python -m ml_pipeline.camera_calibration
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    def _make_observations_pinhole(
        K: np.ndarray,
        dist: np.ndarray,
        img_shape: tuple[int, int],
        n_frames: int = 6,
    ) -> list[np.ndarray]:
        """Project world keypoints through K+dist to create synthetic observations."""
        h, w = img_shape
        rvec = np.zeros(3, dtype=np.float64)
        # Translate world centre to roughly the image centre
        world_pts = np.array(
            [_ref_to_world_3d(i) for i in range(14)], dtype=np.float64
        )
        cx_world = np.mean(world_pts[:, 0])
        cy_world = np.mean(world_pts[:, 1])
        tvec = np.array(
            [-(cx_world - COURT_WIDTH_DOUBLES_M / 2),
             -(cy_world - COURT_LENGTH_M / 2),
             max(w, h) / K[0, 0]],
            dtype=np.float64,
        )
        projected, _ = cv2.projectPoints(
            world_pts.reshape(-1, 1, 3), rvec, tvec, K, dist
        )
        pts = projected.reshape(-1, 2)

        # Jitter slightly across frames to give calibrateCamera enough variation
        observations = []
        for f in range(n_frames):
            noise = np.random.default_rng(f).normal(0, 0.3, pts.shape)
            obs = (pts + noise).astype(np.float32)
            observations.append(obs)
        return observations

    print("Test 1: synthetic perfect-pinhole (no distortion) …", end=" ")
    h, w = 1080, 1920
    K_true = np.array(
        [[w * 1.2, 0, w / 2], [0, w * 1.2, h / 2], [0, 0, 1]], dtype=np.float64
    )
    dist_zero = np.zeros(5)
    obs_pinhole = _make_observations_pinhole(K_true, dist_zero, (h, w))
    result = fit_calibration(obs_pinhole, (h, w), rms_threshold_px=1.5)
    assert result is not None, "Expected non-None result"
    assert result.mode == "radial", f"Expected 'radial', got '{result.mode}'"
    assert result.rms_px < 1.5, f"RMS too high: {result.rms_px:.4f}"
    k1_recovered = float(result.dist.flat[0])
    assert abs(k1_recovered) < 0.05, f"k1 should be ~0, got {k1_recovered:.4f}"
    print(f"OK  (mode={result.mode}, RMS={result.rms_px:.4f}, k1={k1_recovered:.4f})")

    print("Test 2: synthetic barrel-distorted (k1=-0.2) …", end=" ")
    dist_barrel = np.array([-0.2, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
    obs_barrel = _make_observations_pinhole(K_true, dist_barrel, (h, w))
    result_b = fit_calibration(obs_barrel, (h, w), rms_threshold_px=2.0)
    assert result_b is not None, "Expected non-None result for barrel"
    if result_b.mode == "radial":
        k1_est = float(result_b.dist.flat[0])
        rel_err = abs(k1_est - (-0.2)) / 0.2
        assert rel_err < 0.05, f"k1 not within 5% of -0.2: got {k1_est:.4f} (rel_err={rel_err:.3f})"
        print(f"OK  (mode={result_b.mode}, RMS={result_b.rms_px:.4f}, k1={k1_est:.4f}, rel_err={rel_err:.3%})")
    else:
        # Fell back to piecewise — acceptable if radial RMS exceeded threshold
        print(f"OK  (fell back to piecewise, RMS={result_b.rms_px:.4f})")

    print("Test 3: degenerate — only 2 keypoints …", end=" ")
    kps_sparse = np.full((14, 2), -1.0, dtype=np.float32)
    kps_sparse[0] = [500.0, 300.0]
    kps_sparse[1] = [1400.0, 300.0]
    result_d = fit_calibration([kps_sparse], (h, w))
    assert result_d is None, f"Expected None for degenerate input, got {result_d}"
    print("OK  (returned None gracefully)")

    print("\nAll tests passed.")
    sys.exit(0)
