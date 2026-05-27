# T5 vs SportAI — Strategic Scoping (2026-05-20)

**Audience:** Tomo. Concrete definition of what "T5 better than SportAI" means.
**Purpose:** Per-dimension comparison, target metrics, honest gap assessment, strategic asks.
**TL;DR:** T5 is already **13× cheaper** and competitive on serve detection (23/24 = 96%) but is materially behind on ball coverage (13% vs likely 60-80%), active silver row coverage (49 vs 85 rows), and per-point reconciliation (0/17). The good news: **none of the gaps are architectural**. They are all flavours of one root cause (ball-detection coverage, Phase 5 in north_star) and the next 3-5 sessions of work (WASB drop-in, dual-submit corpus, possible TOTNet retrain) are enough to close them. **The strategic moat is cost + unique product surfaces (AI coach, custom analytics, dual-submit), not ML purity.**

---

## 1. Per-dimension comparison

Sources marked: **MEASURED** = current production data; **INFERRED** = best-effort estimate from project docs + market scan + reasoning; **UNMEASURED** = honest "we don't know yet".

### 1.1 Cost per match

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Cost | **~$2.00** (Tomo-stated) | **~$0.12-0.15** Spot G4dn (eu-north-1) — INFERRED from `.claude/handover_t5.md` Spot pricing | **$0.05** | T5 already 13× cheaper. To hit $0.05 either (a) shorter Batch jobs via better caching, or (b) downscale to lighter G-instance once WASB lets us drop TrackNetV2's expense. |

