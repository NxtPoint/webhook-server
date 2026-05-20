# T5 Infrastructure Audit & Roadmap (2026-05-20)

**Audience:** Tomo + future Claude sessions.
**Purpose:** Catalogue what infra exists, what's missing, and what to build next — prioritised by leverage. The session brief framed this as Stream D of the 2026-05-20 infrastructure session.
**TL;DR:** The detection-side tooling is **far more mature than I initially expected** (huge harness, 11 active + 19 archived diag tools, bench CI, snapshot/replay fixtures, golden snapshots). The real gaps are: (a) **no GPU dev box** (Stream A fixes today); (b) **no per-component bench beyond serve detector**; (c) **no model versioning / experiment tracking** so we can't reason about which finetune is best; (d) **no production T5 quality monitoring**; (e) **WASB weights are downloaded but not wired in**. Each is a 1-session-to-1-week build. Total backlog: **5-7 working sessions** to close the high-leverage gaps. The single highest-leverage build is **per-component fixture bench harness** (item 4 below) — it would convert silver-builder and ball-tracker iteration speed from "hours per Batch round-trip" to "seconds per change".

---

## 1. Compute

### What exists

| Layer | Platform | Spec | Status |
|---|---|---|---|
| Production detection (court/ball/player) | AWS Batch GPU | g4dn.xlarge, Spot (eu-north-1 primary, us-east-1 failover); on-demand CE order 1 during testing campaigns | **Live**, retry strategy pinned (3 attempts, auto-retry on Spot eviction). Quota: zero on-demand G-family vCPU last confirmed 2026-04-15 — manual cross-region failover playbook at `.claude/playbook_aws_batch_ondemand_fallback.md`. |
| Production serve detection + silver build | Render | CPU only | **Live** (`upload_app.py::_do_ingest_t5`). ~10s rebuild. |
| Production API + workers | Render | Standard CPU services | **Live** (see CLAUDE.md service table). |
| Dev environment | Windows 11 / local | Tomo's laptop, no GPU | **Bottleneck**. Local bench is fine for serve_detector (sub-second), but ball-tracker / training needs GPU. |
| Training infrastructure | — | — | **Missing.** No dedicated training compute. `train_tracknet.py` exists but only runs locally (CPU) or implicitly on Batch (no Batch job-def for training). |
| GPU dev box | — | — | **Being added today** in Stream A (g4dn.xlarge eu-north-1). |

### Gaps & priorities

| Gap | Verdict | Size |
|---|---|---|
| **GPU dev box** for interactive ball-tracker work + ad-hoc training | **MUST** | Stream A (1-2 hr) |
| Training-specific Batch job-def + queue | Nice-to-have once GPU box is comfortable | 1 session — clone main job-def, override entry to `python -m ml_pipeline.training.train_tracknet`, attach S3 read+write |
| On-demand quota raise (G family) | Nice-to-have (eliminates Spot eviction risk for training) | 1 hr — submit Service Quota request via AWS console; turnaround is variable |

---

## 2. Storage

### What exists

| Asset | Where | Format | Versioning |
|---|---|---|---|
| Raw videos | `s3://nextpoint-prod-uploads/wix-uploads/<file>.mp4` | mp4 | None (filename includes timestamp) |
| Trimmed highlight videos | `s3://nextpoint-prod-uploads/trimmed/{task_id}/review.mp4` | mp4 | None |
| Production tables | Render Postgres | SQL | Schema-versioned via idempotent `ensure_*` functions (see CLAUDE.md "Schema DDL") |
| Model weights | `ml_pipeline/models/*` (git-ignored, baked into Docker at build) | `.pt` / `.pth` | None — `tracknet_v2.pt` overwrites itself between trainings; the current `tracknet_v2_finetuned.pt` has no manifest |
| Training labels | `ml_pipeline/training/labels/*.json` (local) | JSON | None |
| Training datasets | `ml_pipeline/training/datasets/*` (local) | JPEGs + JSON | None — `match_90ad59a8` and `match_90ad59a8_v2` exist side-by-side with no manifest |
| Fixtures (bench) | `ml_pipeline/fixtures_ci/*.pkl.gz` (in git) | pickled | **Committed** — `a798eff0.pkl.gz` is the CI fixture |
| Local fixtures | `ml_pipeline/fixtures/*.pkl.gz` (git-ignored, S3 backup at `s3://nextpoint-prod-uploads/fixtures/`) | pickled | Local + S3, regen from DB via `snapshot_task.py` |

