# Next-session pickup — 2026-06-15 (overnight) — ✅ FIRST FULL TRAIN DONE: 2 sharp-far matches landed, all 4 models retrained on GPU + measured. Deploy PENDING (daylight). main @ `788aaf9`.

> **Resume (READ THIS):** Overnight, 2 sharp-far matches (`fe7e6805/93ebb93d` + `ac9733b6/7d3e2392`) landed end-to-end and **all 4 models retrained on GPU** via `submit_train_job`. **Nothing is deployed to prod** — new weights sit in `s3://…/training/weights/<fact>/_latest/`; deploying = detection-image rebuild (Batch-side, daylight + Tomo). **Results (vs locked baselines): HIT clear win** (matched +256, near_gate +134, precision 54%→**68%**, far +17); **SWING 4-class trained** (macro-F1 **0.747**, NEAR 0.825, `other` F1 0.59 — works); **BOUNCE recall up** (18.2%→20.6%) **but precision down at thr 0.70** (14.4%) → needs a threshold re-tune before deploy; **SERVE** retrained (heldout F1 0.47). **FAR gate barely moved (predicted — 2 matches is thin for far)** → more sharp-far videos needed. See the OVERNIGHT block below.

## ⚡ OVERNIGHT 2026-06-15 — first train results + what's next
**What happened:** Tomo authorized overnight train. Re-ran 2 clean sharp-far matches → corpus now 10 (8 coarse + 2 sharp). Trained serve/hit/bounce/swing on GPU. Identity = rule-based (no train).
**Regression caught + fixed live:** the stroke-driven silver path never called `_apply_serve_events_overlay` (since `472b244` flipped it default-ON) → fresh T5 silver had 0 serves/points/games. **Fixed (`25c9a07`); video 2 confirmed it works in the DEPLOYED pipeline** (silver 544 rows, 144 serves, no manual rebuild). Without this every re-run video built broken silver.
**Training-image bug caught + fixed:** swing first attempt FAILED (`KeyError 'other'`) — `Dockerfile.train` inherited a STALE 3-class `stroke_classifier/` from the base detection image. Fixed (`788aaf9`, COPY fresh `stroke_classifier/`), job-def **rev 2**, swing retrained OK.
**Per-model results (apples-to-apples vs locked benches; weights in S3, NOT deployed):**
- **HIT** `bench_hit` new weights: near_gate 667→**801** (+134), far_gate 245→**262** (+17), matched_any 1488→**1744** (+256), precision **54%→68%**. No regression. → strong, deploy-worthy (daylight).
- **SWING** 4-class: macro-F1 **0.747** (oh 0.88 / fh 0.77 / bh 0.75 / **other 0.59**), NEAR macro-F1 0.825. First real 4-class weights; lock `bench_baseline_swing_type.json` next (bench needs working torchvision = run in the training image, not the broken local venv).
- **BOUNCE** new weights: recall 18.2%→**20.6%** (+89 matched) but precision **23.3%→14.4% at thr 0.70** → the retrain shifted the score distribution; **re-sweep the threshold (0.70 too low now) BEFORE deploy.** Don't deploy at 0.70.
- **SERVE** retrained heldout F1 0.47 / rec 0.45 / prec 0.50.
**The honest read:** NEAR + precision improved a lot; **FAR attribution barely moved (hit far +17 only)** — exactly the predicted "2 sharp matches is thin for the far gate." **More sharp-far videos = more far lift.** (Tomo has 2 more, holding them pending an SA-tagging sanity check.)
**NEXT (daylight):** (1) **bounce threshold re-sweep** on the new weights (find where precision recovers — 0.90 emitted too few; optimum ~0.75-0.85). (2) **Deploy decision** per model — rebuild detection image with new weights (Batch-side, rule #8) once happy. (3) **More sharp-far videos** for the far gate (vet the 2 uncertain ones first: send task_ids, sanity-check SA tagging). (4) lock the swing bench. New weights at `s3://nextpoint-prod-uploads/training/weights/<fact>/_latest/` (+ meta.json); pull with `submit_train_job --fact <f> --download` (⚠️ NOT bounce until re-tuned). Training-image job-def = `ten-fifty5-ml-train:2`.

---

> **Resume (prior milestone):** the project crossed from "per-fact building" into "training seamlessly." Built the SWING model to 4-class, stood up the **GPU training environment** (AWS Batch one-off jobs), locked an enforceable bench for every fact. Read `.claude/training_environment.md` (how to train) + `.claude/training_harness_status.md` (the 5-fact gate map).

## ⚡ Executive summary (read first — 30 seconds)
**Phase:** bronze-first; build essentially complete across the facts; the lever now is the **seamless GPU training push** (build-first → train-last, train-last is finally a one-command operation).
**main @ `89d63ac`.** Batch detection image unchanged (eu rev 79 / us rev 60) — no detector deploy this session.
**Bench floor:** serve `ea1e500c=12/26` + `880dff02=23/24` green (CI). **All 5 facts now have an enforceable committed gate** (hit, bounce, identity locked + exit-1-on-regression this session; serve CI; swing pending its first 4-class train).
**What shipped this session:** (1) **SWING 4-class build** — ADR-02 revised `{fh,bh,overhead,other}`, volley split to its own boolean fact, silver swing heuristics DELETED, 4-class dataset rebuilt (`swing_type_v3_4class`, 1757 hits); (2) **GPU training environment BUILT + PROVEN** — image + job-def `ten-fifty5-ml-train:1` registered, `--fact bounce` GPU smoke SUCCEEDED end-to-end (val F1 0.47, weights→S3); one command per fact via `submit_train_job.py --fact <name>`; (3) **training benches** — bounce+identity baselines LOCKED, all 3 made enforceable; (4) **bounce→silver carry** — silver inherits bounce coords from the MODEL (`ball_bounces`) verbatim, `is_bounce` fallback for old tasks (`T5_BOUNCE_FROM_MODEL`).
**What's blocked / the real gate:** far-side accuracy (hit FAR 19%, bounce recall, swing far) is TRAIN-LAST on **sharp-far** data — and the existing corpus is all **coarse-far** (pre-rev-78). So the high-value next move is re-running the original videos through rev-79 to regenerate a sharp-far corpus. (Local CPU can't train — torch broken for torchvision — but that's moot now: training runs on GPU via the proven env.)
**Next session's job:** the one-time setup is DONE. So: (1) **re-run the original full-res videos** (Tomo has them) through the current pipeline → fresh **sharp-far** corpus (~2h GPU each — parallelize across the queue); verify the new corpus, then retire the old coarse rows; (2) **train all 4 on GPU** (`submit_train_job --fact serve|hit|bounce|swing`), `--download` weights, lock `bench_baseline_swing_type.json` (swing's last gate); (3) measure far-side lift with the benches. Player-v2 (CNN re-id) only after, and only if identity actually fails in practice.