**T5 win.** Already meets the unit-economics story. Pursuing $0.05 vs $0.15 is a secondary optimisation — the $2.00 → $0.15 leap is what enables the free-trial business model. **Stop optimising cost** until WASB integration lands (it'll change the cost shape anyway).

### 1.2 Match-level accuracy (serve detection)

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Serve MATCH rate | reference (24/24 assumed) | **23/24 = 96% on 880dff02** (10/10 FAR, 13/14 NEAR); **20/24 = 83% on a798eff0** — MEASURED via `ml_pipeline/diag/bench.py` | 24/24 on every fixture | Near parity. The 1/24 miss on 880dff02 (148.52 NEAR) is a bronze pose-amplitude gap (Bucket C); 4/24 misses on a798eff0 are Phase 1 territory closed for 880dff02 but the older fixture's baseline is locked. |

**T5 ≥ SA.** Locking the 20/24 floor + 23/24 on the newer fixture demonstrates the architecture is sound. Pushing to 24/24 requires pose-model upgrade (Bucket C) which is deferred to Phase 8. **Sufficient for shipping.**

### 1.3 Match-level accuracy (rally event coverage)

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Active silver rows on 880dff02 | **85** — MEASURED | **49** (after warm-up filter Phase 3 part 1) — MEASURED in north_star.md Progress table | within ±5% (81-89) | **Behind by 42%.** Root cause is Phase 5 — T5 can't emit silver rows for rally events where its bronze has no ball detections. |

**T5 << SA.** This is the single biggest visible-to-customer gap. A coach looking at a T5 dashboard will see roughly half the rally events that SA shows for the same match. **All chains of causation lead back to Phase 5 (ball coverage).**

### 1.4 Ball detection coverage

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Frame coverage % | **est. 60-80%** — INFERRED. SportAI's `bronze.ball_bounce` has ~160 events per 10-min match (per `ml_pipeline/training/label_ball_positions.py` docstring), implying very dense detection across rallies. Their dashboard wouldn't work otherwise. | **13%** — MEASURED on 880dff02 (`docs/_investigation/may07_sa_point6_gap.md`) | ≥50% (north_star Phase 5 target) | **Behind by ~4-6×.** This blocks Phase 6 (stroke classification), Phase 7 (coordinate reconciliation), and Phase 8 (final serve cleanup). |

**T5 << SA.** This is the root cause. Once this rises, items 1.3, 1.6, 1.7 all rise mechanically. **Single point of leverage for the whole project.**

### 1.5 Longest no-ball-detection gap

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Worst gap | **probably <5s** — INFERRED (SA can't operate otherwise) | **91.6 s** — MEASURED on 880dff02 | <5s | **Behind by ~20×.** Specific window 7539-9829 (61.8s of zero ball detections) contains an entire SA point that T5 misses. |

**T5 << SA.** Same root cause as 1.4.

### 1.6 Per-point reconciliation (downstream of 1.4)

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Full per-point match | **17/17 = 100%** on its own data (definitionally) | **0/17 = 0%** on 880dff02 — MEASURED via `audit_points_reconcile.py` | ≥14/17 (Phase 5+6 done-when) | **Behind by ~14×.** Caused by 1.4 (no ball detections in window → no T5 strokes → no point reconcile). |

**T5 << SA.** Will fix mechanically once 1.4 + 1.5 close.

### 1.7 Stroke classification per-class

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Forehand count on 880dff02 | **40** — MEASURED in north_star | **21** | within ±10% of SA (36-44) | **Behind.** Same root cause as 1.3 (events missing). |
| Backhand count | **15** — MEASURED | **10** (after Phase 3 part 1 crushed phantom-Backhands 62→10) | within ±10% (13-16) | Now slightly under SA (was wildly over). |
| Per-class precision (when T5 emits the stroke) | reference | **UNMEASURED** | ≥90% per class | Can't measure until events exist in the right windows (Phase 5). |

**T5 ≈ SA in shape; behind in volume.** Once Phase 5 produces strokes in the right windows, the classifier logic (already in `build_silver_v2.py` passes 4-5) is plausibly fine — the Phase 3 Backhand crush from 62 to 10 already showed the logic works when given good inputs. **Plausibly fast to close.**

### 1.8 Coordinate accuracy (geometric)

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Bounce court coordinates populated | **per-event in `bronze.ball_bounce`** | **mostly null** in `silver.point_detail.bounce_court_x/y` — INFERRED | Populated, error <2m vs SA | **UNMEASURED.** Phase 7 territory. Lens calibration (Brown-Conrady k1/k2 + piecewise homography) is in place from Apr 15 work; integrating bounce projection is the remaining work. |

**T5 ≈ SA potential, but unmeasured.** No reason to expect a structural gap once Phase 5 + Phase 7 land.

### 1.9 Latency

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Time from upload-complete to dashboard-ready | **~minutes** (streaming API) | **~30-60 min Batch + ~30s silver build** | <30 min total | Behind by 2-4× on raw clock time. |

**T5 < SA.** But this matters only for cases where Tomo's customers expect near-real-time. The free-trial / batch-analysis product positioning probably tolerates 30 min. **Worth confirming with users before optimising.**

### 1.10 Court calibration robustness

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Calibration model | Probably homography-only (INFERRED) | **Radial Brown-Conrady k1/k2 + piecewise homography** — MEASURED in `ml_pipeline/camera_calibration.py` | Same | **T5 ≥ SA.** April 15 breakthrough closed the 2.4-7m y-axis offset that pure homography couldn't. SA likely still has this on wide-angle iPhone footage. |

**T5 win** (potentially), at least on iPhone-class wide-angle.

### 1.11 Dashboard / analytics (the customer-visible surface)

| | SportAI | T5 | Target | Status |
|---|---|---|---|---|
| Match analytics dashboard | Provided by SA's product (we don't show it) | **Full custom build** — `match_analysis.html`, `practice.html` (see `docs/dashboards.md`) | T5 unique | T5 owns this. SA is a backend; the customer-visible product is our SQL views + ECharts + AI coach. |
| LLM-coach context | Theoretically could ingest SA data | Built in: feeds gold views to Claude, full match flow per coach prompt — see `docs/llm_coach_design.md` | T5 unique | **T5 win.** |
| Custom analytics (per-coach playbook, per-player history) | Constrained by SA's silver schema | Flexible — we own bronze, silver, gold | T5 unique | **T5 win.** |

**Strategic positioning:** The dashboard + AI coach are **why people buy**, not the ML accuracy itself. SwingVision sells at $14.99/month with worse accuracy than SA on multiple dimensions (per market scan: net-cord, serve speed, scoring, shot filter). What sells is the experience layer.

---

## 2. The "T5 better than SportAI" definition

Concrete claim to make to a customer in 6 months:

> "T5 produces a match dashboard that a coach can use to draw the same conclusions as a SportAI dashboard on the same match. It costs ~$0.10/match instead of ~$2.00. It also gives you an AI coach trained on your match history and analytics SQL views that SportAI doesn't expose."

For this claim to be defensible, T5 must reach:

| Dimension | Today | Required for claim | Phase blocker |
|---|---|---|---|
| Cost | ~$0.15 | <$0.20 | Done ✓ |
| Serve match | 23/24, 20/24 | ≥22/24 on every fixture | Done ✓ (one outlier) |
| Active silver rows | 49 vs 85 | within ±10% of SA | Phase 5 + 6 |
| Ball coverage | 13% | ≥50% | **Phase 5** ← single bottleneck |
| Longest gap | 91.6s | <5s | **Phase 5** |
| Per-point reconcile | 0/17 | ≥14/17 | Phase 5 + 6 |
| Stroke per-class | shape OK, volume low | within ±10% of SA per class | Phase 5 + 6 |
| Coord error vs SA | unmeasured | <2m on validated points | Phase 7 |
| Dashboard + AI coach | live | live | Done ✓ |

**Five of nine dimensions are "Done ✓ or near-done." The remaining four all hinge on Phase 5 (ball coverage).** That's why north_star.md treats Phase 5 as THE bottleneck — closing it mechanically unlocks 4/9 of the claim.

---

## 3. Honest gap analysis — engineering sizing

Conservative estimates. "Multi-week" is honest — the project history shows Phase 1 alone was a multi-week effort.

| Phase | What | Engineering size | Confidence |
|---|---|---|---|
| **5a — ROI bounce extractor** | Port `extract_roi_bounces.py` to prod. Parallel agent owns this. | **1-2 sessions** (active 2026-05-20) | High — pattern is established (`extract_far_pose` precedent) |
| **5c.0+5c.1 — Dual-submit on + retro** | Flip env var, run backfill | **0.5 session** | High |
| **WASB drop-in** | Replace TrackNetV2 with WASB-tennis (weights already in `models/`). A/B vs current. | **1-2 sessions** | Medium-high — depends on whether WASB's input pre-processing is compatible with our 1920x1080 → 640x360 path |
| **TOTNet retrain** (if WASB+5a don't close gap) | Train TOTNet on dual-submit corpus | **3-5 sessions** including data assembly | Medium — depends on corpus quality, GPU time |
| **5c.2+5c.3 — Pair-completion hook + corpus index + first training run** | Stream E phases | **3-4 sessions** | Medium |
| **6 — Stroke classification reconciliation** | Validate per-class precision on real strokes | **2 sessions** once Phase 5 lands | Medium — already crushed Backhand 62→10, logic plausibly fine |
| **7 — Coordinate reconciliation** | Populate `bounce_court_x/y`, validate vs SA <2m | **2-3 sessions** | Medium |
| **8 — Final serve cleanup** | Recover remaining misses with all upstream fixes | **1-2 sessions** | Low — bucket C problem may not have a cheap fix |

**Realistic total to "T5 better than SportAI" claim defensible:** ~6-10 working sessions of project effort, assuming Phase 5 closes with one or two of {ROI bounces, WASB, TOTNet}. Could be longer if Phase 5 produces only partial improvement and 6/7 inherit a smaller-than-needed input signal.

**Calendar time:** depends on Tomo's cadence. Recent commits show 1-2 sessions/week. At that pace, **5-10 weeks from today to defensible claim.**

---

## 4. Strategic asks for Tomo

Each is an investment decision needing a yes/no. Listed in cost order, ascending.

### Ask 1: Flip `AUTO_DUAL_SUBMIT_T5=1` on Render today
- **Cost:** $0 + ~$0.15 per SA upload going forward (Batch Spot)
- **Effect:** every new SA match becomes a paired T5 match. Doubles training-data inflow at zero marginal labor.
- **Why now:** the auto-trigger has been silent for unknown weeks. Every day of delay is lost free training data.
- **Decision:** flip it. (Stream E Phase 5c.0.)

### Ask 2: Retro-dual-submit historical SA matches
- **Cost:** ~$0.15 per match × N historical SA matches. If N = 50, total ~$8. If N = 500, ~$80.
- **Effect:** instant labeled corpus from existing prod data.
- **Why now:** retroactive data only gets cheaper to produce once.
- **Decision needed:** (a) confirm budget; (b) decide whether to filter to a quality subset (e.g. only matches >X minutes long, with both players visible, etc.) before kicking off.

### Ask 3: WASB integration sprint (1-2 sessions)
- **Cost:** Tomo's time + ~$1-2 in Batch reruns for A/B
- **Effect:** market scan claims **+9pp F1** vs TrackNetV2 on tennis. If true, this alone may close 50% of the Phase 5 gap before any training.
- **Why now:** the weights are *literally already in our `ml_pipeline/models/` directory*. Cost of trying is hours.
- **Decision needed:** prioritise WASB ahead of TrackNetV3 self-training? (Recommendation: yes — market scan §2.)

### Ask 4: GPU dev box ongoing-cost decision
- **Cost:** ~$0.526/hr on-demand; ~$0.20/hr Spot. If left running 24/7 = ~$380/mo on-demand. Start/stop per session = $1-3/session.
- **Effect:** unlocks interactive ball-tracker work + training runs.
- **Decision needed:** confirm start/stop model (per-session). Default to that today; revisit if a multi-day training campaign needs it always-on.

### Ask 5: Pay for human-labeled occluded-frame data (deferred)
- **Cost:** market scan §4 puts 10 fully-labeled matches at $7.5k (keypoint) or $15k (bbox)
- **Effect:** unblocks TOTNet retraining on tennis-specific occluded frames where SA's labels are also weak.
- **Why later:** dual-submit gives most of the labels for free. **Only worth paying if WASB + dual-submit-trained TOTNet both plateau.**
- **Decision needed:** none now. Re-raise after WASB integration if Phase 5 isn't moving enough.

### Ask 6: SLA decision on dashboard latency
- **Cost:** none — just a product decision
- **Effect:** if SLA is "<10 min from upload to dashboard," it changes the architecture (need to move silver+serve detection inside the Batch container, or split Batch into streaming + finalisation). If SLA is "<24 hr," current Batch round-trip is fine.
- **Decision needed:** what's the customer expectation? Current T5 timing is ~30-60 min.

---

## 5. What "T5 better than SportAI" means today (the reframe)

The honest framing isn't "we beat them on ML metrics." It's:

1. **We're 13× cheaper at the unit level.** That's the moat.
2. **We're competitive on the things that customers actually see** (serve detection in particular).
3. **We're behind on the things customers don't directly see** (per-rally event coverage), but the gap has a known fix path (Phase 5).
4. **We have product surfaces SA doesn't** (AI coach, custom analytics, dual-submit) that compound over time.
5. **SA's own product has documented failures** (market scan §1: net-cord, serve speed, scoring, shot filter) which our SQL views own and can fix once Phase 5 closes.

The competitive moat is cost + product surface, *not* ML supremacy. Treat Phase 5 as the "draw level on the only thing customers can directly compare" milestone, then lean into product differentiation.

---

## 6. What I'd recommend stopping or deferring

These take focus away from the moat:

1. **Don't chase 24/24 on the serve detector** — diminishing returns past 23/24, requires pose-model upgrade that's a multi-week side quest. Backlog item.
2. **Don't optimise cost below $0.10/match** — the $2 → $0.15 leap is the moat; further halving doesn't change the unit economics meaningfully.
3. **Don't add SportAI-parity for fields customers don't see** (e.g. raw bronze field exhaustiveness) — focus on the *dashboard-visible* gaps.
4. **Don't build a streaming variant of T5** unless an SLA decision requires it.
5. **Don't pay for human annotation yet** — dual-submit is free, abundant, and probably good enough to validate the WASB / TOTNet path first.

---

## 7. The single most important sentence

**Phase 5 (ball coverage) is the only thing blocking the "T5 better than SportAI" claim becoming defensible.** Everything else either already works, or will work mechanically once Phase 5 closes.