### Gaps & priorities

| Gap | Verdict | Size |
|---|---|---|
| **Model weights versioning** — `tracknet_v2_finetuned.pt` exists locally but with no manifest of training corpus, hyperparams, val metrics | **MUST** if Phase 5c.4 (promotion gate) is going to work | 1 session — covered as G8 in `dual_submit_status_2026-05-20.md` |
| **Training corpus index** — `ml_analysis.training_corpus` table | **MUST** for Phase 5c | 1 session — covered as G5 in `dual_submit_status_2026-05-20.md` |
| **WASB integration** — weights are already in `ml_pipeline/models/wasb_tennis_best.pth.tar` (downloaded but **not wired into the pipeline**) | **MUST** — market scan §2 calls this the cheapest +9pp F1 win available | 1-2 sessions — write `ml_pipeline/wasb_tracker.py`, A/B vs current ball_tracker, decide whether to switch backbone |
| S3-backed training labels + datasets | Nice-to-have until training-data volume grows past local laptop disk | Covered by G4 in `dual_submit_status_2026-05-20.md` |
| Schema migration framework | Can-skip — idempotent `ensure_*` pattern works for our scale | — |

---

## 3. Data

Dual-submit covered exhaustively in `dual_submit_status_2026-05-20.md`. Highlights here for completeness:

| Asset | Status |
|---|---|
| Dual-submit code path | **Built but disabled** (`AUTO_DUAL_SUBMIT_T5=0` in prod) |
| Manual retro endpoint | **Built** (`POST /ops/dual-submit-t5`) |
| Label exporters (ball + serve-bounces + stroke) | **Built** — 3 scripts in `ml_pipeline/training/` + 1 in `ml_pipeline/stroke_classifier/` |
| Labeled corpus | **1 match** (`8a5e0b5e`); need ≥10 for V3 retrain |
| Pair-completion signal | **Missing** (Phase 5c.2 work) |
| Corpus index table | **Missing** (Phase 5c.2 work) |
| Validation-set curation | **Missing** — no explicit hold-out matches identified for catastrophic-forgetting checks |
| Per-match metadata | **Partial** — `bronze.submission_context` holds upload metadata (`player_a_name`, `player_b_name`, `sport_type`, `email`); no per-match camera-angle or "diverse coverage" tags |

### Annotation alternatives (from market scan §4)

| Path | Cost for 10 matches | Notes |
|---|---|---|
| Dual-submit (SportAI as teacher) | ~$1.50 in Batch compute (already paying SportAI credits) | **Default path.** Quality limited by SportAI's failures (phantom bounces, miscounted strokes). |
| Roboflow Outsource | ~$7,500 (keypoint) or ~$15,000 (bbox) | Only worth it for **occluded-frame labels** which dual-submit can't produce. |
| CVAT self-hosted + manual | Tool free + 250-500 hrs labor | Reserved for the diverse-cameras validation set, not bulk corpus. |

---

## 4. Testing / QA

### What exists — much more than I initially thought

