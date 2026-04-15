# HANDOVER — T5 ML Pipeline (end of Apr 15, evening)

## Read CLAUDE.md first — the T5 section is the authoritative reference.

═══════════════════════════════════════════════════════════════════════
## TL;DR — where we are
═══════════════════════════════════════════════════════════════════════

**Court calibration SOLVED.** Radial lens correction locks at RMS 6.26 px on MATCHI wide-angle footage.
**Player detection SOLVED.** Near + far both tracked on ~95%+ of frames. Spectators/linespeople rejected.
**First clean silver run**: task `90ad59a8-8853-4014-9fd8-c32af7c4a2e9` — serves jumped 1 → 21, volley over-classification 156 → 2, far half of court now projectable.

Remaining silver-layer bugs are **all surfaced and diagnosed** (they were always there, just hidden under the lens distortion). Ready to iterate on serve logic + ball speed + backhand next session.

═══════════════════════════════════════════════════════════════════════
## DEPLOYMENT STATE
═══════════════════════════════════════════════════════════════════════

| Region | Job definition | Image digest |
|---|---|---|
| eu-north-1 | **revision 24** | `sha256:9107d338e7e05e60ef6a6c32d6220600e023cbc667e34706903109e30815aee6` |
| us-east-1 | **revision 13** | same digest |

Retry strategy: 3 attempts, auto-retry on `Host EC2*` (Spot interruption) only.

**Compute reality**: account has 0 on-demand G-family vCPU quota in both regions. Production is Spot-only. Manual region-migration via `aws batch submit-job` when one region's Spot is flat.

### Reference tasks

| Purpose | Task ID |
|---|---|
| SportAI ground truth | `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` |
| Last pre-calibration T5 (known-wrong) | `ad763368-eb3d-40f0-b9fe-84e0c9755c90` |
| **First clean T5 (rev 23 code, radial calibrated)** | `90ad59a8-8853-4014-9fd8-c32af7c4a2e9` |

### S3 reference video

`s3://nextpoint-prod-uploads/wix-uploads/1776237811_match.mp4` — the 10-min match that 90ad59a8 ran against. Already downloaded locally to `ml_pipeline/test_videos/match_90ad59a8.mp4` for stroke training.

═══════════════════════════════════════════════════════════════════════
## RECONCILE NUMBERS — FIRST CLEAN RUN
═══════════════════════════════════════════════════════════════════════

Compared to SportAI ground truth `4a194ff3`:

| Metric | Pre-cal (ad763368) | Post-cal (90ad59a8) | SportAI |
|---|---|---|---|
| Silver rows | 162 | 160 | 88 |
| Serves raw | 1 | **21** | 24 |
| Serves d (Pass 3 gate) | 1 | **17** | 24 |
| Points | 1 | 2 | 17 |
| Games | 1 | 1 | 2 |
| Volleys | 156 | **2** | 5 |
| Forehand | 21 | 80 | 41 |
| Backhand | 0 | 0 | 15 |
| Ball court_y range | [10.7, 24.3] | **[-3.4, 28.6]** | full court |
| Ball speed avg (km/h) | 30 | 44 | 359 |
| server_end_d populated | 20% | **100%** | 100% |

═══════════════════════════════════════════════════════════════════════
## NEXT SESSION — WHAT TO TACKLE, IN ORDER
═══════════════════════════════════════════════════════════════════════

### P0 — Points collapse (17 serves → 2 points)

**Symptom**: `point_number` only increments twice across 17 serves.

**Traced root cause**: every row in the silver output sample has **identical hitter coordinates** (hx=7.13, hy=-4.16) — so every serve computes `serve_side_d = 'ad'` (x > mid for far-server), no alternation, point numbering stagnates.

**Where to look**: `ml_pipeline/build_silver_match_t5.py::_find_nearest_detection` — this pulls the hitter-side player detection nearest IN TIME to the bounce. Suspect it's returning a stale cached detection because the far player is only detected in 10% of frames (1600/15300 per eval-player).

**Fix direction**: tighten the "find hitter" logic — require a detection within N frames (say, ±5 = ±0.2s) of the actual hit, not the bounce. If none available, flag the bounce as `hitter_resolved=False` rather than silently using stale data. Also worth investigating why far-player detection dropped to 10% of frames (was 11% pre-fix — the fresh rev 24 may help).

