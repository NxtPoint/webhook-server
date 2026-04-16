# HANDOVER — T5 ML Pipeline (Apr 15 eve → Apr 16 start)

## Read CLAUDE.md first — the T5 section is the authoritative reference.

## Apr 16 morning — session log

- Overnight run `fd623ed2` reconciled vs SportAI `4a194ff3`. Improvements: serves_d 17 → 20 (+3 ✓). Concerns: Player 1 frames dropped 1600 → 758 with var_y=155 (identity flipping), points still 2, backhand still 0, ball speed still ~48 km/h.
- User flagged via screenshot: Player B (far) measuring court_y=-7 rejected by tier-2 (-4m cap). Physical distance behind baseline looks ~4m — calibration extrapolates too negatively near the top of the image.
- **Code change merged: A0** — `player_tracker.py::_choose_two_players` tier-2 `behind_baseline` expanded to ±10m on both sides (was -4m / +8m). Tier 1 / 3 unchanged. Follow-up: investigate k1/k2 far-edge residual so tier-2 can tighten again later.
- **Code change merged: A1** — `build_silver_match_t5.py::_find_nearest_detection` now accepts `max_distance_frames`. Caller back-tracks search target by `HIT_BEFORE_BOUNCE_FRAMES` (≈0.32s × fps) to estimate the actual hit frame, gates on `HIT_WINDOW_FRAMES` (≈0.20s × fps). Stale detections are no longer silently reused; the `serve_diag.no_hitter_stale_only` counter tracks how often the gate fires. Expected downstream: hitter coords vary across far-side hits → `serve_side_d` alternates → points 2 → ~15.
- Master plan for rest of dev (Phases A / B / C) written into this doc and CLAUDE.md. Training is explicitly paused until Phase A is green.

**Deployed and submitted** (Apr 16):
- eu-north-1 **job def rev 25**, us-east-1 **rev 14** — both point to `sha256:1f8aa7a3d1a398abc3b4385783478c2cb3444d4e583697e08f8e77741ef1348f`.
- Validation task **`8006ec73-95f5-48c6-ba9f-755cac3ae266`** (batch job `729fca0e-b1ba-4347-9f2d-4c099679af01`) queued in eu-north-1 against `wix-uploads/1776237811_match.mp4`. Clone of `90ad59a8` submission_context so auto-ingest fires on sentinel.
- Orphaned job `249dc06d` marked failed (Spot killed it mid-run Apr 15; DB row was stale).

**Watch for (after run completes)**:
- CloudWatch `T5 serve diagnostics` — `no_hitter_stale_only > 0` (confirms gate is firing; 0 would mean far-side coverage is unexpectedly dense).
- Reconcile points: target 10-15 (up from 2).
- Sample silver rows: hitter coords should show VARIATION across bounces, not a single repeated (hx, hy) pair.
- Tier-2 catch rate: Player B (far) frame count vs 90ad59a8 — should rise once y=-7 observations are no longer rejected.

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
## MASTER PLAN — finish all dev before training (Apr 16+)
═══════════════════════════════════════════════════════════════════════

**Strategy**: Get the pipeline rock-solid FIRST. Only start stroke-classifier
training once Phase A is green on a fresh run. Every iteration before that
burns a dual-submit pair that SportAI charges for — we want each one to
deliver a strictly-better labeled set.

Three phases, strictly sequential: **A = correct, B = fast, C = ops.** Don't
skip ahead. Numbers inside each phase are rough priority; merge order may shift
with findings.

### Phase A — Correctness (blocks training)

Target outcome: reconcile vs SportAI on the reference video closes to
**serves 22-24 / points 12-15 / backhand 10-15 / ball speed 200-400 km/h avg**.