| Tool | Scope | Where |
|---|---|---|
| **`bench.py`** | Serve detector regression — runs `replay_serves` across all fixtures vs `bench_baseline.json` | `ml_pipeline/diag/bench.py`. **Sub-second.** |
| **`bench_baseline.json`** | Per-fixture MATCH count target | `ml_pipeline/diag/bench_baseline.json` (current: a798eff0=20/24, 880dff02=23/24) |
| **CI workflow** | `bench.py` on every push to `main` + every PR touching `serve_detector/` or `build_silver_v2.py` | `.github/workflows/bench.yml` — only workflow that exists |
| **`snapshot_task.py`** | DB → pickle.gz fixture | One-time per task; deterministic regen |
| **`replay_serves.py`** | Fixture → run prod `_run_pipeline` → reconcile | Sub-second offline |
| **`audit_all_serves.py`** | Per-SA-serve verdict + prod-kill tracer (BUCKET A/B/C classifier) | Diag CLI |
| **`reconcile_serves_strict.py`** | SA-vs-T5 reconcile, strict ±0.5s, opposite-side bounce check | Diag CLI |
| **`audit_points_reconcile.py`** | Per-point reconciler (baseline 0/17 — ball-coverage-limited) | Diag CLI + baseline file |
| **`harness.py`** subcommands | `eval-ball`, `eval-player`, `eval-court`, `eval-serve` (with prec/recall targets) | Comprehensive CLI |
| **`golden-snapshot` / `golden-check`** in harness | Component-level regression detection vs known-good baselines | Built but lightly used |
| **`probe_baseline_empty.py`**, **`probe_roi_coverage.py`**, **`inspect_pose_window.py`**, **`inspect_cluster_topology.py`** | Targeted diagnostic probes | All in `diag/` — 11 active tools, 19 archived |

### Gaps & priorities

| Gap | Verdict | Size | Why this matters |
|---|---|---|---|
| **Per-component fixture bench** — silver builder changes can't be tested without a full Batch round | **MUST — HIGHEST LEVERAGE** | 1 session | Today: silver-builder iteration is "edit → Render push → ask Tomo to rerun-silver → wait → query DB". With cached bronze fixtures (already exist via `snapshot_task`), a silver-builder bench would let `python -m ml_pipeline.diag.bench_silver` run all known-good silver against `silver_baseline.json` in seconds. **Phase 3 part 2 was reverted twice; this harness would have caught it locally.** |
| **Per-component bench for ball tracker** — bench `ball_tracker.py` against cached frame stacks | **MUST** for WASB integration | 1 session | Currently can't measure "would WASB beat TrackNetV2?" without full Batch run. Once cached, every `ball_tracker.py` change runs in seconds. |
| **A/B framework for ball detector swap** — measure WASB vs TrackNetV2 on same fixture | Tied to above | Covered by the per-component bench |
| **Validation set diversity** — 2-3 hold-out matches with different camera angles | Nice-to-have until WASB / finetune lands | 2 hrs — identify, run dual-submit on each |
| **End-to-end "ship a Batch change" smoke test** | Can-skip — manual BATCH-SIDE CHANGE CHECKLIST is sufficient at our cadence | — |

---

## 5. Visualization

### What exists

| Tool | Output |
|---|---|
| `inspect_cluster_topology.py` | Cluster structure dump around one timestamp (text) |
| `inspect_pose_window.py` | Per-frame pose profiler with verdict bucketing (text) |
| `probe_baseline_empty.py` | Why a window has 0 baseline rows (text) |
| `probe_roi_coverage.py` | Task-wide + per-window ROI coverage probe (text) |
| `audit_all_serves.py` | Per-serve gate matrix + prod-kill tracer (text) |
| `serve_viewer.py` (archived) | Visual contact sheets — `ml_pipeline/diag/_archive/serve_viewer.py` was the visual tool; archived |
| `visualize_far_serve.py` (archived) | Visual far-serve viewer |
| Dashboards (`practice.html`, `match_analysis.html`) | ECharts visualisations of silver data — what coaches actually see |

### Gaps & priorities

