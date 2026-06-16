# T5 ML Pipeline — North Star

**2026-06-16 (EOD) — ★ BRONZE DETERMINISTIC DEV COMPLETE. Every clean code fix is shipped; every remaining gap is TRAINING/data — the build-first/train-LAST endpoint is reached.** Three DEV items landed + validated on the reference pair (SA `079d2c62` ↔ T5 `375198f5`; recon tool `python -m ml_pipeline.diag.recon_line <t5> --sa <sa>`): **(1) far-pose serve path RETIRED** (`SERVE_FAR_POSE_ENABLED=0` in render.yaml) — serves 55→28 vs SA 24, recall held 18/24, precision 33%→60% (`c174df5`); **(2) ball_speed WIRED** (`stroke_events.ball_speed`, was 0/n in silver) — matched-shot median T5 ~83 vs SA ~90 km/h (`ffa4567`); **(3) pre-match warm-up EXCLUDED** — first-net-crossing-bounce cutoff in both detectors; warm-up serve/stroke FPs gone, real first serve (54.5s) + all 24 serves kept, ZERO real loss (`6576054`). Post-fix recon: serve 77% / volley 80% / swing 57% agree on matched shots; ball_hit_xy ~1.0m; T5 over-emit 1.67×→1.47×. **WHAT'S LEFT IS ALL TRAINING (no code fixes remain):** stroke WHEN/WHO recall (40% line-level), bounce recall (sharp-far retrain), swing-type accuracy, far-player position coverage, per-shot ball_speed (ball-tracker-limited). Corpus accrues + GPU train env built → this is the train-last phase. Full receipts: `.claude/audit_bronze_build_2026-06-16.md`.

> **Doc state:** the full pre-cleanup north_star (10 stacked dated banners + the rev-72 scorecard + the 2026-05 bronze-first/ADR-queue strategy blocks + the per-phase build history) is archived verbatim at `docs/_archive/north_star_2026-06-16_pre-dev-complete-cleanup.md`. This file is now the lean current truth. Current-state handover: `.claude/next_session_pickup.md`.

---

## ★ RULES OF THE GAME — read first, every session. ALL build happens in this vein.

Non-negotiable. A change that violates one of these is going backwards — stop and rethink.

1. **Bronze is the single source of truth. Silver inherits it 100% and does NO work.** Silver only *projects* bronze + adds analytics (score, serve location 1-8, zones, aggression, depth). If you catch silver *computing a base fact* (serve, swing, bounce, identity), that's a bug to fix — not to extend.
2. **One model per fact.** Pipeline: raw detectors (TrackNet / WASB / YOLOv8-pose / ViTPose) → **analysis models** (serve_detector, stroke_detector, …) → **bronze** normalised answers (`ml_analysis.*`) → silver projects. A fact is "done" only when a *model* emits it. A fact with no model is a **model to build/train** — tag it `STOPGAP-until-model-X`, never a silent silver heuristic. (Audit: `docs/_investigation/bronze_silver_18_audit.md`.)
3. **Build-first, train-LAST.** Build all 18 base fields to ~70-80% with the standard models *now*; train to 90-95% *later* — it's free + automatic via SportAI dual-submit. **Train selectively** (don't cap us at SportAI where our heavier models may be better). Pipeline-speed / throughput is a *training-stage* lever, never a reason to pause building.
4. **Measure-first, bench-green.** Validate against live data (`db_init.engine`) before committing. Keep the serve bench green (`ea1e500c 12/26, 880dff02 23/24` — re-baselined 2026-06-06 on rev-72 clean coordinates). No Batch push without the **BATCH-SIDE CHECKLIST** (`.claude/handover_t5.md`).
5. **Keep it clean — always.** Doc structure is fixed: **this file = True North** (rules + 18-field status + phase ladder); `.claude/next_session_pickup.md` = handover; `.claude/handover_t5.md` = ops / how-to-run; `docs/_investigation/*` = per-model references; everything historical → `_archive/`. **Don't create a new doc when an existing one fits.** Every session reads this file + the handover before touching code.
6. **Reconcile SA-active vs T5-active at the EVENT level; exclusion is a BRONZE base fact.** Never compare raw counts — *count alignment ≠ provenance* (`feedback_count_alignment_is_not_provenance`). Match each SA-active event to the nearest T5 event within ~1s, then score the ~12 base fields per pair (time, ball_speed, ball_hit_xy, player_xy, bounce_xy, stroke_type, serve T/F, volley T/F, A/B identity, rally membership). **Exclusion/validity ("is this event live play?") is one of those base facts → for T5 it lives in BRONZE** — a *generalizable* membership rule (`in_point`/`pre_match`/`between_points`, derived from serve+bounce+rally-state; NEVER a per-video constant like "before 55s"), and silver inherits it verbatim into `exclude_d`. **Silver does NO exclusion of its own for T5.** **Bronze = the ~18 base facts ONLY and never does silver's work; silver passes 2-5 (bounce-coord join, point/game/set numbering + score, zones/aggression/depth) are real analytics and STAY in silver.** The **SA (SportAI) prod silver exclude is customer-facing — DO NOT touch it** (migrate to bronze-membership only with a before/after "SA-active unchanged" check). **Triage every material gap** (Tomo reviews footage and names the event) into exactly one of: **(1) structural exclusion** [real, out-of-play → bronze membership rule, mirrors SA], **(2) detector fix** [deterministic, bench-guarded], **(3) training** [no rule separates right from wrong]. **Acid test:** an exclusion must make sense on a *clean* match — it can never be a knob to hide a false detection or make a count match. (Agreed Tomo 2026-06-16; `feedback_reconciliation_and_exclusion_methodology`.)