---

## The 5 facts — current state (one table to rule them all)

| Fact | Build | Bench (enforceable gate) | Train |
|---|---|---|---|
| **Serve** | ✅ signed off | `bench.py` 12/26 + 23/24 — CI-gated | `submit_train_job --fact serve` (coord MLP) |
| **Identity** | ✅ rule-based v1 | `bench_identity` 100% (n=14) — LOCKED this session | no trainer (rule v1); OSNet v2 future |
| **Hit** | ✅ build done | `bench_hit` NEAR 67%/FAR 19%/prec 54% — enforced | `submit_train_job --fact hit` (coord MLP) |
| **Bounce** | ✅ build done | `bench_bounce` rec 18%/prec 23% — LOCKED this session (⚠️ config-sensitive, see status doc) | `submit_train_job --fact bounce` |
| **Swing** | ✅ **4-class build done** (code+dataset); weights pending GPU train | `bench_swing_type` — baseline NOT yet locked (needs the 4-class train) | `submit_train_job --fact swing` (R(2+1)D, GPU) |

**Accuracy on all of these is train-LAST** and the far-side facts (hit FAR 19%, bounce recall) are gated on Tomo's **full-res uploads** accruing sharp-far corpus (north_star DoD #7/#8). The benches measure the floor; only the retrain moves it. Corpus auto-accrual is ON (both `AUTO_DUAL_SUBMIT_T5` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS` = 1, confirmed by Tomo) — the 2026-06-04 stall is just no uploads since.

