"""
Fix E — camera-agnostic lens-distortion estimation (2026-05-28).

Estimates and corrects barrel/fisheye distortion from a single video with NO
checkerboard: the tennis court's many long, mutually-parallel, known-straight
white lines ARE the calibration target ("plumb-line" / straightness methods).

Two estimators + auto model selection (per the research synthesis in
docs/_investigation/court_calibration_silent_degeneracy.md §Architectural
proposal, Agent 2):

  • LINE  — one-parameter division model (Fitzgibbon): r_u = r_d / (1+λ·r_d²).
            Minimise the straightness residual of detected court lines over
            (centre, λ), then convert λ → OpenCV Brown-Conrady (k1, k2).
            Works even when keypoint coverage is sparse (uses whole lines).
  • FISHEYE — cv2.fisheye.calibrate (Kannala-Brandt) from court keypoints,
            for strong wide-angle/GoPro lenses the k1,k2 polynomial can't fit.
  • AUTO  — fit both, pick the one with the lower residual line curvature.

Correction is applied at the COORDINATE-TRANSFORM layer (cv2.undistortPoints
on court keypoints for the homography fit, and on individual detections
downstream) — never a full-frame remap. So it slots in front of the existing
homography/radial fit with negligible per-frame cost and no detector retraining.

⚠️ STATUS: built 2026-05-28, DORMANT by default. Gated behind the env var
T5_CALIB_LENS_MODE (off|line|fisheye|auto; default 'off'). The radial path in
camera_calibration.py already handles the wide MATCHi/club cameras we see in
production; this module future-proofs for the phone-ultrawide / GoPro-fisheye
cameras coming as users onboard. The FISHEYE path is UNVALIDATED end-to-end
until a Class-C/D fixture exists (see court_calibration_camera_taxonomy.md).
Do NOT enable in production without validating on a real wide/fisheye clip.

INTEGRATION (deploy-later — do this WITH a Class-C/D fixture, not before):
  1. Acquire a phone-ultrawide + a GoPro tennis clip → add as bench_lens
     fixtures; tune `band`/bounds and confirm the recovered k1 matches the
     known lens (this is the missing validation — on near-rectilinear footage
     the residual signal is too weak to verify accuracy; `bench_lens` only
     proves the estimator is well-behaved / non-divergent today).
  2. In court_detector: when lens_mode()!='off', buffer ~12 calibration
     frames; at lock call estimate_lens_distortion(frames, observations,
     shape) in a try/except (must never break the lock) → store self._lens.
  3. Apply consistently at the transform layer (this is the load-bearing
     part — get all directions consistent or coordinates will be wrong):
       • undistort the calibration keypoint OBSERVATIONS via undistort_points()
         BEFORE fit_calibration (so the homography/radial fit is in
         undistorted space);
       • undistort the query pixel at the top of to_court_coords();
       • the ROI extractors project metric→pixel (project_metres_to_pixel) to
         find scan regions in the DISTORTED frame — those results must be
         RE-distorted (inverse of undistort_points) or the ROI lands wrong.
     All four gated on `self._lens is not None` (None when the env flag is
     off) so the default path is byte-identical to today.
  4. bench + bench_calib must stay green with the flag OFF; then validate the
     fisheye fixture with the flag ON before the BATCH-SIDE rebuild.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def lens_mode() -> str:
    """Env-driven mode: 'off' (default) | 'line' | 'fisheye' | 'auto'."""
    return os.environ.get("T5_CALIB_LENS_MODE", "off").strip().lower()


@dataclass
class LensDistortion:
    model: str                       # 'brown_conrady' | 'fisheye'
    K: np.ndarray                    # (3,3) intrinsics
    dist: np.ndarray                 # Brown-Conrady (5,) or fisheye (4,1)
    residual_px: float               # worst-line straightness RMS after undistort (lower = better)
    baseline_px: float               # worst-line residual BEFORE undistort (for "did it help")
    center: Optional[tuple] = None   # distortion centre (line model)

    @property
    def improved(self) -> bool:
        return self.residual_px < self.baseline_px


# ──────────────────────────────────────────────────────────────────────────
# Line extraction — the court lines are our straightness target
# ──────────────────────────────────────────────────────────────────────────

def extract_court_line_points(frame: np.ndarray, min_len: int = 120,
                              band: float = 8.0, nbins: int = 24) -> list[np.ndarray]:
    """Detect court lines → list of (N,2) CENTERLINE point arrays per line.

    Critically, these are real Canny edge pixels (which BOW under barrel
    distortion), NOT points re-sampled along a straight Hough segment (which
    would be collinear by construction and blind to distortion). For each
    Hough segment we collect edgels within ±`band` px of the line, bin them
    along the segment, and take the mean perpendicular offset per bin — a
    thickness-robust centerline that curves exactly as the lens distorts it.
    Mirrors court_detector._detect_hough's white-mask so it keys off the same
    court-line signal.
    """
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    white = cv2.inRange(hsv, (0, 0, 180), (180, 50, 255))
    _, bright = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
    mask = cv2.bitwise_or(white, bright)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (5, 2)))
    edges = cv2.Canny(mask, 50, 150, apertureSize=3)
    ys, xs = np.nonzero(edges)
    if len(xs) < 50:
        return []
    edgel = np.stack([xs, ys], axis=1).astype(np.float64)  # (M,2)

    segs = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                           minLineLength=min_len, maxLineGap=10)
    if segs is None:
        return []
    lines = []
    for s in segs:
        x1, y1, x2, y2 = [float(v) for v in s[0]]
        p1 = np.array([x1, y1])
        d = np.array([x2 - x1, y2 - y1])
        L = float(np.hypot(*d))
        if L < min_len:
            continue
        dn = d / L
        nrm = np.array([-dn[1], dn[0]])
        rel = edgel - p1
        t = rel @ dn
        perp = rel @ nrm
        sel = (t >= 0) & (t <= L) & (np.abs(perp) <= band)
        if int(sel.sum()) < 20:
            continue
        tt, pp = t[sel], perp[sel]
        bins = np.clip((tt / L * nbins).astype(int), 0, nbins - 1)
        pts = []
        for b in range(nbins):
            m = bins == b
            if int(m.sum()) >= 2:
                pts.append(p1 + tt[m].mean() * dn + pp[m].mean() * nrm)
        if len(pts) >= 8:
            lines.append(np.array(pts, dtype=np.float64))
    return lines


# ──────────────────────────────────────────────────────────────────────────
# One-parameter division model (Fitzgibbon)
# ──────────────────────────────────────────────────────────────────────────

def _undistort_div(pts: np.ndarray, c: np.ndarray, lam: float) -> np.ndarray:
    d = pts - c
    r2 = (d ** 2).sum(1, keepdims=True)
    return c + d / (1.0 + lam * r2)


def _straightness_residual(lines: list[np.ndarray], c: np.ndarray, lam: float) -> float:
    """Sum of squared perpendicular deviations of each (undistorted) line from
    its own best-fit straight line (PCA minor axis)."""
    res = 0.0
    for pts in lines:
        u = _undistort_div(pts, c, lam)
        m = u.mean(0)
        uc = u - m
        # minor axis via SVD
        _, _, V = np.linalg.svd(uc, full_matrices=False)
        n = V[-1]
        res += float(((uc @ n) ** 2).sum())
    return res


# Physical bounds so the optimiser can't "straighten" lines by collapsing all
# points to a singularity (a huge |λ| drives the corner-undistort factor to 0
# → fake-zero residual, the k1≈1e20 failure mode). Constrain the radial
# undistort scale at the image corner to a real-lens range.
_CORNER_FACTOR_LO = 0.55   # max barrel expansion at corner (λ < 0)
_CORNER_FACTOR_HI = 1.8    # max pincushion compression at corner (λ > 0)
_MAX_ABS_K1 = 0.8          # sane Brown-Conrady k1 ceiling for consumer lenses


def fit_division_model(lines: list[np.ndarray], img_wh: tuple[int, int]):
    """Fit (centre, λ) minimising total line-straightness residual, with a
    physical bound on λ so the fit can't collapse to a singularity.

    Returns (center (2,), lam_pixel_normalised, residual) or None.
    λ is normalised by the image radius² so the optimiser scale is sane.
    """
    if len(lines) < 4:
        return None
    from scipy.optimize import minimize
    w, h = img_wh
    R2 = float(w * w + h * h)
    corner_r2 = R2 / 4.0            # corner radius² from image centre
    c0 = np.array([w / 2.0, h / 2.0], dtype=np.float64)

    def obj(x):
        cx, cy, lam_raw = x
        # keep the centre near the image centre (±25%)
        if abs(cx - c0[0]) > 0.25 * w or abs(cy - c0[1]) > 0.25 * h:
            return 1e18
        lam = lam_raw / R2
        factor = 1.0 / (1.0 + lam * corner_r2)   # undistort scale at corner
        if not (_CORNER_FACTOR_LO <= factor <= _CORNER_FACTOR_HI):
            return 1e18
        return _straightness_residual(lines, np.array([cx, cy]), lam)

    r = minimize(obj, [c0[0], c0[1], 0.0], method="Nelder-Mead",
                 options={"xatol": 1e-2, "fatol": 1e-3, "maxiter": 4000})
    if not np.isfinite(r.fun) or r.fun >= 1e17:
        return None
    cx, cy, lam_raw = r.x
    return np.array([cx, cy]), lam_raw / R2, float(r.fun)


def division_to_brown_conrady(K: np.ndarray, lam: float, img_wh: tuple[int, int],
                              n: int = 4000) -> np.ndarray:
    """Convert a division-model λ to an OpenCV Brown-Conrady (k1,k2) vector by
    sampling radii and least-squares fitting the radial polynomial."""
    rng = np.random.default_rng(0)
    w, h = img_wh
    cx, cy = K[0, 2], K[1, 2]
    fx, fy = K[0, 0], K[1, 1]
    px = rng.random((n, 2)) * np.array([w, h])
    d = px - np.array([cx, cy])
    rd2 = (d ** 2).sum(1)
    xu = d / (1.0 + lam * rd2[:, None])          # undistorted (pixel offset)
    xn = xu / np.array([fx, fy])                 # normalised undistorted
    dn = d / np.array([fx, fy])                  # normalised distorted
    ru2 = (xn ** 2).sum(1)
    factor = np.sqrt((dn ** 2).sum(1) / np.clip((xn ** 2).sum(1), 1e-12, None))
    A = np.stack([ru2, ru2 ** 2], 1)
    k1, k2 = np.linalg.lstsq(A, factor - 1.0, rcond=None)[0]
    return np.array([k1, k2, 0.0, 0.0, 0.0], dtype=np.float64)


def residual_straightness(lines: list[np.ndarray], K: np.ndarray, dist: np.ndarray,
                          fisheye: bool = False) -> float:
    """Worst-line RMS perpendicular deviation (px) after OpenCV undistort."""
    worst = 0.0
    for pts in lines:
        src = pts.reshape(-1, 1, 2).astype(np.float32)
        if fisheye:
            u = cv2.fisheye.undistortPoints(src, K, dist, P=K).reshape(-1, 2)
        else:
            u = cv2.undistortPoints(src, K, dist, P=K).reshape(-1, 2)
        m = u.mean(0)
        uc = u - m
        _, _, V = np.linalg.svd(uc, full_matrices=False)
        rms = float(np.sqrt(((uc @ V[-1]) ** 2).mean()))
        worst = max(worst, rms)
    return worst


def _default_K(img_wh: tuple[int, int]) -> np.ndarray:
    w, h = img_wh
    f = w * 1.2  # standard-lens focal prior (same as camera_calibration._option_a)
    return np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1.0]], dtype=np.float64)


# ──────────────────────────────────────────────────────────────────────────
# Fisheye path (Kannala-Brandt) — needs court-keypoint correspondences
# ──────────────────────────────────────────────────────────────────────────

def estimate_fisheye(keypoint_observations: list[np.ndarray],
                     img_shape: tuple[int, int]) -> Optional[tuple]:
    """cv2.fisheye.calibrate from court-keypoint observations.

    Returns (K, D(4,1)) or None. objectpoints come from the known court
    template (Z=0 plane) via camera_calibration._ref_to_world_3d.
    """
    from ml_pipeline.camera_calibration import _ref_to_world_3d
    h, w = img_shape
    objp, imgp = [], []
    for kps in keypoint_observations:
        o, ip = [], []
        for i, (kx, ky) in enumerate(kps):
            if kx >= 0:
                o.append(_ref_to_world_3d(i))
                ip.append([kx, ky])
        if len(o) >= 6:
            objp.append(np.array(o, dtype=np.float64).reshape(-1, 1, 3))
            imgp.append(np.array(ip, dtype=np.float64).reshape(-1, 1, 2))
    if len(objp) < 3:
        return None
    K = np.eye(3)
    D = np.zeros((4, 1))
    flags = (cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC | cv2.fisheye.CALIB_FIX_SKEW)
    try:
        rms, K, D, _, _ = cv2.fisheye.calibrate(
            objp, imgp, (w, h), K, D, flags=flags,
            criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-6))
    except cv2.error as e:
        logger.info("estimate_fisheye: cv2.fisheye.calibrate failed: %s", e)
        return None
    return K, D


# ──────────────────────────────────────────────────────────────────────────
# Top-level estimator
# ──────────────────────────────────────────────────────────────────────────

def estimate_lens_distortion(frames: list[np.ndarray],
                             keypoint_observations: list[np.ndarray],
                             img_shape: tuple[int, int],
                             mode: Optional[str] = None) -> Optional[LensDistortion]:
    """Estimate lens distortion. `mode` defaults to the env var.

    Returns the best LensDistortion (lowest residual straightness) or None.
    Pure estimation — does not mutate any pipeline state.
    """
    mode = (mode or lens_mode())
    if mode == "off":
        return None
    h, w = img_shape
    img_wh = (w, h)

    # Aggregate court-line points across the supplied frames.
    lines: list[np.ndarray] = []
    for fr in frames:
        lines.extend(extract_court_line_points(fr))
    baseline = residual_straightness(lines, _default_K(img_wh),
                                     np.zeros(5)) if lines else float("inf")

    candidates: list[LensDistortion] = []

    if mode in ("line", "auto") and len(lines) >= 4:
        fit = fit_division_model(lines, img_wh)
        if fit is not None:
            center, lam, _ = fit
            K = _default_K(img_wh)
            K[0, 2], K[1, 2] = center
            dist = division_to_brown_conrady(K, lam, img_wh)
            if abs(float(dist[0])) > _MAX_ABS_K1 or not np.isfinite(dist).all():
                logger.info("estimate_lens_distortion: line fit rejected — "
                            "non-physical k1=%.3g", float(dist[0]))
            else:
                res = residual_straightness(lines, K, dist)
                candidates.append(LensDistortion("brown_conrady", K, dist, res,
                                                 baseline, tuple(center)))

    if mode in ("fisheye", "auto"):
        fe = estimate_fisheye(keypoint_observations, img_shape)
        if fe is not None and lines:
            K, D = fe
            res = residual_straightness(lines, K, D, fisheye=True)
            candidates.append(LensDistortion("fisheye", K, D, res, baseline))

    if not candidates:
        logger.info("estimate_lens_distortion: no candidate (mode=%s, lines=%d)",
                    mode, len(lines))
        return None
    candidates.sort(key=lambda c: c.residual_px)
    best = candidates[0]
    logger.info(
        "estimate_lens_distortion: mode=%s model=%s residual=%.3fpx baseline=%.3fpx improved=%s",
        mode, best.model, best.residual_px, best.baseline_px, best.improved)
    return best


def undistort_points(pts: np.ndarray, lens: LensDistortion) -> np.ndarray:
    """Undistort image points (N,2) → straightened image points (N,2). Used at
    the transform layer before the homography (court keypoints + detections)."""
    src = np.asarray(pts, dtype=np.float32).reshape(-1, 1, 2)
    if lens.model == "fisheye":
        out = cv2.fisheye.undistortPoints(src, lens.K, lens.dist, P=lens.K)
    else:
        out = cv2.undistortPoints(src, lens.K, lens.dist, P=lens.K)
    return out.reshape(-1, 2)