---

## ★ THE OVERARCHING GOAL — build the 18, THEN train (build-first, train-LAST)

The objective is an in-house pipeline whose **bronze** (`ml_analysis.*` → silver Pass 1) reproduces SportAI's **18 base facts materially.** Silver derives *everything else* (zones, aggression, serve location 1-8, rally analytics) off those same 18 — so **bronze-t5 ≈ bronze-sportai is the whole game.**

**Sequence — do NOT reorder:**
1. **BUILD** all 18 base fields to ~70-80% using the standard models (YOLOv8-pose, TrackNet/WASB, ViTPose). ✅ **DONE 2026-06-16** — every base fact is model-emitted; silver Pass-1 projects verbatim (no base-fact heuristics). Receipts: `.claude/audit_bronze_build_2026-06-16.md`.
2. **THEN TRAIN** to 90-95% — **FREE + automatic:** every production SportAI dual-submit accumulates a training pair, so we get hundreds of labelled games for nothing. ← **current + only remaining phase.** Training is LAST and self-funding; it runs incrementally as sharp-far full-res footage accrues. How-to: `.claude/training_environment.md` + `.claude/training_harness_status.md`.

> **⚠️ Training caution (Tomo, 2026-05-27):** SportAI-as-teacher **caps us at SportAI's accuracy** on any field where our (more detailed) models could exceed it — TrackNet/WASB + ViTPose are heavier than SportAI's "good-enough" pretrained stack. So **don't blindly fit to SportAI** on fields where we may already be as good or better (e.g. fine-grained pose-driven swing type, hit location). Train **selectively** — teacher-train the fields where SportAI is the clearly-better signal, manually verify / hold out the fields where we might be ahead. The free auto-corpus is for *volume*; what we train *on* is a deliberate per-field choice.

**The 18 base fields** = the Pass-1 projection in `build_silver_match_t5.py`: WHO hit (player_id/side), WHAT (serve, swing_type, volley), WHEN (ball_hit_s), WHERE-hit (ball_hit_location_x/y), WHERE-bounced (court_x/y), ball_speed, ball_player_distance, rally membership — plus the point/game/set structure passes 3-5 derive from them. Field-by-field truth: `docs/_investigation/bronze_silver_18_audit.md`.