**Expected gain**: points 2 → 15+ once serve_side alternates correctly.

### P1 — 4 serves lost Pass 1 → Pass 3

**Symptom**: `serves_raw = 21`, `serves_d = 17`. Four rows have `serve=TRUE` but get filtered by Pass 3's `serve_d` check.

**Pass 3 gate** (in `build_silver_v2.py:515-525`):
```sql
serve_d = CASE
  WHEN swing_type IN ('fh_overhead','bh_overhead','overhead','smash','other')
   AND (y < 0.30 OR y > 23.47)
  THEN TRUE ELSE FALSE
```

**Likely culprits**: (a) swing_type assigned `fh`/`bh` by near-player stroke heuristic instead of `overhead` on some serves; (b) hitter_y falls in [0.30, 23.47] band (not close enough to baseline).

**Diagnostic**: enable INFO logging in Pass 1 (`logger.info("T5 serve cand ...")` already exists at line 644 — confirm it's emitting) then walk the counter output:
```
T5 serve diagnostics: {geometric_pass: N, pose_pass: M, cooldown_block: K, fired_primary: J}
```
and cross-reference against Pass 3 `serve_d` survivors.

### P2 — Ball speed 8× under (44 km/h vs 359)

**Hypothesis**: `ball_tracker.py::compute_speeds` averages over ALL 1983 bounce detections including near-stationary "ball rolling between rallies" samples. SportAI only reports peak-at-hit speeds.

**Fix direction**: restrict speed reporting to frames within ±3 of a detected hit event. Or take 95th percentile of speed distribution per rally.

**Read**: `ml_pipeline/ball_tracker.py` — functions `compute_speeds`, `interpolate_gaps`, `detect_bounces`. The court coords are right (confirmed by `court_coord_pct_>=50.0 → 97.2% PASS`), so the projection layer is fine. The issue is the speed's semantic.

### P3 — Backhand classification (0 of 15 SportAI)

**Where**: `ml_pipeline/build_silver_match_t5.py` near-player stroke heuristic (search for `_assign_stroke_near` or similar — uses wrist vs shoulder x-coord).

**Diagnostic**: pick a specific backhand frame (SportAI labels it, we don't) and dump the COCO keypoint positions. If left_wrist_x < left_shoulder_x on a backhand stroke, the heuristic is correct and something else is masking it. If keypoints aren't being detected for the near player in those frames, that's a keypoint-confidence issue.

### P4 — MIN_SERVE_INTERVAL_S cooldown (optional polish)

Currently 8s. Probably blocks 1-2 fault-retry serves per game. Relax to per-point reset once points collapse is fixed. Low-value until P0 lands.

═══════════════════════════════════════════════════════════════════════
## VALIDATION SEQUENCE (run after EACH next code change)
═══════════════════════════════════════════════════════════════════════

After deploying a fix and getting a fresh T5 run:

```bash
# On Render shell (needs DATABASE_URL env):
python -m ml_pipeline.harness validate <new_task_id>
python -m ml_pipeline.harness rerun-ingest <new_task_id>   # if direct-submit bypassing auto-ingest
python -m ml_pipeline.harness reconcile 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb <new_task_id>
python -m ml_pipeline.harness eval-ball <new_task_id>
python -m ml_pipeline.harness eval-player <new_task_id>
python -m ml_pipeline.harness eval-court <new_task_id>
```

**Quick regression check on CloudWatch logs**:

```
grep "court_calibration: LOCKED"      # should say VALIDATED mode=radial rms=6.xx
grep "Option A iter"                   # should converge to bad_indices=[]
grep "per-keypoint errors (metres)"    # mean should be <0.3m
```

If any of these look wrong, calibration regressed — fix that before chasing silver.

═══════════════════════════════════════════════════════════════════════
## STROKE TRAINING — WEEKEND TASK
═══════════════════════════════════════════════════════════════════════

User wants to do training on the weekend once multiple matches are accumulated. Infrastructure ready:

- Local venv: `.venv` (Python 3.14) has `torch==2.11.0+cpu`, `torchvision`, `opencv`, `sqlalchemy` — tested working.
- Training video: `ml_pipeline/test_videos/match_90ad59a8.mp4` (already downloaded).
- Commands (on Render shell for DB access, then local for training):
  ```bash
  python -m ml_pipeline.harness export-stroke-data \
    --sportai-task 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb \
    --t5-task 90ad59a8-8853-4014-9fd8-c32af7c4a2e9 \
    --video ml_pipeline/test_videos/match_90ad59a8.mp4 \
    --output ./stroke_data/
  python -m ml_pipeline.harness train-stroke --data ./stroke_data/ --epochs 50
  ```
- Output lands at `ml_pipeline/models/stroke_classifier.pt` (gitignored by default — **manually add to commit** if wanted to ship, OR change .gitignore to include `.pt` files).

Training on one match yields skewed weights (over-fit to that one match). Real accuracy requires 3-5 matches / 300+ examples / held-out validation. Start proof-of-concept on weekend, accumulate more matches over coming weeks.

═══════════════════════════════════════════════════════════════════════
## OPEN THREADS (not urgent)
═══════════════════════════════════════════════════════════════════════

- AWS on-demand G-family quota request (see `.claude/playbook_aws_batch_ondemand_fallback.md`). Without it, Spot tightness = manual region migration.
- Performance target 55 min → 20 min: untouched. Requires per-stage timing instrumentation first. Realistic post-tuning 35-40 min; 20 min needs batching refactor (~1 day of engineering).
- TrackNetV3 weights unavailable (architecture ported in `tracknet_v3.py`). Ball is already 99.5% detected — not critical.
- SportAI training data is NOT a gold standard:
  - 100% on player xy/movement — trust for calibration
  - 95% on strokes — train stroke classifier on this (model ceiling ~95%)
  - 70% on ball bounces — **DON'T** train bounce detection on this; our rule-based detector is cleaner.

═══════════════════════════════════════════════════════════════════════
## COMMIT CHRONOLOGY (Apr 15)
═══════════════════════════════════════════════════════════════════════

Summary: 25+ commits today, rev 10 → rev 24 (eu) / rev 3 → rev 13 (us-east-1). Timeline in `project_t5_apr15_breakthrough.md` memory file.

Latest commits (most recent first):
- `216d111` chore: tidy VS Code source control + AWS playbook
- `f97690e` fix(t5): legacy branch tier 0 → score 0 + MIN_SELECTABLE_SCORE
- `9525875` fix(t5): tier 0 scores 0, near baseline +8m, SAHI crop margin 10% → 30%
- `ab0b5bc` fix(t5): tier 0 for null-projection candidates + relax pixel gate
- `15094b3` fix(t5): court polygon from calibrated projection when available
- `d54d31c` fix(t5): raise Option A RMS threshold 5.0 → 10.0
- `584c3bb` fix(t5): iterative outlier rejection in calibration
- `09bf724` fix(t5): unblock CNN-path + decouple calibration observations
- `116cd81` feat(t5): lens distortion calibration (Option A + Option C fallback)
- `8a1a253` fix(t5): court calibration locks BEST detection, not most recent (where it all began today)

═══════════════════════════════════════════════════════════════════════
## IF STARTING A FRESH CLAUDE CHAT
═══════════════════════════════════════════════════════════════════════

Paste this as the first message to the fresh agent:

> Continuing T5 ML pipeline work from Apr 15. Lens distortion is solved (rev 24/13 deployed, mode=radial rms=6.26). Last clean run is task `90ad59a8-8853-4014-9fd8-c32af7c4a2e9`. Please read CLAUDE.md (T5 section) and `.claude/handover_t5_current.md` first — they have full context. Top priority today is P0 (points collapse from 17 → 2 due to stale hitter coords in `build_silver_match_t5.py::_find_nearest_detection`), then P1 (4 serves lost between Pass 1 and Pass 3), then P2 (ball speed 8× compressed in `ball_tracker.compute_speeds`), then P3 (backhand classification returning 0 of 15). Validation sequence after each fix is in the handover doc.

That gives a fresh agent everything they need in ~150 words.