| Gap | Verdict | Size |
|---|---|---|
| **T5 vs SportAI side-by-side overlay** (frame viewer that shows where they agree/disagree) | **SHOULD** — fastest way to diagnose silver-builder issues | 1 session — ECharts + presigned video URL + overlay frame ranges from both silvers |
| **Per-frame debug viewer** with ball detections + pose keypoints overlaid on video | **MUST** for WASB A/B | 1 session — extend `serve_viewer.py` (move out of `_archive`), point at `ml_analysis.ball_detections` |
| **Model quality dashboard across matches** — show ball coverage %, per-point reconcile, bench MATCH count over time | **SHOULD** for production T5 monitoring | 1-2 sessions — gold view `gold.t5_quality_rollup` + add a tab to `backoffice.html` (admin-only); reuses dashboard CSS |
| **W&B / MLflow experiment tracking** for training runs | **MUST** once training cadence picks up | 1 session — W&B free tier; instrument `train_tracknet.py` and `train_stroke.py` |
| Real-time inference latency dashboard | Can-skip — we're not latency-sensitive (Batch is async) | — |

---

## 6. Iteration speed (the meta-metric)

This is the leverage measure. Today's reality:

| Layer | "Code change → validated result" today | Target |
|---|---|---|
| Serve detector | **Sub-second** via `bench.py` + fixture | **Achieved** ✓ |
| Silver builder | ~5-10 min (push → wait for Render auto-deploy → `harness rerun-silver` on Render shell → query DB) | **Target: <30s** via per-component bench (item 4 above) |
| Ball tracker | ~30-90 min (push → Docker rebuild → ECR push → job-def revision → user reruns Batch) | **Target: <1 min** via cached-frame-stack bench |
| Pose extractor (ROI) | ~30-90 min (same Batch round-trip) | **Target: <2 min** — locally on GPU box (Stream A) with `replay_roi_pose.py` |
| Training (TrackNet) | Untested at scale — local CPU is hours per epoch | **Target: <1 hr/epoch** on g4dn.xlarge |
| Stroke classifier training | Same | Same |

**Single biggest leverage move:** the silver-builder bench. Phase 3 part 2 was reverted twice in May; both reverts would have been local-bench reds. The build is ~1 session and replaces hours-per-change with seconds-per-change for every silver edit downstream.

---

## 7. Observability

### What exists

| Signal | Surface |
|---|---|
| Bench CI status on every push | GitHub Actions (`.github/workflows/bench.yml`) — visible on PR + push |
| Batch job CloudWatch logs | `awslogs-group=/aws/batch/ten-fifty5-ml-pipeline` |
| `BallTracker.log_diagnostics()` | Logs per-tier breakdown at end of every Batch job (Phase 5b characterisation reads this) |
| Render app logs | Render dashboard |
| DB ops endpoint | `POST /ops/diag/sql` for read-only SELECT runner (Tier 2 autonomy) |
| Frontend "what completed" view | `backoffice.html` |

### Gaps & priorities

| Gap | Verdict | Size |
|---|---|---|
| **Production T5 quality monitoring** — alert when a customer's T5 match has <X% ball coverage or 0 active silver rows | **SHOULD** before opening T5 to real customers | 1-2 sessions — gold view + scheduled job (or just dashboard) |
| **Bench history tracking** — track bench MATCH count over time, surface regressions visually | **SHOULD** — currently the only record is `bench_baseline.json` git history | 1 session — append every bench run to `ml_pipeline/eval_history.jsonl` (exists already!) + render as a chart |
| **Drift detection on dual-submit divergence** — when T5 vs SA reconcile drops across multiple matches in a week, alert | Nice-to-have | 1 session (post-Phase 5c) |
| Customer-quality SLA telemetry | Can-skip until we have a SLA |

---

## 8. Prioritised gap roadmap (the punch list)

Sorted by leverage × ease, descending. **Top 4 items would more than double iteration speed.**

