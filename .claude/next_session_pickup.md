# Next-session pickup — 2026-06-16 EOD — ★ BRONZE DETERMINISTIC DEV COMPLETE. Training is the next (and final) phase. main @ `2a4886a`.

## ⚡ Executive summary (read first — 60 seconds)

**Today's date:** 2026-06-16
**Phase:** **Bronze deterministic DEV is DONE** — every clean code fix is shipped. Every remaining gap is **training/data** (the build-first/train-LAST endpoint we've been driving to).
**Bench:** `ea1e500c=12/26, 880dff02=23/24` — GREEN (verified repeatedly today).
**What shipped this session:** far-pose serve path retired + ball_speed wired + pre-match warm-up exclusion + the `recon_line` tool + RULE 6 (reconciliation/exclusion methodology).
**What's blocked:** nothing on DEV. Training needs (a) more sharp-far full-res uploads (data, Tomo) and (b) a GPU train run.
**Next session's job:** EITHER (1) **training** (stroke/hit, bounce, swing — measure with `recon_line`), OR (2) the **doc + legacy-code baseline cleanup** (scoped below — Tomo asked for it).

If that's enough, stop reading. Depth below.

---

## THE BIG REFRAME (what happened today — important context)

The 2026-06-15 "BRONZE BUILD COMPLETE / train next" claim was **audited and found wrong**, then corrected. A full due-diligence audit (`.claude/audit_bronze_build_2026-06-16.md`) on the reference video proved:
- Serve over-emission (8× on ea085d50) was NOT training-fixable — it came from the **far-pose heuristic path unioned into bronze**; training the model can't remove it (separate path). The fix was **architectural** (retire far-pose), proven on a real upload (model_far covers the same serves, recall held 18/24).
- The candidate "quick fixes" were tested against the bench and **one failed** (a far-pose corroboration gate regressed 9/10→0/10 — real far serves are indistinguishable from FPs without the model). The bounce rally-gate "bug" was a **measured non-bug** (37%→34%, deliberately disabled).
- Conclusion: only THREE clean DEV fixes existed, and they're now all shipped.

**Lesson banked:** count-alignment ≠ correctness; reconcile SA-active vs T5-active at the EVENT level (`recon_line`); classify every gap as structural-exclusion / detector-fix / training (RULE 6). Never ship a heuristic the bench disproves.

## What shipped (commit trail, all on origin/main)
1. `1c05502` docs — RULE 6 (reconcile SA-active vs T5-active; exclusion is a bronze fact) + audit verdict
2. `c174df5` **far-pose serve path retired in prod** (`SERVE_FAR_POSE_ENABLED=0` in render.yaml; code default stays ON so CI bench stays green)
3. `185b796` **`recon_line` diag tool** — line-level SA-active vs T5-active, ~12 fields, ~1s match
4. `ffa4567` **ball_speed wired** — `stroke_events.ball_speed` (was 0/n in silver); silver projects verbatim
5. `6576054` **pre-match warm-up exclusion** — first-net-crossing-bounce cutoff in both detectors (prod-side)
6. `2a4886a` docs — north_star marked BRONZE DETERMINISTIC DEV COMPLETE

## The reference pair (use for every recon)
SA truth `079d2c62-b871-4364-b0ad-5da0fc268848` ↔ T5 `375198f5-1adf-4c6f-9862-be8466f0c192`
(video `1781589562_match.mp4` — the canonical 10-min test match; SA ≈ 24 serves / 68 floor bounces / 87 active swings).
Run: `python -m ml_pipeline.diag.recon_line 375198f5-... --sa 079d2c62-...`

## Post-fix recon scorecard (far-pose off + ball_speed + cutoff) — the honest state
| element | state | verdict |
|---|---|---|
| serve count | 28 vs SA 24 | ✅ DEV done (far recall = train) |
| serve agree (matched) | 77% | — |
| volley agree | 80% | bounce-recall-gated (TRAIN) |
| swing_type agree | 57% | TRAIN (classifier accuracy) |
| ball_hit_xy | ~1.0m median | OK |
| **ball_speed** | wired; matched median T5 ~83 vs SA ~90 | ✅ DEV done; per-shot ±40 = ball-tracker-limited (TRAIN) |
| identity A/B | clean (0% pollution) | ✅ done |
| near player position | −0.42m | ✅ done |
| stroke WHEN/WHO recall | **40% line-level (35/87)** | ❌ TRAIN (the big one — stroke detector) |
| bounce recall | 28 vs 68 floor | ❌ TRAIN (sharp-far retrain) |
| far player position | ~absent | ❌ TRAIN/coverage |

**No code fix remains for any ❌** — all are training/data. That is the DEV-done line.

## Env flags that make prod correct (don't lose these)
- `SERVE_FAR_POSE_ENABLED=0` (render.yaml, main API) — far-pose retired. Rollback=1.
- `T5_STROKE_DRIVEN_SILVER=1` (default on) — hit-driven silver.
- `T5_SERVE_FROM_EVENTS`, `T5_BOUNCE_FROM_MODEL`, `SERVE_MODEL_ENABLED`, `SWING_CLASSIFIER_ENABLED`, `BOUNCE_CNN_THRESHOLD=0.70` — all default-on/set; see docs/env_vars.md.

## NEXT SESSION — pick one
### A) TRAINING (the final phase) — `.claude/training_environment.md`
GPU Batch one-off jobs: `submit_train_job.py --fact {serve|hit|bounce|swing}` (job-def rev 3). Bounce recall is highest-leverage (gates bounce + volley). Re-bench swing on GPU. Deploying retrained detection weights = rule-#8 detection-image rebuild. Gated on Tomo's sharp-far full-res uploads (data).
### B) DOC + LEGACY-CODE BASELINE CLEANUP (Tomo asked 2026-06-16)
Plan in the close-out recommendation: (1) doc-tier sanity sweep (north_star / CLAUDE.md / handover all reflect DEV-done + RULE 6), (2) legacy-code inventory refresh (cleanup sprint did Phase 1; bounce-driven retirement still HELD — see below), (3) archive superseded session docs.

## HELD / not-yet-done (deliberate)
- **Bounce-driven silver rollback NOT retired** — still the fallback until stroke-driven is proven on a fresh REAL upload (this session validated by re-firing detectors on 375198f5, which is strong but is one match). Keep `_t5_pass1_load_bounce_driven` until then.
- `recon_line` swing-type mapping is coarse (fh/bh/oh/other) — fine for now.

## Key docs
`docs/north_star.md` (banner + RULE 6) · `.claude/audit_bronze_build_2026-06-16.md` (the full audit + 3 rounds of verdicts) · `docs/_investigation/bronze_silver_18_audit.md` · `.claude/handover_t5.md` (ops) · `.claude/training_environment.md` (how to train) · memory `feedback_reconciliation_and_exclusion_methodology`.

---
**END OF PICKUP**
