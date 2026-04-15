# HANDOVER — T5 ML Pipeline (current state, Apr 15 evening)

## Read CLAUDE.md first — the T5 section at the bottom is the authoritative reference.

═══════════════════════════════════════════════════════════════════════
## WHERE WE ARE
═══════════════════════════════════════════════════════════════════════

**Pipeline runs end-to-end.** Bronze, silver, video trim, SES notification all work. Calibration → detection → bronze → silver passes complete cleanly on MATCHI indoor footage.

**Data quality is wrong.** Not broken, wrong. The homography is being fitted to a wide-angle lens that barrel-distorts the court. Every downstream metric (coords, speed, stroke class, serve gate) inherits that error.

### Latest reference run (ad763368)

| Signal | Value | Target (SportAI) | Verdict |
|---|---|---|---|
| Runtime | 55.9 min | <25 min | perf work deferred |
| Bronze ball court_x populated | 99.9% | - | works |
| Silver rows | 162 | 88 | over-detects (bounces-as-rows) |
| Serves detected | 1 | 24 | **homography + cooldown** |
| Stroke Volley | 156 | 5 | **homography** (hitters project near net line) |
| Ball speed avg | 30 km/h | 359 km/h | **homography** (compressed scale) |
| Ball court_y range | [10.69, 24.29] | should include 0-2 | **homography** (far half not projectable) |

Every "**homography**" tag is one bug. Lens distortion. Fix lands next.

═══════════════════════════════════════════════════════════════════════
## DEPLOYMENT STATE
═══════════════════════════════════════════════════════════════════════

**Batch job definition `ten-fifty5-ml-pipeline:13`** is current (auto-selected for new T5 submissions).

- Image: `sha256:2397c93b...` (rev 12 code + rev 13 retry strategy)
- Retry: 3 attempts, auto-retry only on `Host EC2*` (Spot eviction); code errors exit immediately
- Queue `ten-fifty5-ml-queue`: Spot at priority 1, on-demand CE at priority 2
- Image parity: eu-north-1 and us-east-1 ECR both hold the same digest

### Git state

- Branch: `main`
- Latest commit: `90eb92b` fix(t5): catch 'banner+logo as baselines' in geometry validator
- origin/main is in sync

### Code landing sequence (committed, deployed)

| Commit | Summary |
|---|---|
| `8a1a253` | Court calibration locks BEST detection, not most recent |
| `00658f3` | Restore projections + surface y= in debug annotations (MIN_INLIERS 8→4, `_locked_detection` priority in `to_court_coords`, strict=False debug mode) |
| `48c62c4` | Implement 3-tier player scoring per spec |
| `85268c8` | Homography geometry validation + pixel-space player gate (Fix A + Fix B) |
| `80a56d9` | Log keypoints at lock + x= in debug annotations; tighten perspective check to 0.75 |
| `684bda7` | Fail-fast if no homography passes geometry validation |
| `85e88df` | Log keypoints on calibration failure (diagnostic before abort) |
| `90eb92b` | Catch 'banner+logo as baselines' — span 25-70% of frame, far baseline not in top 8%, near baseline not in bottom 5% |

═══════════════════════════════════════════════════════════════════════
## NEXT — LENS DISTORTION FIX (APPROVED APR 15)
═══════════════════════════════════════════════════════════════════════

### Approach

**Option A (primary)**: fit radial distortion `[k1, k2]` using the 14 court keypoints as planar correspondences via `cv2.calibrateCamera` with planar-safe flag set. Precompute undistort maps, remap each frame before CNN/ball/player detection.

**Option C (fallback)**: 4-zone piecewise homography, used when Option A's RMS reprojection error > 1.5 px after calibration.

### Agent-researched implementation details

Three parallel research agents confirmed (Apr 15 evening). See `project_t5_lens_distortion_plan.md` memory for the synthesis. Key decisions:

- **Model**: Brown-Conrady (k1, k2), not fisheye. MATCHI cam is ~90° FoV, not true fisheye.
- **API**: `cv2.initCameraMatrix2D` → `cv2.calibrateCamera` (single-image planar with strict flags) → `cv2.getOptimalNewCameraMatrix(alpha=0.0)` → `cv2.initUndistortRectifyMap(type=CV_16SC2)` → `cv2.remap` per frame.
- **Flags**: `CALIB_FIX_PRINCIPAL_POINT | CALIB_FIX_ASPECT_RATIO | CALIB_ZERO_TANGENT_DIST | CALIB_FIX_K3 | CALIB_USE_INTRINSIC_GUESS`.
- **Fallback if calibrateCamera gives RMS > 1.5px**: custom `scipy.optimize.least_squares` over [k1, k2, f] minimizing reprojection error, seeded by `cv2.solvePnP`.
- **Piecewise (Option C)**: 4 quadrants split at net-y, centre-x. Per-zone keypoint subsets already available. Blend within 80px of boundary via inverse-distance weighting.
- **Expected accuracy**: Option A ~10-25cm metric error; Option C ~0.8-1.2m at edges (vs 4-5m now).

### Implementation scope

- New file: `ml_pipeline/camera_calibration.py` — fitting logic for Option A + Option C.
- Modify `ml_pipeline/court_detector.py` — call calibration at lock time; store `K`, `dist_coeffs`, `undistort_map1/2`, per-zone homographies.
- Modify `ml_pipeline/pipeline.py::_process_frame` — `cv2.remap(frame, map1, map2, ...)` before each detector.
- Modify `ml_pipeline/config.py` — RMS threshold for A→C fallback, feature flag.

### Validation gates

- Reprojection RMS < 1.5 px after Option A → accept; otherwise fall back to Option C.
- Post-calibration, projection of each court keypoint via the new pipeline should round-trip to within 2 px in pixel space.
- After deploy: eval-ball `court_y_in_range[-2..26]` must PASS (actual range should include values near 0).
- Reconcile vs SportAI 4a194ff3: serves should jump from 1 toward 24.

═══════════════════════════════════════════════════════════════════════
## KEY TASK IDs
═══════════════════════════════════════════════════════════════════════

| Purpose | Task ID |
|---|---|
| SportAI ground truth | `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` |
| Latest T5 reference (distortion-confirmed) | `ad763368-eb3d-40f0-b9fe-84e0c9755c90` |
| Latest T5 (validator + retry, spot-evicted before completion) | `447cb53c-b7dd-4cf9-ad5a-bc2c96ac5703` |

═══════════════════════════════════════════════════════════════════════
## AFTER LENS DISTORTION LANDS
═══════════════════════════════════════════════════════════════════════

Queued in priority order (from the Apr 15 audit):

1. Silver `MIN_SERVE_INTERVAL_S=8.0` cooldown — blocks 2nd serve in a point (fault-retry)
2. Silver `server_end_d` forward-fill window — add `ORDER BY ball_hit_s, id` to window frame
3. Performance instrumentation — add per-stage timings in `pipeline.py::_process_frame`
4. Performance tuning to 35-40 min (SAHI tile 640→800, overlap 15→10%, PLAYER_DETECTION_INTERVAL 5→8, HW video decode)
5. Stroke classifier training (blocked on clean dual-submit pair)
