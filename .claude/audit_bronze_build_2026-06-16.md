# T5 BRONZE-BUILD DUE-DILIGENCE AUDIT — 2026-06-16

**Method:** live Render DB comparison (T5 `ml_analysis.*` vs SA `bronze.*`, silver `point_detail`) +
5 parallel read-only code root-cause agents. Read-only; nothing edited or committed.

---

## ★ EXECUTIVE SUMMARY (read this first)

1. **VERDICT: We are NOT at train-stage. Bronze development is NOT done.** Tomo's instinct is correct.
2. The "BRONZE BUILD COMPLETE — only training remains" banner is **false on the very match it cites
   as proof** (`ea085d50`, rev-80).
3. On `ea085d50` vs its SportAI truth (47 serves): **serves ~8× over** (silver 356 active / 398 events
   vs 47), **volleys ~19× over** (383 vs 20), **173 points / 32 games** for one match (sane ≈ 15-20 / 10-15).
4. These are **STRUCTURAL DEV BUGS, not training gaps.** Training cannot start while emission is 8-19× over true counts.
5. **Two concrete, fixable-now DEV bugs explain almost everything:**
   - **Serve over-emission** — no per-point serve cap + `sustained_ok` rally-gate escape hatches let
     nearly every overhead motion fire as a serve (`serve_detector/detector.py`, `pose_signal.py`).
     This ALSO produces the 173-point / 32-game structure (point/game numbering anchors purely on serves).
   - **Bounce non-rally exclusion disabled** — the rally gate is fed a hardcoded `in_rally=True` for
     every frame (`ml_pipeline/__main__.py:331`), so pre-serve / between-point / airborne bounces are
     NOT excluded and leak into bronze→silver. **This is exactly the "pre-serve bounces not excluded in
     bronze" Tomo remembered.**
6. **Volley over-emission is downstream of bounce** (rule = "no bounce between consecutive hits" — correct
   definition, but bounce recall is too low so most strokes falsely read as volleys). Don't band-aid it;
   it self-corrects when bounce improves.
7. **Point/game structure is downstream of serves** — fixing serves fixes it for free. Do NOT patch the SQL.
8. **Swing-type CNN** is wired/enabled/carry-through-correct and bench-locked (0.7468 offline), but has
   only ever produced output on `ea085d50` — **unproven on a real upload**. Not a bug; an unverified claim.
9. **The "build done" sign-off rests on ONE non-reference match** whose components were written at four
   different times (bounces 08:58, players 10:50, serves 10:51, strokes 15:16) — a **patchwork of separate
   re-fires, never one coherent end-to-end run.** Your actual 10-min reference video (`match.mp4` family)
   has **never been run on current code at all.**
10. **The single test that proves dev-done:** fix the 2 DEV bugs, then do ONE clean full re-ingest of the
    reference video on current code and confirm T5 lands within tolerance of SA (serves ≈24, floor-bounces
    ≈68, swings ≈84-106, volleys ≈5-20, points ≈15-20, games ≈10-15). Until that passes once, "done" is unproven.

---

## EVIDENCE — live DB, `ea085d50` (rev-80) vs SA `5aea81d6` (47 serves)

| fact | T5 | SA | ratio | classification |
|---|---|---|---|---|
| serves (silver active) | 356 (398 events) | 47 | ~8× over | **DEV** (+ far-precision TRAIN residual) |
| volleys (silver) | 383 | 20 | ~19× over | TRAIN (bounce-recall-gated) + DEV (bounce exclusion) |
| strokes/swings | 648 | 407 | 1.6× over | mixed |
| bounces (ml_analysis) | 175 (all `in_point=TRUE`) | 330* | — | **DEV** (exclusion) + TRAIN (recall) |
| points / games | 173 / 32 | ~18 / ~12 | ~9× over | **DEV** (downstream of serves) |
| swing stroke_class rows | 1002 | — | — | UNPROVEN (only this task ever) |

\* this SA match reports floor=0 / all type='swing' — an SA-side anomaly for this match, not a T5 bug
(other SA matches show ~67 floor + ~94 swing). Confirmed: T5 reads its own bounce table, no type-misread.

---

## ROOT CAUSES (DEV vs TRAIN, with file:line)

### 1. SERVE over-emission — DEV (HIGH confidence) — fixable now
- Silver inherits verbatim (`build_silver_match_t5.py:816-927`, min-conf 0.0) — **silver is exonerated**;
  the 398→356 is a faithful echo of the bronze detector.
