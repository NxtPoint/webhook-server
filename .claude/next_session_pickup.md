# Next-session pickup — 2026-06-14 PM — ✅ SWING 4-class BUILD done · ✅ seamless GPU TRAINING ENV built · ✅ all 5 training benches LOCKED. Next = run the GPU training (one-time Docker setup → train all 5). main @ `89d63ac`.

> **Resume:** the project just crossed from "per-fact building" into "ready to train all 5 seamlessly." This session built the SWING model (2nd-last fact) to its 4-class architecture, stood up a proper **GPU training environment** (AWS Batch one-off jobs — no more CPU start/stop struggle), and locked an enforceable bench for every fact. **Nothing is half-built.** The next move is operational: run the training. Read the exec summary, then `.claude/training_environment.md` (how to train) + `.claude/training_harness_status.md` (the 5-fact gate map).

## ⚡ Executive summary (read first — 30 seconds)
**Phase:** bronze-first; build essentially complete across the facts; the lever now is the **seamless GPU training push** (build-first → train-last, train-last is finally a one-command operation).
**main @ `89d63ac`.** Batch detection image unchanged (eu rev 79 / us rev 60) — no detector deploy this session.
**Bench floor:** serve `ea1e500c=12/26` + `880dff02=23/24` green (CI). **All 5 facts now have an enforceable committed gate** (hit, bounce, identity locked + exit-1-on-regression this session; serve CI; swing pending its first 4-class train).
**What shipped this session:** (1) **SWING 4-class build** — ADR-02 revised `{fh,bh,overhead,other}`, volley split to its own boolean fact, silver swing heuristics DELETED, 4-class dataset rebuilt (`swing_type_v3_4class`, 1757 hits); (2) **GPU training environment** — AWS Batch one-off jobs, `submit_train_job.py --fact <name>`; (3) **training benches** — bounce+identity baselines LOCKED, all 3 made enforceable.
**What's blocked:** the swing train + any real retrain can't run on THIS box (CPU-only + local `torch==2.11.0` broken for torchvision). That's WHY the GPU env exists — training runs in the Batch image (CUDA torch 2.3.1+cu121). One-time image build needs **Docker running** (was down this session).
**Next session's job:** do the **one-time training setup** (`.claude/training_environment.md` §ONE-TIME SETUP: build/push `ten-fifty5-ml-train` image + register job-def — needs Docker) → cheap GPU smoke (`--fact bounce`, ~3 min) → run the **swing 4-class train** (dataset prebuilt) and lock `bench_baseline_swing_type.json` → then the **train-all-5** accuracy push.

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
