# Next-session pickup — 2026-05-27 (latest) — serve hit its DEV CEILING; T5 silver now inherits serve_events

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" (bronze = single source of truth; silver inherits bronze + applies SAME derive logic both flows; one-model-per-fact; build-first/train-last).

**Date:** 2026-05-27 (latest session)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (floor locked, unchanged all session).

**The recipe (Tomo, confirmed this session):** for each of the 18 base fields — **build a model to its dev ceiling (~60-70%), THEN train to 90-95%** using the dual-submit corpus with **SportAI as a reliable teacher** (SA's serve+stroke mapping is *generally good* — see correction below). Silver inherits the 18 base verbatim from each flow's bronze; the ~20 derived fields (serve_d, zones, aggression, point/game, serve-location 1-8) apply the **same shared code** for both flows.

**What shipped this session:**
1. **Serve detection confirmed at its DEV CEILING.** Receiver false-positives (the far player standing at baseline while receiving) are the dominant serve-precision error. Two structural suppression attempts (minority-server by raw count, then by serve-cluster count) BOTH regressed the bench near-recall (13/14→1/14, then →5-7/14) because the far player over-emits events. Combined with the already-proven-bad per-event bounce/source filters (detector.py NOTE), **serve precision is the far-court ceiling → training territory, not heuristics.** Reverted clean.
2. **T5 silver now INHERITS serve_events verbatim** (`fc9bc6b`), gated `T5_SERVE_FROM_EVENTS` (default OFF, env-flip rollback). Overlay in `build_silver_match_t5.py`: suppresses Pass-1 geometric serve firing, maps each `serve_event >= T5_SERVE_EVENTS_MIN_CONF` (0.70) onto a serve row (carrying the model's hitter pos; y snapped to baseline; bounce kept), demotes stray overhead-at-baseline rows so the shared pass-3 can't re-flag. **T5 serve rows == bronze serve_events exactly.** Validated Match 1 (rolled back then applied for real): serves 21→**26** (= SA's 26), SA-point coverage held 14/18, honest cost points 17→21 / games 3→5 (bronze receiver-FP over-fire flowing through honestly). SportAI untouched. Bench unaffected.
3. **Match 1 (`78c32f53`) silver REBUILT live with the overlay** — its gold serve data now reflects bronze verbatim. (Pipeline-wide still needs the dashboard flag — see below.)
4. **Comment corrections** (`ef13e40`, `7ea804d`): tagged + then corrected the geometric serve_d comment — SA serve mapping is generally good; the "8/160" was ONE camera-setup-affected video, not SA in general.

**OPEN — to actually turn the overlay on pipeline-wide (Tomo, dashboard):** set `T5_SERVE_FROM_EVENTS=1` on the **main API service** ("Sport AI - API call") env in the Render dashboard (same place AUTO_DUAL_SUBMIT_T5 etc. live — NOT render.yaml), then it applies on future T5 ingests; `rerun-silver` to backfill existing T5 tasks. Match 1 already done manually.

**Next session's job — pick one:**
- **(A) Serve TRAINING infra.** The corpus today has ONLY `label_kind='ball_position'` (488 labels across 2 matches: Match 1 + c645a7ee; video 3 not landed). There is NO serve classifier (`serve_classifier.pt` absent, no scaffold) and NO serve labels in the corpus. So "train serve" = BUILD: (1) serve-label extraction into the corpus (SA `serve_events` as teacher + T5 pose/ball features, new `label_kind='serve'`), (2) a serve classifier, (3) accumulate ~5-10 matches, (4) train, (5) watch gold improve. Nearer corpus-ready lever: **retrain the BALL model** (TrackNet/WASB) on the 488 ball labels → better far-serve bounce corroboration → lifts far-serve recall indirectly (but 2 matches is thin).
- **(B) Apply the recipe to another of the 18** (build-to-dev-ceiling on a near/buildable field).
- **(C) Symmetric serve_d cleanup** (the architectural target below) — make `serve_d` INHERIT each flow's bronze serve verbatim (SA flag, T5 serve_events), retire the geometric gate. Identical "inherit" logic both flows, zero special-casing. Touches customer-facing SA silver + must handle the one-bad-camera video → daylight + care.

## Architecture invariant (Tomo's design — confirmed in code this session)
- 18 base columns: inherited verbatim from each flow's bronze. ✅
- ~20 derived columns: **same shared code** (pass3/4/5 in build_silver_v2, no model branches) for both flows. ✅ confirmed by audit.
- **ONE asymmetry exists (serve):** serve_d formula is shared, but T5's inputs are now seeded from serve_events (the overlay) while SA's are pure-geometric — deliberate (T5 has a real serve model). Fully-symmetric target = option (C) above.

## Read in this order
1. This file.
2. `docs/north_star.md` — RULES → 18-field build status → far-court ceiling (serve precision now CONFIRMED ceiling).
3. `docs/_investigation/bronze_silver_18_audit.md` — the per-field model blueprint.
4. `.claude/handover_t5.md` — ops / how-to-run / deploy.

## Commits this session
`ef13e40` tag stopgap · `fc9bc6b` T5 serve_events inheritance (gated) · `7ea804d` correct serve_d comment. (Parallel docs commits `db34fc7`/`83a8ca3` about AWS on-demand capacity were not mine.) All pushed; origin/main = `7ea804d`.

## Scratch (gitignored, .claude/tmp/)
`measure_serve_inherit.py`, `probe_serve_anchoring.py`, `compare_serve_structure.py`, `bronze_serve_dedup.py` (serve bronze characterisation), `validate_serve_overlay.py` + `validate_overlay_final.py` (rolled-back silver validation harness — the pattern for safely testing silver changes against prod without mutating it).
