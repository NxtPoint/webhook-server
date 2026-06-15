# Next-session pickup — 2026-06-15 PM — ✅ BRONZE BUILD COMPLETE (all 6 base facts model-emitted + silver-verbatim). main @ `943b159`.

## ⚡ Executive summary (read first — 30 seconds)
**Today's date:** 2026-06-15
**Phase:** build-first DONE → **train-last** is now the only lever.
**main @ `943b159`** (+ a docs commit). All T5 changes this session are **Render-side** (already pushed → Render auto-deploys). Detection image unchanged (eu rev 80 / us rev 61). Bench floor untouched.
**What shipped:** closed the last build stopgaps so **silver Pass-1 is now 100% verbatim bronze, no heuristics**:
- **hit-WHERE keystone** (`867119f`+`746b954`): `stroke_detector` now emits `ball_hit_location_x/y` + `hitter_side_near` to `stroke_events`; silver projects verbatim (reconstruction deleted). Verified ea085d50: 432/432 traced, Pass-3 unchanged.
- **hit-WHO** (`943b159`): `detect_identity_for_task` wired into ingest; silver maps side→stable A/B (rally + serve via `_ab_pid`). Verified: player_id now person-stable across changeovers (each id on both ends).
- **volley** (`fba739a`): deterministic no-bounce-since-hit rule → `stroke_events.volley`; silver verbatim. **Architecture done; accuracy BLOCKED on bounce recall** (566 vs SA 20 — bounce model only found 119/407; train-last).
- **swing fh/bh** (`15734f5`): silver was dropping bronze stroke_class; now projected verbatim (fh 1→43, bh 1→24).
- **Definition-of-Bronze-Complete** locked (`d551700`) to kill the "complete→not" churn.
**What's blocked:** nothing build-wise. All residual is TRAIN-LAST.
**Next session's job:** TRAIN. (1) lock the swing bench (the one missing committed gate); (2) retrain on the sharp-far corpus (serve/hit/bounce/swing); (3) re-verify each fact on a real upload. NO more silver/detector build work to reach bronze-complete.

---

## ★ DEFINITION OF BRONZE-COMPLETE (the anti-drift gate — canonical: `docs/_investigation/bronze_silver_18_audit.md`)
A fact is bronze-complete ONLY when **a MODEL emits it to `ml_analysis.*`** AND **silver Pass-1 projects it VERBATIM**. NOT signals: count-alignment with SA, "model exists" (disabled/unwired ≠ done), silver recomputing. Accuracy is train-LAST and does NOT gate completeness. A box is only ticked once **verified on a real rev-80+ upload**.

## The base facts — status (BRONZE BUILD COMPLETE)
| Fact | Model → bronze | Silver | Status |
|---|---|---|---|
| serve | serve_detector → serve_events | verbatim overlay | ✅ build done; far over-emission = train |
| bounce | bounce CNN → ball_bounces | verbatim | ✅ build done; recall = train |
| swing_type | stroke_classifier → player_detections.stroke_class | verbatim (carrier lookup) | ✅ build done; accuracy = train |
| hit WHEN | stroke_detector → stroke_events.predicted_hit_frame | verbatim | ✅ done |
| **hit WHERE** | stroke_detector → stroke_events.ball_hit_location_x/y | **verbatim** | ✅ **done (this session)** |
| **hit WHO** | identity_detector → player_identity_segments | **side→A/B verbatim** | ✅ **done (this session)** |
| **volley** | stroke_detector → stroke_events.volley | verbatim | 🟡 architecture done; accuracy BLOCKED on bounce recall |
| ball_player_distance | derived from 2 bronze coords | computed | 🟢 legit derivation |

**No Pass-1 base-fact heuristics remain.** `grep "STOPGAP-until-"` in build_silver_match_t5.py: only the bsn→bounce-opposite transition shim (until all tasks re-ingested) + volley's bounce-recall dependency.

## ⚠️ Verification status (IMPORTANT — "verified ≠ moved")
All five changes verified **on `ea085d50`** (the only rev-80 task) by **re-firing the Render detectors locally vs the prod DB** + rebuilding silver. **Existing T5 tasks need a full re-ingest** to populate the new bronze columns (`ball_hit_location`, `hitter_side_near`, `volley`, `player_identity_segments`); until then they fall back (transition shims). New uploads get everything natively. **Re-verify on the next real upload** before treating any accuracy as moved (rule: count alignment is not provenance).

## Next steps (priority — ALL train-last)
1. **Lock the swing bench** — `bench_swing_type --bless` in the training image (local torch broken). The one missing committed gate. Confirm `--dataset-dir swing_type_v3_4class`.
2. **Train on the sharp-far corpus** — `submit_train_job --fact {serve|hit|bounce|swing}` (GPU job-def `ten-fifty5-ml-train:2`, trains on full 11-pair corpus incl. 3 sharp-far). Measure with the benches.
3. **Bounce recall** is the highest-leverage train target — it gates BOTH bounce accuracy AND volley accuracy (volley over-emits at 566 because bounce recall is low).
4. **Re-verify** WHERE/WHO/volley on the next real upload (full re-ingest path, not local re-fire).
5. Deploy decisions (bounce threshold re-sweep, overnight weights) AFTER measuring.

## Corpus / training state
`ml_analysis.training_corpus` = **11 SA↔T5 pairs**, all 3 label kinds; the 3 most recent (Jun 14–15) are the sharp-far re-runs (`93ebb93d`, `7d3e2392`, `ea085d50`). Only `ea085d50` ran on rev-80; the other two are pre-deploy (re-run to refresh). Trainer trains on the FULL corpus (no per-match flag). Identity has no trainer (rule-v1).

## HELD items (train-last / decisions, AFTER the build is verified on a real upload)
- **Swing bench NOT locked** (`bench_baseline_swing_type.json` absent).
- **Serve far over-emission** (ea085d50: 336 far events → 389 serves / 173 points), inherited verbatim — precision = train (no silver filter, rule #1).
- **Overnight weights HELD in S3** (serve F1 0.47; hit prec 54→68% strong; bounce precision dropped at thr 0.70 → re-sweep before deploy). Bounce deployed at thr 0.85.

## Watch-items
- **Parallel marketing session is live** on `frontend/`, `locker_room_app.py`, `marketing_app.py`, `build_blog.py` — always `git pull --rebase` before push; stay out of those files.
- New bronze columns on `stroke_events` (ball_hit_location_x/y, hitter_side_near, volley) are idempotent `ADD COLUMN IF NOT EXISTS` — they land on Render boot.

---
**END OF PICKUP**