**★ SILVER ROW ARCHITECTURE = HIT-DRIVEN. SETTLED 2026-06-14, LIVE 2026-06-14.** One silver row = one SHOT = one HIT. The bounce is an *outcome attribute* of the shot (where it landed), matched in as an UPDATE — NEVER the row key. Evidence: (1) every SA swing carries hit coords (100% of 2,380 swings across 6 matches); the bounce stream is multi-typed (floor/net/racket) at 1.12× swing count, not 1:1 with shots; (2) prod SA silver is ALREADY hit-driven — `build_silver_v2.py:357` inserts one row per `bronze.player_swing`, Pass-2 matches a bounce in as an UPDATE; (3) `build_silver_match_t5.py` Pass-1 now mirrors this. **LIVE:** `T5_STROKE_DRIVEN_SILVER` **defaults ON** (Tomo flipped early — T5 silver isn't prod-consumed; validated `78c32f53` pass1 110→174 rows clean). The bounce-driven path (`_t5_pass1_load_bounce_driven`) is **HELD as the `=0` rollback** until stroke-driven is re-proven on a fresh real upload. far-side WHO-attribution accuracy continues to improve via training (not an architecture gate). Memory `silver_must_be_hit_driven`.

**★ DEFINITION OF DONE — BUILD + ARCHITECTURE fully DONE; only TRAIN-LAST remains (verified 2026-06-16).** Every base fact emits from a model and silver projects it verbatim: serve, bounce (`ball_bounces`), hit (`stroke_events`), swing-type (`stroke_class`, PROVEN on `375198f5` — 257 rows), identity (A/B), ball_speed, volley. Architecture decided hit-driven ✅; `bench_hit` locked ✅ (NEAR 67% / FAR 19% / prec 54%); silver flipped hit-driven ✅. **REMAINING (all TRAIN-LAST / data):** sharp-far full-res footage accrues (DATA, Tomo) → retrain bounce CNN + hit model on the sharp distribution → far reaches the ~70-80% bar (measure via `bench_hit`/`bench_bounce`/`recon_line`). The whole build/architecture is in place; **the ball is in the data court.** Full checklist: `.claude/next_session_pickup.md`.

---

## ★ Current honest scorecard — reference pair, 2026-06-16 (SA `079d2c62` ↔ T5 `375198f5`)

First-ever clean current-code run on the canonical 10-min test match (`1781589562_match.mp4`; SA ≈ 24 serves / 68 floor bounces / 87 active swings). Measured with `recon_line` (event-level, per RULE 6).

| element | state | verdict |
|---|---|---|
| serve count | 28 vs SA 24 | ✅ DEV done (far recall = train) |
| serve agree (matched) | 77% | — |
| volley agree | 80% | bounce-recall-gated (TRAIN) |
| swing_type agree | 57% | TRAIN (classifier accuracy) |
| ball_hit_xy | ~1.0m median | OK |
| ball_speed | wired; matched median T5 ~83 vs SA ~90 | ✅ DEV done; per-shot ±40 = ball-tracker-limited (TRAIN) |
| identity A/B | clean (0% pollution) | ✅ done |
| near player position | −0.42m | ✅ done |
| stroke WHEN/WHO recall | 40% line-level (35/87) | ❌ TRAIN (the big one — stroke detector) |
| bounce recall | 28 vs 68 floor | ❌ TRAIN (sharp-far retrain) |
| far player position | ~absent | ❌ TRAIN / coverage |

**No code fix remains for any ❌** — all are training/data. That is the DEV-done line. The per-fact derivation history (rev-72 scorecard, far-court ceiling analysis, the bronze-first freeze) is archived in `docs/_archive/north_star_2026-06-16_pre-dev-complete-cleanup.md`.

---

## Product goal

A tennis match analytics dashboard where:

1. Every event shown corresponds to a real point-stroke (no pre-serve racquet-bouncing, no between-points walking).
2. Every coordinate is geometrically accurate within the dashboard's tolerance: hitter location, bounce location, ball trajectory.
3. Per-stroke claims (forehand winner, backhand depth, attack/defence, serve-side) are correct on validated points.
4. The user can trust per-rally and per-set aggregates because the underlying events are clean.

We are NOT chasing 100% serve detection. We're getting the dashboard data trustworthy enough that a coach would draw the same conclusions as if they'd watched the match.

---

## Phase ladder — BUILD COMPLETE 2026-06-16

> The phases below are the historical build ladder. **The build is COMPLETE** (see top banner); this table is retained for orientation. Full per-phase history (what shipped, key learnings, the reverted attempts) lives in `docs/_archive/north_star_2026-06-16_pre-dev-complete-cleanup.md`. The active phase is now **TRAINING** (incremental, off the live dual-submit corpus).

| # | Phase | Status |
|---|---|---|
| 0 | Doc cleanup + this file | DONE |
| 1 | Bounce-validity rule | DONE (`880dff02` 23/24, 10/10 FAR) |
| 2 | Point boundary detection | Folded into silver pass-3 SQL (point/game/set numbering) |
| 3 | Pre-/between-point filter | DONE — warm-up exclusion is now a bronze membership fact (RULE 6, `6576054`) |
| 4 | Point-completeness reconciler | DONE — superseded by `recon_line` (event-level) |
| 5 | Ball detection coverage | DONE — WASB shipped (52%+ coverage); far-ball ROI deployed (candidate recall 40%→87%) |
| 6 | Stroke detection | DONE — `stroke_detector/` shipped; silver hit-driven (`T5_STROKE_DRIVEN_SILVER` default ON); recall accuracy = TRAIN |
| 7 | Bounce accuracy | BUILD DONE — CNN bounce model + precision lever shipped; recall = TRAIN (sharp-far retrain) |
| 8 | Final serve cleanup | DONE — far-pose retired; serve over-emission resolved architecturally |

**Detector build queue (ADR-01…05):** all shipped — bounce CNN (ADR-01), swing-type 4-class (ADR-02, proven), identity (ADR-03, 100% bench), volley analytic (ADR-04), sequencing (ADR-05). ADR references frozen in `docs/_investigation/adr_0*.md`.

---

## How to run / query (quick reference)

- **Serve bench** (mandatory pre-push on any detector edit; floor ea1e500c 12/26, 880dff02 23/24): `.venv/Scripts/python -m ml_pipeline.diag.bench`
- **Reconcile a dual-submit pair** (event-level SA-active vs T5-active, RULE 6): `python -m ml_pipeline.diag.recon_line <t5_tid> --sa <sa_tid>`
- **Per-fact training gates:** `bench_hit`, `bench_bounce`, `bench_identity`, `bench_swing_type` (map: `.claude/training_harness_status.md`).
- **Query the prod DB directly** (this dev box's IP is allowlisted): `from db_init import engine; engine.connect()`.
- **Rebuild a T5 silver match**: `from ml_pipeline.build_silver_match_t5 import build_silver_match_t5; build_silver_match_t5('<task_id>', replace=True)`.
- **Batch deploy** (after a Batch-side change — see the file-list trigger in `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST"): Docker rebuild → dual-region ECR push → new job-def revisions in **eu-north-1 + us-east-1**.
- **Train a fact** (GPU Batch one-off): `submit_train_job.py --fact {serve|hit|bounce|swing}` (job-def rev 3). Runbook: `.claude/training_environment.md`.
- **Full ops/how-to-run reference**: `.claude/handover_t5.md`.

---

## Operating rules

1. **No detector edit without `bench` green first.** Hard rule from CLAUDE.md.
2. **No T5 detector branch merges without the Batch-side change check.** Non-empty diff against `ml_pipeline/roi_extractors/`, `__main__.py`, `pipeline.py`, `Dockerfile`, `requirements.txt`, `serve_detector/`, etc. → Docker rebuild + dual-region ECR push + new job-def revisions. See `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST".
3. **Don't ship code that depends on SA truth at runtime.** The reconciler/recon_line are diag tools; production has no SA counterpart.
4. **Phase/status changes update this file.** Keep it lean — historical detail goes to `_archive/`, not stacked banners here.

---

## How to update this file

- **Status change (phase ships/parks, new bottleneck):** edit in place; keep it lean. Move superseded strategy/scorecard blocks to `docs/_archive/` rather than stacking dated banners.
- **Major restructuring:** copy current file to `docs/_archive/north_star_YYYY-MM-DD_<context>.md` first, then rewrite. Don't lose history.
- **Bench baseline shifts:** mention here (not just in the `bench_baseline.json` commit message).