| # | Symptom (vs SportAI) | Location | First move |
|---|---|---|---|
| **A0** | Player B measuring y=-7 rejected by -4m tier-2 cap | `player_tracker.py::_choose_two_players` | ✅ Done — tier-2 expanded to ±10m. Validate on next run that Player B capture rate > 60%. |
| **A1** | Points 2 vs 17 — stale hitter coords | `build_silver_match_t5.py::_find_nearest_detection` | Require hit-frame detection within ±5 frames of the hit event, not the bounce. Flag `hitter_resolved=False` on miss; do NOT silently reuse last seen. |
| **A2** | Player 1 identity instability (var_y=155 in fd623ed2) | `player_tracker.py` tracking loop | Confirm bbox continuity — if ID jumps between two disjoint spatial clusters, tighten assignment with IOU threshold + distance gate. |
| **A3** | Ball speed 48 km/h vs 359 (8× under) | `ball_tracker.py::compute_speeds` | Switch from mean-across-all-bounces to per-rally peak-at-hit ±3 frames (or 95th pct). Court coords are known-correct, it's a semantic bug. |
| **A4** | Backhand 0 vs 15 | `build_silver_match_t5.py` near-player heuristic | Dump COCO keypoints for one SportAI-labeled backhand frame. Validate wrist/shoulder x signal; check keypoint confidence floor didn't swallow them. |
| **A5** | 4 serves lost Pass 1→3 | `build_silver_v2.py:515-525` gate | Enable serve-diag logging in Pass 1; cross-ref swing_type assignments for the 4 dropped rows. Likely `fh`/`bh` instead of `overhead`, OR hitter_y just inside [0.30, 23.47]. |
| **A6** | Wide serve bucket 0 vs 43 | Pass 3 zone derivation for `serve_bucket_d` | Check bounce x-thresholds against actual MATCHI video geometry; likely off by a metre or using wrong court width. |
| **A7** | shot_ix_in_point / rally_length collapse | Pass 3 | Cascades from A1 — should auto-fix once points partition correctly. Verify, don't pre-patch. |

### Phase B — Performance (runtime 55 min → target 30-45, stretch 20)

Only once Phase A is stable — perf tweaks that change detection cadence will
muddle correctness debugging.

| # | Change | Expected gain |
|---|---|---|
| **B1** | Per-stage timing instrumentation in `pipeline.py::_process_frame` (court / ball / MOG2 / player / SAHI). Log stage totals per 1000 frames. | Baseline for the rest of Phase B. |
| **B2** | `PLAYER_DETECTION_INTERVAL` 5 → 8 frames | ~30% player-stage reduction; interpolation between detections already good. |
| **B3** | SAHI tile 640 → 800 px, overlap 15% → 10% | Fewer tiles/frame. |
| **B4** | Skip SAHI when full-frame YOLO already has a valid far candidate | Avoid the big-cost path on easy frames. |
| **B5** | FFmpeg CUDA hardware decode (`h264_cuvid`) | ~10-20% wall-clock. |
| **B6** | Batch YOLO inference across frames (stretch) | Big win, ~1 day eng. |

### Phase C — Operations (non-blocking, schedule alongside A/B)

| # | Change | Why |
|---|---|---|
| **C1** | Submit AWS on-demand G-family vCPU quota request (both regions) | Current 0-quota forces Spot-only; Spot starvation = manual region migration. See `.claude/playbook_aws_batch_ondemand_fallback.md`. |
| **C2** | `harness dual-submit` command — submit SportAI + T5 to same video in one call, return both task_ids + diff URL once done | Turns each training round into one command instead of five. |
| **C3** | `T5_DEBUG=1` env var toggle for diagnostic logging & debug-frame uploads | Production runs stay quiet; debug runs go loud. |

### Stopping condition — move to training when:

- Phase A: 6 of 7 items green on reconcile
- Phase B: wall-clock < 45 min (not the 20-min stretch)
- Phase C: dual-submit tool working, so training prep doesn't bottleneck

Then run 5 dual-submit matches, export stroke data, train, re-benchmark.

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