- **No per-point / per-game serve cap exists anywhere.** Only limiters: rally-state gate
  (`serve_detector/detector.py:718-726`) + per-player temporal dedup `min_serve_interval_s`
  (near 4.0s / far 1.5s, `pose_signal.py:271`).
- The `sustained_ok` exceptions (`detector.py:703-706`) explicitly let candidates through **even when
  `IN_RALLY`** → return-strokes, rally swings, ready-position arm-raises register as serves. Pose scorer
  fires on a single raised-arm signal (`pose_signal.py:90-176`); size-1 clusters accepted (`:361-364`).
- 398 events / 328 distinct 2s clusters, pose_only=244 (zero ball/bounce corroboration) = the tell.
- **Fix:** add a point-structure serve cap (≤2/point), require corroboration for near `pose_only`
  (ball-toss OR opposite service-box bounce), tighten/remove the IN_RALLY `sustained_ok` escape.
  Re-run `bench` (ea1e500c=12/26 guards near-serve recall).

### 2. BOUNCE non-rally exclusion — DEV (HIGH confidence) — fixable now
- `ml_pipeline/__main__.py:331`: `_rally = {fi: "in_rally" for fi in range(_last+1)}` — **every frame
  hardcoded in_rally**, so `passes_rally_state_gate` (`bounce_detector/pre_gates.py:95`) never fires.
- Confirmed: all 175 `ea085d50` bounces have `in_point=TRUE`. Net-cord/airborne gate is also a no-op
  (`above_net_flag` hard-wired False — bronze has no ball-z).
- A real serve_events-derived rally map already exists (`detector.py:120 _load_rally_states_by_frame`)
  but is not used on the Batch in-memory path. **Fix:** wire it in.
- Recall *headline* remains TRAIN-gated (sharp-far footage), but **exclusion is structural DEV.**

### 3. VOLLEY over-emission — TRAIN (via bounce), architecture correct — do NOT band-aid
- Rule (`stroke_detector/hit_location.py:189-200`, shipped `fba739a`): `volley = no detected bounce
  between prev hit and this hit`. Correct tennis definition; uses bounce-frame ordering, NOT hit coords.
- Misfires because ~70% of inter-hit bounces are missing from the stream → empty interval → false volley
  (the commit itself measured 566/648). **Self-corrects when bounce recall improves.** No threshold fix helps.

### 4. POINT/GAME structure (173/32) — DEV, but purely downstream of serves
- `_apply_serve_events_overlay` snaps each serve to a baseline-overhead row (`build_silver_match_t5.py:871-911`)
  → `serve_d=TRUE` (`build_silver_v2.py:550-556`) → point_number increments per serve-side/hitter change
  between serves (`:634-653`); game_number per server change (`:776-789`). 356 noisy serves → ~173 points / ~32 games.
- **Fix serves and this resolves for free.** Do not patch the point/game SQL.

### 5. SWING-TYPE — UNPROVEN (not a bug)
- Wired + enabled (`SWING_CLASSIFIER_ENABLED` default 1), Dockerfile COPYs `stroke_classifier/` + weights,
  carry-through correct. Bench 0.7468 is real (offline GPU). NULLs on reference-video tasks = those runs
  predate the deployed classifier (landed `00ecee5` 2026-06-04, 4-class `5d15933` 2026-06-14).
- When `stroke_class` is NULL, silver assigns literal `"other"` (sentinel, `:678-683`/`:1105-1111`) —
  NOT a derived heuristic → **no rules-of-the-game violation** (the old heuristic was deleted 2026-06-14).
- Status: needs one real upload to prove the CNN populates in production.

---

## RECOMMENDED WAY FORWARD TO 100% DEV-DONE

