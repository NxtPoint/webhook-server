# Next-session pickup — 2026-06-07 — SERVE SIGNED OFF end-to-end; D1+D2 deployed (rev 74/55), p11 validating

## ⚡ Executive summary (read first — 30 seconds)
**Phase:** bronze-first; **SERVE IS SIGNED OFF** (north_star sign-off list updated). Deployed: **eu rev 74 / us rev 55** (amd64 `ac33fc04`).
**Bench:** NEW FLOOR `ea1e500c=12/26` (CI fixture, rev-72 clean coords, SA truth ba4812be 26 serves) + `880dff02=23/24` (legacy-path guard). Green, CI green.
**Serve final (p10, rev 73):** near 13/14, **far 7/12 (was 3/12)**, total **20/26** at eval tol; silver↔bronze trace **48/48 BOTH directions**. Chain: Batch serve-model stage (`serve_candidates`) → detector `model_far` merge → bronze `serve_events` → silver verbatim (min-conf **0**).
**In flight:** p11 probe (`90bba646` / Batch `e5cb2197`, rev 74) validating D1+D2. Validate with `.claude/tmp/p11_validate.py` when SUCCEEDED.

## The day's chain (all on main, all bench-green, CI green)
1. **Fixture regen + re-baseline** (`f28a4d9`,`08b5b13`): harness drift fixed — fixtures now carry CNN bounces (schema v2, prod-parity); CI fixture a798eff0→ea1e500c (12/26); a798eff0 retired (S3 archived). All old fixtures were the SAME video, warp-era.
2. **Zone tighten** (`3b33c9c`): `_baseline_zone` far (-3.5..4.5)→(-5.0..2.0); P 39→45.7.
3. **Scorecard promoted** (`12aad57`): `python -m ml_pipeline.diag.scorecard <job_id>`; fresh 18-field table + sign-off list in north_star (`197bccc`).
4. **Serve model v1 retrained** (`ccc3c6d`): clean held-out eval via EXTRA_EVAL; gate met (far 4/10 @ P 0.40).
5. **C1 ROI gate** (`a841c6d`): rally gate on validated PROJECTED bounces — far wind-ups 11/12→0/12 blocked (NULL-coord pre-serve ball-bouncing was the blocker, validity rule keeps NULLs).
6. **Batch serve stage** (`399712c`): `ml_analysis.serve_candidates` (survives re-ingest like ball_bounces); `SERVE_MODEL_STAGE=1` on job-defs.
7. **Detector wire-in** (`63e2f5b`,`f2be8b4`): `model_far` additive merge; **SERVE_MODEL_ENABLED default ON** post-p10.
8. **⚠️ RULE-1 AUDIT FINDING** (`d4ebb95`,`a54d11a`): T5_SERVE_FROM_EVENTS had NEVER been live in prod (default-OFF, Render env flip never landed) — silver ran the legacy geometric serve path for 10 days while docs said "inherits verbatim"; the "24v26 count-aligned" was coincidence (1/24 traced). Fixed: **default ON in code** + overlay inherits by event player_id (NULL hitter coords tolerated — mandatory for model_far events) + **min-conf 0 (Tomo: "literally everything verbatim")**. See memory `count-alignment-is-not-provenance`.
9. **D1** (`49ef908`): tier-500 got a geometric domain — the standing spectator at (-4.8,+6.1) was pid-1 in 45% of its non-NULL frames (tier-500 had NO bounds; pose-carrying off-court people qualified). Predicate validated: kills 950/969 FP rows, 0 real.
10. **D2** (`aba54ad`): NULL-coord CNN bounces get court coords by projecting ball image xy at the bounce frame (ball is ON the ground plane exactly then); was 72% NULL, 140/140 fillable. Feeds the ROI gate density too.

## Deploy state
- **eu rev 74 / us rev 55** @ amd64 `ac33fc04` (D1+D2). rev 73/54 @ `606a5c7d` (serve stack). Cross-region digests VERIFIED equal (a tag/push race on the 73 deploy briefly pushed stale bits to us-east-1 — caught by the digest check; handover step 3 now mandates cross-region digest equality, `c2f8f65`).
- Env knobs (all default-ON in code, env = rollback): `SERVE_MODEL_ENABLED`, `T5_SERVE_FROM_EVENTS`, `SERVE_CNN_BOUNCES`; `T5_SERVE_EVENTS_MIN_CONF=0.0`; Batch-side `SERVE_MODEL_STAGE=1`. All documented in docs/env_vars.md.

## NEXT (in order)
1. **p11 validation** (if not done): `.claude/tmp/p11_validate.py 90bba646-2745-4d4a-8e03-10c0b8ad4ad3`. Bars: pid-1 off-court ≪45%, FAR p90 tightens from +8.07, bounce NULL ≪72%, far serve ≥7/12, near 13/14. If D2 fills change serve numbers (more validated bounces → rally gating shifts), investigate before celebrating either direction.
2. **Regen ea1e500c fixture from a rev-74 run + re-baseline** if p11 moves serve numbers (same rule-9 unit as before). Note: p11 silver build for probe needs local rerun-silver.
3. **bench_silver baseline regen** (stale + the serve-inheritance flip shifts it).
4. Remaining 18-field items: **stroke event alignment** (TRAIN territory — heuristic at ceiling on 3 revisions: near 13/51@1s), **swing v2.1** (4th class), **bounce recall** (38%), set_number, point/game structure on next real upload.
5. Corpus retrains as Tomo uploads (serve model first — clean-coordinate features now accumulating).

## Canonical state
- main @ `aba54ad` synced with image rev 74/55. Bench floor: ea1e500c 12/26 + 880dff02 23/24.
- Probe rows in ml_analysis: p9b `ea1e500c` (scorecard source + fixture), p10 `432c3ff3` (serve-stack validation + silver build exists), p11 `90bba646` (in flight).
- Reference video local: `ml_pipeline/test_videos/a798eff0_sa_video.mp4`. SA companion `ba4812be` (26 serves: 14N/12F).
- Probe harness: `.claude/tmp/probe_{submit,measure}.py`, `p10_validate.py`, `p11_validate.py`; per-run scorecard now in `ml_pipeline/diag/scorecard.py`.

## Memory entries this arc
`nat-idle-drop-long-db-connections` (dataset build hang), `count-alignment-is-not-provenance` (the rule-1 audit), handover deploy step 3 cross-region digest check (`c2f8f65`).
---
**END OF PICKUP**