---

## How training works now (the seamless path — `.claude/training_environment.md` is canonical)

Decision: **AWS Batch GPU one-off jobs** on the existing detection compute envs (scale-to-0, no idle cost; Batch reaches the prod corpus DB + S3). Training runs in `ml_pipeline/Dockerfile.train` (FROM the detection image → inherits CUDA torch 2.3.1+cu121, the working torchvision the local CPU box lacks).

**One-time setup (needs Docker running — was down this session):** build/push `ten-fifty5-ml-train` image + `submit_train_job.py --register-jobdef`. Steps in `.claude/training_environment.md` §ONE-TIME SETUP.

**Then, per fact:** `.venv/Scripts/python -m ml_pipeline.training.submit_train_job --fact <serve|hit|bounce|swing>` → trains on GPU → uploads weights to `s3://nextpoint-prod-uploads/training/weights/<fact>/`. `--download` to sync to `models/`, `--status` to watch.

**Swing specifics:** the 4-class dataset is already built locally (`ml_pipeline/training/datasets/swing_type_v3_4class`, 1757 hits incl. 169 `other`). The swing job can rebuild it in-image, or pre-bake + `--skip-dataset`. After training: run `bench_swing_type --dataset-dir .../swing_type_v3_4class` and `--bless` to lock the (currently missing) `bench_baseline_swing_type.json` — THE swing gate.

---

## Next steps (priority order)
1. **One-time training-image setup** (needs Docker): build/push `ten-fifty5-ml-train`, register the job-def (`submit_train_job --register-jobdef`). `.claude/training_environment.md` §ONE-TIME SETUP.
2. **Cheap GPU smoke** — `submit_train_job --fact bounce` (~3 min) to confirm train → S3 on real GPU.
3. **Swing 4-class train** — `submit_train_job --fact swing` → `--download` → `bench_swing_type --bless` to LOCK the swing gate (completes the swing build: it's the last unlocked bench). Gate = classifier macro-F1 standalone (no heuristic crutch — the heuristics are deleted).
4. **Train-all-5 accuracy push** — once Tomo has uploaded new **full-res** SA matches (corpus accrues sharp-far automatically), retrain hit + bounce (+ swing) on the sharp-far distribution; measure with `bench_hit`/`bench_bounce`/`bench_swing_type`. This is the DoD #7→#8 close.
5. **Then:** volley (the split-out boolean fact) — derive (fh/bh/overhead before bounce crosses net) + validate vs SA's 96 `player_swing.volley` labels + its own bench. Then ADR-04 volley analytic.

## Residuals / watch-items
- **bench_bounce is config-sensitive** — a bare run uses STOPGAP random weights → emits 0 → FALSE-regress. Reproduce only with `BOUNCE_CANDIDATE_MODE=gravity_residual` + `--weights-path models/bounce_detector_v2_7match.pt` + `--threshold 0.70`. Documented in the bench docstring + status doc. Not CI-wired (rule #9).
- **Swing is Batch-side** (`stroke_classifier/` in the detection image, `SWING_CLASSIFIER_ENABLED=0`). The 4-class code is committed but NOT deployed — enabling it in prod is a rule-#8 Batch rebuild cycle (daylight + supervised), to be done after the train-to-ceiling, not before.
- **Identity has no trainer** (rule v1) — its gate guards the rule only; v2 CNN re-id needs a player-crop `label_kind` that doesn't exist yet.
- `train_swing_type.py` docstring says "NOT runnable today" (stale — referred to the old 368-hit corpus; now 2301 labels + 4-class). Cosmetic; not a blocker.

## Memory entries this session
- (none new — the patterns are captured in ADR-02 + the two `.claude/training_*.md` docs.) Consider a `feedback_*` on "broken local torch → train in the Batch image, not the dev venv" if it recurs.

---
**END OF PICKUP**
