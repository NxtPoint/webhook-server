# HANDOVER — T5 ML Pipeline (end of Apr 16)

## Read CLAUDE.md first — the T5 section is the authoritative reference. This doc is the working state.

═══════════════════════════════════════════════════════════════════════
## TL;DR — where we are end of Apr 16
═══════════════════════════════════════════════════════════════════════

**3 things fundamentally solved on Apr 16**:
- **Ball speed**: from 30 km/h (m/s unit bug) → p95 = 132 km/h matching SportAI's 129 km/h within 3 km/h. Real.
- **Court calibration + far-player mapping**: user visually verified on frame 300+ that Player B at y=-7 is now correctly kept. A0 strict=False fix unblocked the tier-2 widening.
- **SILVER VALIDATION = PASS** (harness validate 081e089c). First clean pass.

**1 thing with structural work ahead**: serve detection. 17 detected vs SportAI's 24. Some false positives still (ts=15.3 warmup), serve_side_d skew, low-speed noise. Genuine progress but not done.

═══════════════════════════════════════════════════════════════════════
## BASELINE — task 081e089c (Apr 16 end-of-day reference)
═══════════════════════════════════════════════════════════════════════

**Task ID**: `081e089c-f7b1-49ce-b51c-d623bcc60953`
**Region / image**: eu-north-1 / `sha256:378f0219846d3f3b193dedc45276de62a2d64fe992c519ead119447ed1ffb334` (rev 28 eu, rev 17 us — pre strict=False). Silver built with ALL Render-side fixes.

### Reconcile vs SportAI `4a194ff3`

| Metric | Pre-calibration (Apr 15, ad763368) | Start of Apr 16 (90ad59a8) | **End of Apr 16 (081e089c)** | SportAI target |
|---|---|---|---|---|
| Silver rows | 162 | 160 | 160 | 88 |
| Serves raw | 1 | 21 | **17** | 24 |
| serves_d | 1 | 17 | **17** | 24 |
| **Points** | **1** | **2** | **6** | 17 |
| Games | 1 | 1 | 1 | 2 |
| Volleys | 156 | 2 | 5 | 5 |
| Forehand | 21 | 80 | 88 | 41 |
| **Backhand** | **0** | **0** | **43** | 15 |
| Overhead | — | — | 3 | 1 |
| **Ball speed avg** | **30** | **44** (m/s bug) | **72.1** | 99.7 |
| **Ball speed p95** | — | — | **132.1** | **129.2** ✅ |
| **Ball speed max** | — | 246.8 (clamp) | **175.3** | 132.5 |
| Ball y range | [10.7, 24.3] | [-3.4, 28.6] | [-3.4, 28.6] | full court |
| Serve bucket wide | 0 | 0 | 16 | 43 |
| Serve bucket T | — | — | 40 | 4 |
| server_end_d populated | 20% | 100% | 82% | 100% |
| point_number populated | — | 62% | **82%** | 100% |
| shot_ix_in_point populated | — | 9% | 22% | 88% |

### Eval signals (081e089c)

- **eval-court**: PASS (confidence 0.857, VALIDATED mode=radial rms=6.26)
- **eval-player**: PASS. Player 0 (near) var_y=1.65 (rock solid), 1590 frames. Player 1 (far) var_y=104 (umpire interference — A8 remaining), 1656 frames (up from 1283 pre-A0-strict=False).
- **eval-ball**: detection 13.0%, 162 bounces, court_coord 97.2%, speed_max 246.8 (non-bounce pairwise still has jitter but bounces corrected to 175).
- **BRONZE VALIDATION**: PASS (except ball court_x/y out-of-range — known calibration extrapolation edge, non-blocking).
- **SILVER VALIDATION**: **PASS** (all fields present, coverage above thresholds).

═══════════════════════════════════════════════════════════════════════
## PHASE A STATUS (correctness — the list you asked for)
═══════════════════════════════════════════════════════════════════════

