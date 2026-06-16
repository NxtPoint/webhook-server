# Next-session pickup — 2026-06-16 — ★ DOC + LEGACY-CODE BASELINE CLEANUP DONE. Bronze DEV complete; training is the only remaining (incremental) phase.

## ⚡ Executive summary (read first — 60 seconds)

**Today's date:** 2026-06-16
**Phase:** **Bronze deterministic DEV is DONE.** Every remaining gap is training/data. This session was the **doc + legacy-code baseline cleanup** (Tomo asked for it after DEV-complete was declared) — now finished.
**Bench:** `ea1e500c=12/26, 880dff02=23/24` — GREEN (verified before + after the cleanup; this session was docs/memory only, no code touched).
**What shipped this session:** full doc-tier realignment to DEV-COMPLETE (CLAUDE.md, north_star lean-rewrite with history archived, handover_t5, env_vars, sop, docs_hygiene, training docs), investigation-doc resolved-banners, cleanup-inventory status update, 4 shipped-phase kickoffs archived, 6 memory entries refreshed.
**What's blocked:** nothing on DEV. Training needs (a) more sharp-far full-res uploads (DATA, Tomo) and (b) a GPU train run.
**Next session's job:** **TRAINING** — the final, incremental phase. `submit_train_job.py --fact {serve|hit|bounce|swing}` (job-def rev 3); measure with `recon_line` against the reference pair. Bounce recall is highest-leverage (gates bounce + volley).

If that's enough, stop reading. Depth below.

---

## What the cleanup did (this session — all on origin/main)

Five read-only audit agents (one per doc-tier) + serialized writers. Bench stayed green throughout; no code changed (the prior sprint had already cleared all provably-dead Render-side code).

1. `9832201` — **archived 4 shipped-phase kickoffs** (identity / swing-classifier / court-calibration kickoffs + adr01 label audit) → `.claude/_archive/`.
2. `eb05fd9` — **Tier-1 truth docs.** north_star collapsed from 475 lines (10 stacked banners + superseded strategy/scorecard blocks) to a lean current version; **full pre-cleanup file archived verbatim** at `docs/_archive/north_star_2026-06-16_pre-dev-complete-cleanup.md`. RULES (incl RULE 6) kept verbatim. CLAUDE.md: DEV-COMPLETE status line, swing-classifier PROVEN, far-pose-retired note, `recon_line` in diag lists, dropped `tracknet_v3.pt`, rule #11 update.
3. `da18d19` — **Tier-2 ops docs.** handover_t5 NEXT SESSION + TEST HARNESS rewritten to training-next; env_vars gained `SERVE_FAR_POSE_ENABLED` / `T5_BOUNCE_FROM_MODEL` / `SWING_CLASSIFIER_ENABLED`; sop bench baseline fixed; docs_hygiene dangling strategy/research refs repointed; training docs job-def rev 3 + recon_line.
4. `244dabc` — **investigation docs + cleanup inventory.** Resolved/superseded banners on 3 investigation docs; `t5_cleanup_inventory.md` status → Render-side cleanup COMPLETE.
5. **Memory** (local, not git): refreshed `project_far_player_stroke_research` (swing PROVEN), `project_t5_may27_serve_dev_ceiling` (far-pose architectural fix, not training), `feedback_bronze_first_t5_reconciliation` (flag default ON), `project_t5_may07_phantom_bounces` (warm-up exclusion shipped), `project_dual_submit_pipeline_state`, + new MEMORY.md DEV-COMPLETE bullet. Canonical reconciliation memory = `feedback_reconciliation_and_exclusion_methodology` (RULE 6).

## The reference pair (use for every recon)
SA truth `079d2c62-b871-4364-b0ad-5da0fc268848` ↔ T5 `375198f5-1adf-4c6f-9862-be8466f0c192`
(video `1781589562_match.mp4`; SA ≈ 24 serves / 68 floor bounces / 87 active swings).
Run: `python -m ml_pipeline.diag.recon_line 375198f5-... --sa 079d2c62-...`

## Honest recon scorecard (the DEV-done line — unchanged this session)
| element | state | verdict |
|---|---|---|
| serve count | 28 vs SA 24 | ✅ DEV done (far recall = train) |
| volley agree | 80% | bounce-recall-gated (TRAIN) |
| swing_type agree | 57% | TRAIN (classifier accuracy) |
| ball_speed | wired; matched median T5 ~83 vs SA ~90 | ✅ DEV done; per-shot ±40 = TRAIN |
| identity A/B | clean (0% pollution) | ✅ done |
| stroke WHEN/WHO recall | 40% line-level (35/87) | ❌ TRAIN (the big one) |
| bounce recall | 28 vs 68 floor | ❌ TRAIN (sharp-far retrain) |
| far player position | ~absent | ❌ TRAIN / coverage |

**No code fix remains for any ❌.**

## Env flags that make prod correct (don't lose these)
- `SERVE_FAR_POSE_ENABLED=0` (render.yaml) — far-pose retired. Code default ON keeps CI bench green. Rollback=1.
- `T5_STROKE_DRIVEN_SILVER=1` (default on) — hit-driven silver. Rollback=0 → bounce-driven path (`_t5_pass1_load_bounce_driven`, **HELD** until stroke-driven re-proven on a fresh real upload).
- `T5_BOUNCE_FROM_MODEL`, `SERVE_MODEL_ENABLED`, `SWING_CLASSIFIER_ENABLED`, `BOUNCE_CNN_THRESHOLD=0.70` — all default-on/set; see `docs/env_vars.md`. (`T5_SERVE_FROM_EVENTS` was DELETED 2026-06-07.)

## NEXT SESSION — TRAINING (the final phase) — `.claude/training_environment.md`
GPU Batch one-off jobs: `submit_train_job.py --fact {serve|hit|bounce|swing}` (job-def rev 3). Bounce recall is highest-leverage (gates bounce + volley). Re-bench swing on GPU. Deploying retrained detection weights = rule-#8 detection-image rebuild. Gated on Tomo's sharp-far full-res uploads (DATA). Measure every retrain with `recon_line` + the per-fact benches (`bench_hit`/`bench_bounce`/`bench_identity`/`bench_swing_type`, map in `.claude/training_harness_status.md`).

## HELD / deferred (deliberate)
- **Bounce-driven silver rollback NOT retired** — `_t5_pass1_load_bounce_driven` stays until stroke-driven is proven on a fresh REAL upload.
- **Batch-side disk-only v1 weights** (`bounce_detector_v1*.pt`, `swing_classifier_v1.pt`) — DEFER to a daylight Docker-rebuild cycle (rule #8, low value); see `docs/_investigation/t5_cleanup_inventory.md`.
- **Bench Option B** (regenerate fixtures WITH serve_candidates so far-pose-OFF is bench-guarded, then flip code default) — durable follow-up, not done; not blocking.

## Key docs
`docs/north_star.md` (lean — banner + RULES + scorecard + ladder) · `docs/_archive/north_star_2026-06-16_pre-dev-complete-cleanup.md` (full build history) · `.claude/audit_bronze_build_2026-06-16.md` (the audit + 3 verdict rounds) · `.claude/handover_t5.md` (ops) · `.claude/training_environment.md` + `.claude/training_harness_status.md` (how to train) · memory `feedback_reconciliation_and_exclusion_methodology` (RULE 6).

---
**END OF PICKUP**
