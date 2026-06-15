# Next-session pickup — 2026-06-15 PM — ✅ BRONZE-COMPLETE *defined + locked*; 3 stopgaps remain to close it. main @ `d551700`.

## ⚡ Executive summary (read first — 30 seconds)
**Today's date:** 2026-06-15
**Phase active:** bronze-first, closing the last 3 base-fact stopgaps to reach DEV-CEILING bronze-complete (then train-last only).
**main @ `d551700`.** Detection image unchanged (eu rev 80 / us rev 61). Bench floor untouched.
**What shipped this session:** (1) **fixed the swing fh/bh silver leak** (`15734f5`) — stroke-driven Pass-1 now projects bronze `stroke_class` verbatim (was dropping it: silver fh 1→43, bh 1→24 on `ea085d50`, verified). (2) **Locked a Definition-of-Bronze-Complete** (`d551700`) to kill the "complete→actually-not" churn + tagged every Pass-1 stopgap `STOPGAP-until-<model>`.
**What's blocked:** nothing in code. The 3 stopgaps are model-gaps to BUILD, not bugs.
**Next session's job:** close the 3 stopgaps (B keystone first). All appear RENDER-SIDE (stroke_detector + identity_detector run on Render) → git-push deploys, NOT Batch rebuilds. Confirm, then build to dev ceiling + verify on a real upload.

If the above is enough, stop and go. Depth below.

---

## ★ DEFINITION OF BRONZE-COMPLETE (the anti-drift gate — canonical in `docs/_investigation/bronze_silver_18_audit.md`)
A base fact is bronze-complete ONLY when **(1) a MODEL emits it to `ml_analysis.*`** AND **(2) silver Pass-1 projects it VERBATIM**. NOT signals: count-alignment with SportAI, "the model exists" (disabled/unwired doesn't count), silver "inheriting" a value it recomputed. **Accuracy is train-LAST and does NOT gate completeness.** A box is only checked once **verified end-to-end on a real rev-80+ upload** (`ea085d50` is the only such task today).

## The 18 base fields — LIVE status (stroke-driven Pass-1)
| Fact | Status | Gate to DONE |
|---|---|---|
| serve | ✅ COMPLETE (serve_events, verbatim) | precision = train-last (far over-emission) |
| bounce (court_x/y, speed) | ✅ COMPLETE (ball_bounces, verbatim) | recall = train-last |
| swing_type | ✅ COMPLETE (stroke_class, verbatim — fix `15734f5`, verified `ea085d50`) | accuracy = train-last |
| hit WHEN (ball_hit_s) | ✅ COMPLETE (stroke_events frame) | — |
| **hit WHO (player_id)** | 🟡 STOPGAP-until-identity | wire `detect_identity_for_task` into `_do_ingest_t5` + A/B join in silver |
| **hit WHERE (ball_hit_location_x/y)** | 🟡 STOPGAP — **KEYSTONE (B)** | enrich `stroke_events` to carry hit location → silver projects verbatim |
| **volley** | 🟡 STOPGAP-until-volley | bronze volley signal (ball-not-bounced-before-hit) |
| ball_player_distance | 🟢 legit derivation | — |

**Scoreboard: 4 COMPLETE / 3 STOPGAP / 1 derivation. We are NOT bronze-complete.** Grep `STOPGAP-until-` in `build_silver_match_t5.py` to find every non-verbatim Pass-1 field.

## The 3 stopgaps to close (priority order)
1. **(B) hit-WHERE — keystone.** `stroke_events` carries timing only; silver RECONSTRUCTS hit location (side-resolve + mirror-fallback). Move the hit-event assembly into the bronze stroke_detector so `stroke_events` carries `ball_hit_location_x/y` (+ side) → silver one-row-per-event verbatim. ⚠️ Pass-3 point/serve-numbering is coupled to hit/side geometry — don't break it (audit warned this on the serve wire, line ~100). stroke_detector is RENDER-side → git push, not Batch.
2. **hit-WHO — identity.** identity_detector v1 exists + benched (100%) but `detect_identity_for_task` is NEVER called in `_do_ingest_t5`, and silver never reads `player_identity_segments`. Wire it (Render-side) + add the A/B join so silver player_id is stable A/B (matches SA's person-based id). 
3. **volley.** No model emits it; silver uses net-distance. Add a bronze volley signal in the stroke_detector (ball-not-bounced-before-hit) → silver projects verbatim.

**Dev-ceiling rule for all three:** build the model to emit the fact + silver verbatim; STOP. Remaining accuracy (esp. far-side) is train-last (sharp-far corpus), not buildable.

## Corpus / training state (verified live DB)
- `ml_analysis.training_corpus` = **11 SA↔T5 pairs**, each with all 3 label kinds (ball_position 4152, serve 590, stroke_classifier 3263). The **3 most recent (Jun 14–15) are the sharp-far re-runs** (`93ebb93d`, `7d3e2392`, `ea085d50`). Only `ea085d50` ran on the rev-80 image (has bronze `stroke_class`); the other two are pre-deploy (stale — re-run to refresh).
- Trainer: `submit_train_job --fact {serve|hit|bounce|swing}`, GPU job-def `ten-fifty5-ml-train:2`, trains on the FULL corpus (no per-match flag). Identity has no trainer (rule-v1).

## HELD / open accuracy items (train-last, NOT build — do AFTER the 3 stopgaps)
- **Swing bench NOT locked** — `bench_baseline_swing_type.json` absent; run `bench_swing_type --bless` in the training image (local torch broken). The one missing committed gate.
- **Serve far over-emission** — bronze far-serve fires ~hundreds/match (ea085d50: 336 far events → 389 silver serves / 171 points), inherited verbatim. Precision = train-last (rule #1: no silver filter).
- **Overnight retrained weights HELD in S3** (serve F1 0.47, hit prec 54→68% strong, bounce precision dropped at thr 0.70 → re-sweep before deploy). Deploy decisions AFTER the stopgaps + bench lock.
- Bounce deployed at thr 0.85 (rev 80/61).

## Watch-items
- **Parallel marketing session is live** on `frontend/`, `locker_room_app.py`, `marketing_app.py`, `build_blog.py` — pushes commits during T5 work. Always `git pull --rebase` before push; stay out of those files.
- Bench config-sensitivity unchanged (bounce needs gravity_residual + weights + thr).

---
**END OF PICKUP**