| # | Item | Status | Notes |
|---|---|---|---|
| **A0** | Player tier-2 widen + strict=False | ✅ **DONE** | Far player at y=-7 now caught. User visually verified on frame 300+. |
| **A1** | Hitter-window gate (+soft-fallback #2) | ✅ **DONE** | Hitter coords vary per bounce (no more stale 7.02/-4.44). |
| **A2** | Player identity guards | ✅ **PARTIAL** | var_y 155→104. Umpire still wins some frames. Needs A8 to fully resolve. |
| **A3a** | ball_speed unit (km/h) | ✅ **DONE** | Phantom "359 km/h" gone, units consistent. |
| **A3b** | Peak flight speed (p75 over window) | ✅ **DONE** | Max clamps gone, p95 matches SportAI within 3 km/h. |
| **A4** | Dual-window keypoint + #1 stroke_d 'bh' | ✅ **DONE (over-counts)** | Backhand pipeline works (0→43). Over-classifies vs SportAI 15 — needs heuristic calibration, non-blocking. |
| **A5** | eps widen + FIRST_SERVE_MIN_TS + stationarity | ✅ **PARTIAL** | eps=1.5 landed, ts=15.3 warmup still slips through — bump to 30s. Stationarity working but can't catch first-2s bounces. |
| **A6** | Wide serve bucket fix | ❌ **NOT STARTED** | T bucket 40 vs 4 (over), wide 16 vs 43 (under). Bounce x-thresholds need tuning. |
| **A7** | shot_ix_in_point / rally_length | ⚠️ **CASCADES** | Currently 22% vs SportAI 88%. Root: missing serves/points; improves as A5/A6 land. |
| **A8** | Non-player filter (umpire) | ❌ **NOT STARTED** | Umpire wins far-slot on some frames (tier 1 in-court beats tier 2 behind baseline). Needs motion-persistence or aspect-ratio or explicit zone exclusion. Major contributor to Player 1 var_y=104. |

**Newly discovered — Apr 16 evening**:
| Item | Details |
|---|---|
| **A5 follow-up — FIRST_SERVE_MIN_TS_S too loose** | 15s allowed ts=15.3 warmup serve. SportAI's first real is ts=54.5. Bump to 30s (conservative). |
| **serve_side_d skew** | T5 5 deuce / 12 ad (skewed). SportAI alternates properly. Logic mis-computes from hitter_x. Investigate. |
| **Low-speed serve bug** | p75 over window returns slow values when no pre-hit flight data exists (window all-slow). Some serves show 0.1, 13, 17 km/h. Require window to have ≥ N frames with speed > threshold, else leave NULL. |
| **Cooldown (MIN_SERVE_INTERVAL_S=8)** | Probably blocking fault-retry serves. SportAI has 24 serves for 17 points (1.4 serves/point). T5 has 17/6 (2.8/point) — cooldown may be letting through SECOND bounces of the same serve, and rejecting legitimate retries. Investigate. |

═══════════════════════════════════════════════════════════════════════
## PHASE B STATUS — performance (47-min run, data collected today)
═══════════════════════════════════════════════════════════════════════

**B1 timing data from 081e089c** (CloudWatch, full-run FINAL):

```
stage_timings FINAL [total=2822.9s=47min]
  court=2.2s (0%)  ball=714.6s (25%)  motion_mask=623.0s (22%)  player=1483.0s (53%)  postprocess=0s
player_sub FINAL [player_total=1480.9s  sahi_ran=3166 sahi_skipped=2]
  full_yolo=410.9s (28%)  sahi=1065.4s (72%)  choose2=1.7s (0%)  other=2.9s (0%)
```

**Key finding: SAHI is 38% of total runtime (1065s of 2823s). Current B4 skip rule fires only 2 times out of 3168 player frames (<0.1%). Huge optimization wedge.**

| # | Item | Status | Expected savings |
|---|---|---|---|
| **B1** | Per-stage timing instrumentation | ✅ **DONE** | Data collected. Gives us this table. |
| **B4+** | Tighten SAHI skip rule | ❌ **TOP PRIORITY** | Currently <0.1% skip rate. If we skip when full-frame YOLO has 2 candidates OR far player tracked last N frames, easy 40-60% skip rate = **~400-600s saved (14-21%)**. |
| **B3** | SAHI tile 640→800, overlap 15%→10% | ❌ Not started | ~200-300s saved. Risk: larger tiles = less zoom on 30-40px far player; may reduce far detection rate. |
| **B2** | PLAYER_DETECTION_INTERVAL 5→8 | ❌ Not started | ~550s on player stage. Risk: reduces pose coverage further (A4 already pose-starved at 13%). |
| **B5** | FFmpeg CUDA decode | ❌ Not started | ~200-400s. High risk — real code rewrite. |
| **B6** | YOLO frame batching | ❌ Not started | ~500-800s. Very high — ~1 day of work. |

**Realistic Phase B target**: 47 min → ~30 min with B4+ (SAHI skip rule) + B3 (tile config). No architecture changes.

═══════════════════════════════════════════════════════════════════════
## PHASE C STATUS (ops, non-blocking)
═══════════════════════════════════════════════════════════════════════

| # | Item | Status |
|---|---|---|
| C1 | AWS on-demand G-family vCPU quota request | ❌ User action needed via AWS Support (~24-48h turnaround). |
| C2 | `harness dual-submit` one-command tool | ❌ Not started. |
| C3 | `T5_DEBUG=1` env var toggle for live debug | ❌ Not started. |
| — | Cross-region S3 (was blocking us-east-1) | ✅ Fixed in `__main__.py` via bucket-region auto-discovery. |

═══════════════════════════════════════════════════════════════════════
## KEY INSIGHTS / LESSONS LEARNT ON APR 16
═══════════════════════════════════════════════════════════════════════

1. **Tier widening alone doesn't help** — if `to_court_coords(strict=True)` returns None for y < -5, the widened zone never gets consulted. Config changes in the wrong branch are no-ops. Always trace the call chain before claiming a config fix is live.

2. **Shadowed config constants** — `EPS_BASELINE_M` was defined in TWO places (build_silver_v2.py and build_silver_match_t5.py). T5 uses its own local copy via SPORT_CONFIG_SINGLES, so the shared-config fix was a no-op. Memory saved at `feedback_shadowed_config_constants.md`.

3. **Render auto-deploy lag** — commits need to propagate to Render before `rerun-ingest` uses them. Auto-ingest that fires at batch-end can race with Render rebuilds. Always push, wait 2-3 min, THEN rerun. Memory saved at `feedback_push_before_rerun.md`.

4. **Ball-speed fix was a chain of 3 bugs**:
   - Unit inconsistency (T5 m/s vs SportAI km/h)
   - Bounce-frame pairwise speed is not "speed at hit" (needed peak-over-window)
   - `max()` over window is fragile to TrackNet jitter (needed p75)
   Fixing one without the others produced nothing meaningful.

5. **B1 timing data revealed the real bottleneck** — SAHI at 72% of player stage. Every hour of engineering effort on ball / court / choose2 would have been a waste. Measurement before optimization is mandatory.

6. **Parallel front-end submission as Spot insurance** — user submitted via Media Room while dc5e1945 retry was stuck, and the frontend run won. Same image, same data. Insurance against Spot pool instability in either region. Keep using this pattern on important runs until on-demand quota lands.

7. **`rerun-silver` vs `rerun-ingest`** — rerun-ingest re-downloads bronze JSON from S3 (fails if JSON was cleaned up). rerun-silver rebuilds from existing ml_analysis.* tables. For iterating on silver code against the SAME bronze, always use rerun-silver.

═══════════════════════════════════════════════════════════════════════
## DEPLOYMENT STATE
═══════════════════════════════════════════════════════════════════════

| Rev | Region | Image digest | Contents |
|---|---|---|---|
| **29 / 18** | eu / us | `sha256:7d38fded6e85be6bcffa5f220821a058b55651986a1a1d4e328bf62f0168afd5` | Current latest. A0-A5 + #1 + #2 + cross-region S3 + B1 + strict=False |

**Render main branch is at** commit `1e87a91` (A3b p75 ball-speed fix) — auto-deployed. Includes all silver-side fixes: A1, A3a, A4, A5 eps=1.5, A5+ stationarity, FIRST_SERVE_MIN_TS_S=15.0, #1 bh-mapping, #2 soft-fallback.

**Not yet in a Batch image** (pushed to main but need rebuild to apply):
- `1e87a91` — ball_tracker.py p75 over window (currently only backfilled manually on 081e089c; next Batch run picks it up natively)

═══════════════════════════════════════════════════════════════════════
## REFERENCE TASKS
═══════════════════════════════════════════════════════════════════════

| Purpose | Task ID |
|---|---|
| SportAI ground truth | `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` |
| Pre-calibration T5 (known-wrong, Apr 15 AM) | `ad763368-eb3d-40f0-b9fe-84e0c9755c90` |
| Post-calibration, pre-today (Apr 15 PM) | `90ad59a8-8853-4014-9fd8-c32af7c4a2e9` |
| First successful today (Apr 16 AM, A0+A1+A2) | `a015bf3a-a6e6-47ae-9988-55f9bffc9820` |
| **Apr 16 end-of-day baseline (everything)** | **`081e089c-f7b1-49ce-b51c-d623bcc60953`** |

**Reference video S3**: `s3://nextpoint-prod-uploads/wix-uploads/1776237770_match.mp4` (re-uploaded Apr 16 after original 1776237811 was cleaned up). Local copy at `ml_pipeline/test_videos/match_90ad59a8.mp4.mp4` (50.8 MB).

═══════════════════════════════════════════════════════════════════════
## VALIDATION SEQUENCE (run after each code change)
═══════════════════════════════════════════════════════════════════════

**For silver-code changes (Pass 1/3/5, stroke_d, etc)** — no Batch run needed, ~10 seconds:
```bash
# On Render shell (DATABASE_URL env set):
python -m ml_pipeline.harness rerun-silver <task_id>
python -m ml_pipeline.harness reconcile 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb <task_id>
```

**For Batch-side changes (ball_tracker, player_tracker, pipeline, camera_calibration)** — need fresh run:
1. Docker build + push to ECR (both regions)
2. Register new Batch job def revision
3. Submit via `aws batch submit-job` OR via Media Room upload (preferred — auto-ingest works)
4. ~47 min wait for completion + auto-ingest
5. Reconcile + evals

**Full eval suite**:
```bash
python -m ml_pipeline.harness validate <task_id>
python -m ml_pipeline.harness reconcile 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb <task_id>
python -m ml_pipeline.harness eval-ball <task_id>
python -m ml_pipeline.harness eval-player <task_id>
python -m ml_pipeline.harness eval-court <task_id>
```

**Serve viewer** (local, Windows):
```bash
DATABASE_URL="..." python -m ml_pipeline.diag.serve_viewer <task_id> \
    --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \
    --output ./diag_<tid>
```

═══════════════════════════════════════════════════════════════════════
## NEXT SESSION — PRIORITY ORDER
═══════════════════════════════════════════════════════════════════════

### P0 — Serve detection quality (the remaining Phase A work)

1. **Bump FIRST_SERVE_MIN_TS_S 15s → 30s** — 1-line change in `build_silver_match_t5.py`. Kills the ts=15.3 warmup false positive. SportAI's first real serve is 54.5s, 30s is safely conservative.

2. **Investigate serve_side_d skew** (5 deuce / 12 ad vs SportAI's balanced). Look at the `serve_side_d` CASE in `build_silver_v2.py` Pass 3 — probably uses dynamic mid_x that gets skewed when most serves are from one side.

3. **Fix low-speed serve artefacts** — `assign_peak_flight_speeds` should return None (not the p75) when fewer than N genuine flight samples exist in the window. Signals the silver builder to leave ball_speed NULL instead of reporting 0.1 km/h.

4. **Review MIN_SERVE_INTERVAL_S=8** — SportAI has 24 serves / 17 points = 1.4 serves/point (fault retries). T5 has 17/6 = 2.8/point which implies we're LETTING THROUGH second bounces of the same serve AND rejecting legitimate retries. Split into two mechanisms: aggressive same-serve-dedup (3s) and legitimate-retry allowance (per-point reset).

5. **A6 — wide serve bucket** (40 T vs 4, 16 wide vs 43). Check the x-thresholds in Pass 3 `serve_bucket_d` CASE against actual MATCHI court geometry.

### P1 — A8 umpire filter (Player 1 var_y=104)

Real source of Player 1 oscillation. Umpire at y=11-12 scores tier-1 (in-court) 3000 vs real far player tier-2 (behind baseline) 2000. Options:
- **Motion-persistence**: real player moves across multiple frames; umpire sits still. Use MOG2 history over 3-5 seconds.
- **Aspect-ratio**: sitting umpire bbox has different aspect than standing player.
- **Explicit pixel-zone exclusion**: umpire chair area is known fixed region.

Recommend motion-persistence — cleanest, no fragile pixel zones.

### P2 — Phase B optimisation (biggest wedge identified)

**B4+ — tighten SAHI skip rule** (top priority, low risk). Currently skips <0.1% of frames. Proposed rule: skip SAHI when full-frame YOLO returned ≥2 candidates AND one of them is in the far half. Expected 40-60% skip rate → **~400-600s runtime saved (47 min → 35 min)**.

**B3 — SAHI tile config** (second priority). Bigger tiles, less overlap. Test against far-player detection rate on a short clip.

### P3 — A4 heuristic calibration (over-counting backhands)

Backhand 43 vs SportAI 15. A4's wider-window keypoint lookup is now finding poses but the FH/BH heuristic (in `_infer_swing_type_from_keypoints`) is too aggressive. Dump COCO keypoints for SportAI-labelled forehands vs backhands and calibrate the wrist/shoulder x-threshold.

### P4 — Auto-generate serve-diag visuals in the pipeline (~45 min of work)

Right now the serve viewer (`ml_pipeline/diag/serve_viewer.py`) is a manual tool requiring the local video. Wire it into auto-ingest so every T5 run produces visual diagnostics automatically.

Steps:
1. New harness command `python -m ml_pipeline.harness generate-diag <task_id>` — downloads trimmed video from S3 (`s3://{bucket}/trimmed/{task_id}/practice.mp4`), runs the viewer, uploads outputs to `s3://{bucket}/diag/{task_id}/` (contact_serves.png + contact_overheads.png + per-shot subfolders).
2. Hook into `upload_app.py::_do_ingest_t5` after silver build completes: one extra function call, fire-and-forget background thread (like video trim).
3. Store pointer in `ml_analysis.video_analysis_jobs.diag_s3_prefix` (new column, ALTER TABLE IF NOT EXISTS pattern).
4. New client API `GET /api/client/match/serve-diag/<task_id>` returns presigned URL for `contact_serves.png`.
5. Dashboard button in `match_analysis.html` opening the contact sheet inline.

Cost: ~30s per task on Render ingest worker, ~40MB S3 per task. No GPU compute, no Batch impact. Runs in parallel with video trim.

Payoff: never SSH to Render shell again to validate a run. Every future task has visual proof-of-classification accessible from the dashboard.

### Stopping condition to move to stroke-classifier training

6 of 9 Phase A items green, wall-clock < 40 min, dual-submit tool working. Then 5 dual-submit matches → train stroke classifier → re-benchmark.

═══════════════════════════════════════════════════════════════════════
## STARTER PROMPT FOR NEXT SESSION
═══════════════════════════════════════════════════════════════════════

```
Continuing T5 ML pipeline work. End of Apr 16 we landed a major milestone:
SILVER VALIDATION = PASS on task 081e089c-f7b1-49ce-b51c-d623bcc60953.
Ball speed is p95 = 132 km/h matching SportAI 129. Player B (far side)
correctly tracked after A0 strict=False fix. Points 1→6 (SportAI 17).

Please read `.claude/handover_t5_current.md` first — it has the full
Apr 16 baseline, Phase A/B/C status table, key insights learned, and
ranked next-session priorities (P0/P1/P2/P3).

Today's priority is P0 — serve detection quality. Four fixes in order
(all small, silver-side only — no Batch rebuild needed):

  1. Bump FIRST_SERVE_MIN_TS_S 15 → 30 in build_silver_match_t5.py
     (kills ts=15.3 warmup false positive)
  2. Investigate + fix serve_side_d skew (5 deuce / 12 ad)
  3. p75 should return NULL when flight-sample count is too low
     (kills 0.1 / 13 / 17 km/h phantom serves)
  4. Split MIN_SERVE_INTERVAL_S into same-serve-dedup (3s) vs
     legitimate-retry (per-point reset) — T5 has 2.8 serves/point
     suggesting both directions broken

Then P1: A8 umpire filter via motion-persistence (real source of
Player 1 var_y=104 umpire interference — tier-1 in-court umpire
beats real far player at tier-2).

Then P2: SAHI skip rule is top perf win (72% of player stage, <0.1%
skip rate today). Before coding optimization, re-read B1 timing data
in the handover — measurement-driven, not guessed.

Then P4: auto-generate serve-diag visuals in the auto-ingest flow
(new harness command + hook into _do_ingest_t5 + upload to S3 +
client API + dashboard button). ~45 min of work; means every future
T5 run produces visual serve-classification diagnostics accessible
from the dashboard — no more SSH to Render for validation.

Reference task for comparison: 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb
(SportAI ground truth).

Starter session checks:
  git status                           # clean, no uncommitted
  git log --oneline origin/main..HEAD  # empty, should have 0 ahead
  cat .claude/handover_t5_current.md   # this doc
```

═══════════════════════════════════════════════════════════════════════
## COMMIT CHRONOLOGY (Apr 16)
═══════════════════════════════════════════════════════════════════════

Chronological session commits for git-log reference:
- `753d8fb` A0 + A1 (tier-2 widen, hitter-window gate)
- `f6b5d05` A2 (player identity guards)
- `111174b` A3 (ball_speed unit + peak flight)
- `3ff78ef` A4 (dual-window keypoint)
- `aa1157f` cross-region S3
- `a6ec499` B1 per-stage timing
- `16191f0` #1 stroke_d bh + #2 soft-fallback
- `b6e9d7a` A0 follow-up (strict=False) — the fix that unblocked far player
- `bec5e5e` A5 eps widen (wrong file, no-op)
- `bcb1ec3` serve viewer
- `ae1a454` A5+ stationarity
- `2f4a53d` A5 eps actually landed (right file) + FIRST_SERVE_MIN_TS_S
- `1e87a91` A3b p75 not max

Plus repeated docs/handover updates.