**Phase order (do not reorder):**
1. **Fix SERVE over-emission** (DEV #1). Highest leverage — also fixes point/game structure for free.
   Gate: `bench` green (ea1e500c=12/26, 880dff02=23/24).
2. **Fix BOUNCE non-rally exclusion** (DEV #2 — `__main__.py:331`). Removes the pre-serve bounce leakage.
   Batch-side → trips rule #8 (Docker rebuild + dual-region ECR + job-defs).
3. **Do NOT touch** volley or point/game SQL — both self-correct once #1/#2 land.
4. **Run the reference video once, clean, on current code** (the proving test). Compare T5 vs SA across
   all six headline facts. This is the gate that was never run — it, not `ea085d50`, decides "dev done."
5. Only AFTER that single run is within tolerance: the residuals (far-serve precision, bounce recall,
   far hit-WHO) are genuinely TRAIN — proceed to training.

**Cost note for the reference-video run:** ~2h Batch GPU, costs money — recommend, do not trigger without
Tomo. But run it AFTER the two DEV fixes, else it just re-confirms known bugs.

**Bottom line:** the build emits 8-19× over true counts from two fixable structural bugs plus their
downstream symptoms. That is development, not training. Fix the two, prove on the reference video, then train.

---

# ★ ROUND 2 — EMPIRICAL VERIFICATION (2026-06-16, supersedes the DEV/TRAIN calls above)

I tested the two proposed DEV fixes against live code + the locked bench. **Both initial classifications
were partly wrong, and the corrected conclusion is sharper — and it VINDICATES Tomo's "training won't
fix this" instinct, for a reason neither the audit nor north_star stated.**

## What the experiments showed

1. **Serve fix (far-pose corroboration gate) → BENCH REGRESSION, reverted.** I added a gate requiring far
   pose serves to have a ball-toss or opposite-side service-box bounce. Bench `880dff02` far recall
   collapsed **9/10 → 0/10** (near 14→13). The real far serves on the bench fixture ARE bare far
   `POSE_ONLY` events with no toss/bounce — the *identical* signal to the 219 FPs on ea085d50. **No
   corroboration signal separates real far serves from far FPs**, because the far serve's bounce is exactly
   what TrackNet misses. This reproduces the project's 6 prior failed variants (`detector.py:821` NOTE,
   memory `feedback_stroke_overemission_is_far_attribution_trained_not_rally_gated`). Reverted; bench green.

2. **Bounce fix (rally-gate) → NOT A BUG, it was a measured decision.** `ml_pipeline/__main__.py:331`
   hardcodes `in_rally=True`. The comment at `:294` is explicit: *"validated 2026-06-05: the rally gate
   barely moves precision, 37%→34%."* Enabling it was tested and slightly HURTS. The agent mis-read a
   tested-and-rejected choice as a defect.

## The corrected verdict — why "train next" is the WRONG call

`ml_analysis.serve_events` = `near_pose ∪ far_pose ∪ far_bounce ∪ model_far` (union of separate code
paths; `detector.py:1160-1166`). The 323 far false positives come from the **far-pose heuristic path**.
The trained **serve model** (`model_far`) is a SEPARATE path that only ADDS events.

**Therefore: training the serve model can never reduce the serve over-emission.** No matter how good the
model gets, the far-pose heuristic keeps emitting its ~323 FPs, and silver inherits them verbatim. "Train
next" is structurally incapable of fixing the headline problem.

The over-emission is **neither a quick bug (heuristic gates bench-fail) NOR training (separate path).** It
is an **unfinished architectural decision**: the far-pose serve heuristic (high recall, terrible precision)
runs in parallel with its intended replacement (the trained model), and nobody has decided/validated
whether the model can replace it. That decision is the remaining DEV work — and it has never been made.

## Is bronze dev done? NO — but the remaining work is ONE architectural decision, not a bug list

| fact | over-emission | nature | resolution |
|---|---|---|---|
| serve (far) | ~14× | far-pose heuristic FPs; model can't filter them by union | **DEV decision**: retire/model-gate far-pose; validate model far-recall holds. Needs proving run (bench has no model candidates). |
| serve (near) | ~2.6× | near-pose over-fire | smaller; bench-risky; defer until far decided |
| volley | ~19× | downstream of bounce recall | TRAIN (bounce CNN sharp-far retrain) — architecture correct |
| bounce precision | — | CNN discrimination | TRAIN (rally-gate already proven not to help) |
| point/game (173/32) | ~9× | downstream of serve over-emission | resolves when serve far is fixed; do NOT patch silver |
| swing_type | — | only ran on ea085d50 | UNPROVEN — needs the proving run |

## The decision that must be made (Tomo's call, informed by the proving run)

**Can the trained serve model replace the far-pose heuristic?**
- The bench fixtures contain NO model candidates, so this is **un-measurable offline.** It can only be
  answered by running the reference video on current code (model active) and comparing, with far-pose
  ON vs OFF, against SA's ~24 serves.
- If the model gives acceptable far recall with far-pose OFF → **retire far-pose** (a real, shippable DEV
  change behind a `SERVE_FAR_POSE_ENABLED` env flag). Serve over-emission collapses; point/game structure
  fixes itself.
- If not → far recall is genuinely model-capacity-limited → grow the corpus and retrain the model until it
  can stand alone. THAT is the only legitimate "train" path, and it's about model *replacement*, not
  tuning the heuristic.

## Recommended way forward (revised)
1. **Do NOT ship heuristic gates** — bench-disproven, and the project has the receipts.
2. **Stage `SERVE_FAR_POSE_ENABLED` (default ON = no change)** so the proving run can A/B far-pose-off.
   (Not yet done — needs Tomo's go-ahead; it's the only code change worth making and it's reversible.)
3. **Run the reference video once on current code** (the never-run proving test) and measure serves /
   bounces / swings / volleys / points vs SA, with far-pose ON and OFF.
4. That single run decides: retire far-pose (DEV, ship it) vs grow-model-then-retire (TRAIN). Either way,
   **"just start training" is not the answer** — the far-pose path must be addressed in code first.

**Net:** Tomo is right that we're not dev-done. The precise reason: a known-imprecise heuristic is unioned
into bronze and training cannot remove it. The remaining work is a measured architectural decision (retire
far-pose), gated on the proving run — small, but real, and not "training."

---

# ★ ROUND 3 — THE PROVING RUN (reference video, rev-80, 2026-06-16)

Reference video re-uploaded as dual-submit: SA `079d2c62` (truth) ↔ T5 `375198f5` (rev-80, first-ever
clean run on current code). Far-pose A/B measured OFFLINE (`detect_serves_offline`, read-only, no writes,
no silver). `model_far` ACTIVE (unlike the bench fixtures — the whole reason this run was needed).

## T5 vs SportAI truth
| fact | T5 (far-pose ON) | SA | read |
|---|---|---|---|
| serves | 55 | 24 | 2.3× over |
| strokes | 220 | 108 | 2.0× over |
| volleys | 128 (silver) / 201 (bronze) | 5–7 | ~26× over — **caused by bounce under-recall** |
| bounces | **28** | 162 (68 floor) | T5 massively UNDER |
| points / games | 34 / 6 | 18 / 2 | ~2× — downstream of serve over-emit |
| swing stroke_class | **257 rows (fh87/bh77/oh57/other36)** | — | **✓ WORKS on real upload — "unproven" caveat RESOLVED** |

## Far-pose A/B (the decisive experiment) — recall scored vs SA at 2.0s tolerance
| | total serves | recall | precision | over-emit |
|---|---|---|---|---|
| far-pose ON  | 55 | 18/24 | 33% | 2.3× |
| far-pose OFF | 30 | **18/24 (unchanged)** | **60%** | 1.2× |

**Retiring far-pose loses ZERO real serves and halves the over-emission.** The 25 far-pose events were all
FPs/dupes; the trained `model_far` + near-pose already catch the same real far serves. CONFIRMED DEV fix.

## CORRECTED per-fact verdict (this run is authoritative — it has the model)
| fact | verdict | action |
|---|---|---|
| **serve far** | **DEV — SHIP** | Set far-pose OFF in prod (`SERVE_FAR_POSE_ENABLED=0`). 2.3×→1.2×, zero recall loss. |
| serve near | DEV (smaller, ~1.6×) | separate investigation; bench-risky; defer |
| volley | TRAIN | bounce CNN recall (28 vs 162); architecture correct, self-corrects with bounce |
| bounce recall | TRAIN | sharp-far retrain (the standing gate) |
| swing_type | **PROVEN ✓** | works on real upload — done |
| point/game | DEV-downstream | improves automatically once serves fixed (do NOT touch silver) |

## Bench tension (the one wrinkle)
The committed bench fixtures (`ea1e500c`, `880dff02`) carry NO `serve_candidates`, so far-pose-OFF makes
them go red (far 0/10) — the bench cannot see the model that makes OFF correct. Two ways to ship:
- **A (quick):** keep code default ON (bench stays green), set `SERVE_FAR_POSE_ENABLED=0` in the Render env
  / render.yaml. Prod clean today. Risk: env-flip can rot (`feedback_count_alignment_is_not_provenance`).
- **B (durable):** regenerate the bench fixtures WITH `serve_candidates` so far-pose-OFF is bench-guarded,
  THEN flip the code default to OFF and re-baseline. Correct end-state; more work.
Recommend A now + B as the follow-up.

## Bottom line
The reference proving run resolves the audit: **serve over-emission is a SHIPPABLE DEV fix (retire
far-pose) — not training.** Volley + bounce are genuinely TRAIN (bounce recall). Swing is proven. Point
structure self-heals off the serve fix. After far-pose-off, T5 serves land 30 vs SA 24 (1.2×) with full
recall — that IS within striking distance of dev-done; the residual (near ~1.6×, volley/bounce) is a short
list, not an open-ended one.
