# Next-session pickup — 2026-06-15 EOD — ✅ BRONZE BUILD COMPLETE · SILVER 100% VERBATIM · 5/5 BENCHES LOCKED · cleanup sprint Phase 1 done. main @ `a8394b2` (+).

## ⚡ Executive summary (read first — 60 seconds)
**The build is DONE.** All base facts come from models → silver Pass-1 projects them VERBATIM (no heuristics) → 5/5 training benches locked → GPU training env proven. The ONLY two things left: (1) finish the **hardening/cleanup sprint** (Phase 1 done, rest queued), then (2) **TRAIN** (the next big phase — Tomo has questions).

**main @ `a8394b2`.** Detection image eu rev 80 / us rev 61. Training job-def `ten-fifty5-ml-train:3` (now bundles `diag/` + `--bench` mode). All T5 changes this session are Render-side (already pushed → auto-deployed). Bench floor green.

**⚠️ THE ONE CAVEAT THAT GOVERNS EVERYTHING:** every bronze/silver change this session was verified on **`ea085d50` only** (the single rev-80 match), by re-firing the Render detectors locally vs the prod DB. **The next REAL T5 upload (full re-ingest) is the true proof.** Until then: don't trust accuracy as "moved," don't deploy retrained weights, and DON'T retire the bounce-driven rollback.

**⚠️ Parallel marketing session is live** on `frontend/`, `locker_room_app.py`, `marketing_app.py`, `build_blog.py` — its files sit uncommitted in the working tree (not ours). Commit only T5 files by explicit path; stay out of those.

---

## What shipped this session (commit trail, all on origin/main)
1. `15734f5` swing fh/bh silver leak fixed (projects bronze stroke_class verbatim)
2. `d551700` **Definition-of-Bronze-Complete** locked (the anti-churn gate) + Pass-1 stopgap tags
3. `8243a2f` pickup → bronze-complete state
4. `867119f` + `746b954` **hit-WHERE keystone**: stroke_detector emits `ball_hit_location_x/y`+`hitter_side_near` → silver verbatim (reconstruction deleted)
5. `fba739a` **volley** bronze rule (`stroke_events.volley`) → silver verbatim
6. `943b159` **hit-WHO identity**: `detect_identity_for_task` wired into ingest + silver maps side→stable A/B
7. `1b31bba` / `2c2fc52` docs → BRONZE BUILD COMPLETE (pickup + north_star + audit)
8. `bfe9531` training image bench-capable (`COPY diag/` + `batch_train --bench swing`); job-def rev 3
9. `34707be` **swing bench LOCKED** (`bench_baseline_swing_type.json`, macro-F1 0.7468) — 5/5
10. `f1ba526` docs → 5/5 benches
11. `c58987f` **cleanup inventory** (`docs/_investigation/t5_cleanup_inventory.md`)
12. `1f55b9b` cleanup **Phase 1** (live-silver doc fixes + dead helpers) + `a8394b2` sprint-progress doc

## The 5 facts — final state (build done; only TRAIN remains)
| Fact | Bronze model → table | Silver | Bench |
|---|---|---|---|
| serve | serve_detector → serve_events | verbatim overlay | ✅ `bench.py` (CI) |
| bounce | bounce CNN → ball_bounces | `T5_BOUNCE_FROM_MODEL` verbatim | ✅ `bench_bounce` |
| swing_type | stroke_classifier → player_detections.stroke_class | verbatim (carrier lookup) | ✅ `bench_swing_type` (0.7468) |
| hit WHEN/WHERE | stroke_detector → stroke_events.{predicted_hit_frame, ball_hit_location_x/y, hitter_side_near} | verbatim | ✅ `bench_hit` |
| hit WHO | identity_detector → player_identity_segments | side→A/B verbatim (`_ab_pid`) | ✅ `bench_identity` |
| volley | stroke_detector → stroke_events.volley | verbatim | (rides hit; accuracy bounce-recall-gated) |
| ball_player_distance | derived from 2 bronze coords | computed | 🟢 legit derivation |

Canonical detail: `docs/_investigation/bronze_silver_18_audit.md` (Definition-of-Bronze-Complete + per-field table). Architecture rules: `docs/north_star.md` §RULES OF THE GAME.

---

## NEXT SESSION — do these in order