| # | Item | Size | Leverage | Stream |
|---|---|---|---|---|
| 1 | **Provision GPU dev box** (g4dn.xlarge eu-north-1, IAM, SG, instance profile, runbook) | 1-2 hr | Unblocks everything ML | **Stream A (today)** |
| 2 | **Per-component bench: silver builder** — `python -m ml_pipeline.diag.bench_silver` against cached bronze fixtures + silver baseline | 1 session | Converts silver-builder iteration from hours to seconds; would have caught both Phase 3 part 2 reverts | Stream F candidate |
| 3 | **WASB integration** — `wasb_tracker.py`, A/B vs TrackNetV2 on cached frame stack | 1-2 sessions | Market scan says **+9pp F1** for almost zero training cost; the weights are *already in `ml_pipeline/models/`* | Phase 5 (post Stream A) |
| 4 | **Per-component bench: ball tracker** — bench `ball_tracker.py` against cached frame stacks; gates WASB swap | 1 session | Required for #3 to be safe to ship | Phase 5 (post Stream A) |
| 5 | **Dual-submit Phase 5c.0+5c.1** — flip `AUTO_DUAL_SUBMIT_T5=1`, run retro backfill | 2.5 hr | Doubles training-data inflow from $0 incremental cost | Phase 5c (see Stream E) |
| 6 | **Pair-completion hook + corpus index** — Phase 5c.2 | 1 session | Removes manual labeling step from every new pair | Phase 5c |
| 7 | **T5 vs SA frame overlay viewer** — visual diff tool | 1 session | Replaces "stare at text logs" with "see the disagreement" | Stream F candidate |
| 8 | **W&B integration in `train_tracknet.py`** + S3-backed weights with manifest | 1 session | Required before serious training cadence | Phase 5c.4 |
| 9 | **Production T5 quality dashboard** — gold view + backoffice tab | 1-2 sessions | Required before real customer T5 traffic | Phase 6+ |
| 10 | **Bench history chart** — append `eval_history.jsonl` rendered as a line chart in backoffice | 1 session | Removes "did we get better or worse over time?" guesswork | Phase 6+ |
| 11 | Validation-set diversity — identify 2-3 hold-out matches | 2 hr | Required for catastrophic-forgetting checks during finetune | Phase 5c.4 |
| 12 | Training-specific Batch job-def | 1 session | Only matters if GPU box constraint becomes painful | Backlog |
| 13 | On-demand G-vCPU quota raise | 1 hr | Only matters if Spot evictions become painful during training campaigns | Backlog |
| 14 | Drift detection on dual-submit divergence | 1 session | Only matters once dual-submit is producing dozens of pairs/week | Phase 6+ |
| 15 | Schema migration framework | — | Current idempotent pattern works | **Can-skip** |
| 16 | Real-time inference latency monitoring | — | Async pipeline, not latency-sensitive | **Can-skip** |

---

## 9. Recommended order of operations

The honest read: **before any more model work, build #2 (silver bench) and #4 (ball-tracker bench)**. They convert the dominant cost (iteration time) from hours to seconds for everything downstream. Then everything else gets ~10× cheaper.

**Stream F (bonus tool) should be #2 (silver-builder bench)** if there's time today. Cost-benefit: one session of build, then every subsequent silver edit is testable in <30s vs the current 5-10 min round-trip — payback in ~10 silver iterations.

**Alternative Stream F:** #7 (T5 vs SA frame overlay) — easier to demo, more "screenshot-able", less infrastructural. Both are good picks; silver bench is higher leverage but less immediately visible to Tomo.

The full session plan after today would look like:

| Session | Focus | Items |
|---|---|---|
| Today | Infra + scoping + GPU box | Streams A, B, C, D, E (+ optional F) |
| +1 | Silver-builder bench harness | Item #2 |
| +2 | WASB integration + ball-tracker bench | Items #3, #4 |
| +3 | Dual-submit on + retro + corpus index | Items #5, #6 |
| +4 | First real training run on GPU box | Item #8 (with assembled corpus) |
| +5 | T5 quality dashboard + bench history | Items #9, #10 |

That's **5 working sessions to a fully unblocked Phase 5** with iteration speed measured in seconds, weights versioned, and a production quality monitor live.