### A) Finish the hardening/cleanup sprint — playbook: `docs/_investigation/t5_cleanup_inventory.md`
Phase 1 (live-silver doc fixes + dead helpers) is DONE + verified. Remaining, each its OWN verified commit (rhythm: grep-verify-each-symbol → compile → `rerun-silver ea085d50` Pass-3 unchanged → `bench` → commit):
1. **db_writer dead methods** — `save_ball_detections`/`save_player_detections`/`save_match_analytics` (0 callers confirmed; contiguous lines 82–260). The D1 off-court-x drop in `save_player_detections` is already-wiped/dead — the LIVE D1 filter is in `bronze_export`+`bronze_ingest_t5` (north_star scorecard). Safe.
2. **swing_type_events dead path** — remove the `detect_swing_types_for_task` block in `upload_app._do_ingest_t5` (~2570) + `stroke_classifier/detector_v2.py` + the `swing_type_events` DDL. No-op (ImportError on Render / weights-gated locally), no consumer (silver uses `stroke_class`). Touches prod ingest → daylight, careful.
3. **v1 stroke_classifier** (`stroke_classifier/model.py`,`train.py`,`flow_extractor.py`,`export_training_data.py`) + harness `export-stroke-data`/`train-stroke` CLI — superseded by v2.
4. **TrackNet V3 scaffold** (`config.py` V3 block + `tracknet_v3.py` + Dockerfile COPY) — "keep WASB" is locked.
5. **SAHI_BATCHED prototype** (`player_tracker._run_sahi_batched`+`_tile_offsets`) — confirmed dead (job-def `SAHI_BATCHED=0`). Batch-side → effective on next detection rebuild.
6. **Bounce-driven retirement — LAST, and ONLY after stroke-driven runs on a REAL upload.** It removes the rollback. Unlocks deleting `_t5_pass1_load_bounce_driven` + near/far/any/kp buckets + mirror + `_min_player_distance_m`/proximity guard + `VOLLEY_NET_DISTANCE_M` + `_lookup_dominant_hand`/`is_left_handed` + keypoint compaction.
- **KEEP (not legacy):** `hit_model/` (research, unwired by design), `serve_model/` (LIVE, default-on — docs saying "gate not met" are stale), `build_silver_practice.py` (live dev-only), `point_structure/` (diag uses it), the armed bounce/swing stopgaps. Tier-3 perf flags PIPELINE_STAGE_OVERLAP=1 + MOG2_DOWNSCALE=4 are ADOPTED (keep rollback branches).

### B) Verify on the next REAL upload (the governing gate)
On the next `tennis_singles_t5` upload (full re-ingest, not local re-fire): confirm `stroke_events.ball_hit_location_x/y`+`hitter_side_near`+`volley` populate, `player_identity_segments` populates, silver player_id is stable A/B, Pass-3 holds. THEN accuracy/deploy decisions unlock.

### C) Training (the next big phase — Tomo has questions; don't start until A+B done)
GPU only. `submit_train_job --fact {serve|hit|bounce|swing}` (job-def rev 3, scale-to-0). Identity has no trainer (rule v1). Bounce recall is highest-leverage (gates bounce AND volley accuracy). Re-bench swing on GPU via `batch_train --bench swing` (recipe in `.claude/training_harness_status.md`). Deploying retrained serve/bounce/swing weights = rule-#8 detection-image rebuild; hit_model needs prod-inference WIRING first (no deploy surface today).

---

## HELD / train-last items (after A+B)
- swing `other` class weak (F1 0.591); serve far over-emission (336 far events on ea085d50, inherited verbatim — train, not silver filter); bounce recall low (119/407 — gates volley too); overnight retrained weights in S3 `_latest` UNDEPLOYED (bounce dropped precision at thr 0.70 → re-sweep before deploy; deployed at 0.85).

## Corpus / training env
- `ml_analysis.training_corpus` = 11 SA↔T5 pairs, all 3 label kinds; 3 most recent (Jun 14–15: `93ebb93d`,`7d3e2392`,`ea085d50`) are sharp-far re-runs (only `ea085d50` on rev-80). More sharp-far uploads = the accuracy lever.
- Training: GPU Batch proven; `ten-fifty5-ml-train:3` (eu-north-1 only — no us failover). Image lags main by commits but trainer code unchanged (rebuild only on a TRAINER change). Local CPU cannot train/bench torchvision.

## Key docs
- `docs/north_star.md` §RULES + the BRONZE-BUILD-COMPLETE banner · `docs/_investigation/bronze_silver_18_audit.md` (Definition-of-Bronze-Complete) · `docs/_investigation/t5_cleanup_inventory.md` (sprint playbook) · `.claude/training_harness_status.md` (5-fact gates + GPU bench recipe) · `.claude/training_environment.md` (how to train).

---
**END OF PICKUP**
