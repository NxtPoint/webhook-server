# Next-session pickup — 2026-06-15 EOD — ✅ BRONZE BUILD COMPLETE · SILVER 100% VERBATIM · 5/5 BENCHES LOCKED · **HARDENING/CLEANUP SPRINT DONE** (one item held, one deferred). main @ `cc57ea3` (+).

## ⚡ Executive summary (read first — 60 seconds)
**The build is DONE and the hardening/cleanup sprint is DONE.** All base facts come from models → silver Pass-1 projects them VERBATIM → 5/5 training benches locked → GPU training env proven. This session finished the cleanup sprint (`docs/_investigation/t5_cleanup_inventory.md`). The ONLY things left: (1) **TRAIN** (the next big phase — Tomo has questions), and (2) the two cleanup items that are deliberately NOT done yet (see "Cleanup — what's left" below).

**main @ `cc57ea3`.** Detection image eu rev 80 / us rev 61. Training job-def `ten-fifty5-ml-train:3`. Serve bench floor GREEN (`ea1e500c=12/26`, `880dff02=23/24`).

**⚠️ THE ONE CAVEAT THAT GOVERNS EVERYTHING:** every bronze/silver change was verified on **`ea085d50` only**. **The next REAL T5 upload (full re-ingest) is the true proof.** Until then: don't trust accuracy as "moved," don't deploy retrained weights, and DON'T retire the bounce-driven rollback.

**⚠️ TWO BATCH-SIDE cleanup commits ride the next detection rebuild (rule #8):** `f11b8ac` (bounce provenance v1→v2 tag) and `cc57ea3` (TrackNet V3 + SAHI_BATCHED removal). They're dead-code/label-only (no behaviour change, serve bench green, imports verified) but they touch Batch-bundled files (`ball_tracker.py`, `player_tracker.py`, `config.py`, `Dockerfile`, `bounce_detector/`). **The next detection-image rebuild MUST include them** (the Dockerfile lost its `tracknet_v3.py` COPY, so a stale rebuild would be fine, but a rebuild from an OLD checkout would re-add the file — rebuild from `main`).

**⚠️ Parallel marketing session is live** on `frontend/`, `locker_room_app.py`, `marketing_app.py`, `build_blog.py`, `CLAUDE.md` (marketing rows), `MEMORY.md`. Commit only T5 files by explicit path; stay out of those.

---

## What shipped THIS session (cleanup sprint — all on origin/main, T5 only)
1. `7854d91` removed 3 dead `db_writer` methods (`save_ball/player/match_*`, 0 callers; Batch writes via S3 re-ingest)
2. `b1f8b3f` dropped dead `dets_by_pid`/`raw_by_pid` bucket + `_lookup_dominant_hand` in `build_silver_match_t5` (rerun-silver ea085d50 = 821 rows, no-op)
3. `5207c7c` fixed stale `T5_SERVE_FROM_EVENTS`/volley comments + `serve_model` doc + documented 6 Batch perf levers in `env_vars.md` (CLAUDE.md serve_model/stroke_classifier/point_structure rows landed in marketing's `3a46fa7`)
4. `f11b8ac` **(BATCH-SIDE)** bounce provenance tag `bounce_detector_v1`→`v2` to match deployed weights
5. `0764df0` removed dead `swing_type_events` path (`detector_v2.py` + `db.py` + `upload_app` boot/ingest blocks; silver reads `stroke_class` from the live Batch `inference_v2` path)
6. `3f9c270` removed v1 `stroke_classifier` scaffold (`model/train/flow_extractor/export_training_data` + harness `export-stroke-data`/`train-stroke`; superseded by v2)
7. `cc57ea3` **(BATCH-SIDE)** removed inert TrackNet V3 scaffold (collapsed `ball_tracker.py` to V2-only, deleted `tracknet_v3.py` + Dockerfile COPY) + SAHI_BATCHED prototype (`player_tracker._run_sahi_batched`/`_tile_offsets`/`_nms_numpy`, ~173 lines)

Rhythm followed per item: grep-verify-each-symbol → compile/import → (silver: rerun-silver ea085d50 unchanged) → serve bench green → small verified commit.

## Cleanup — what's left (the inventory is otherwise fully executed)
- **HELD (do NOT do until a real upload proves stroke-driven):** Tier 2 #1 — retire the bounce-driven silver path (`_t5_pass1_load_bounce_driven` + near/far/any/kp buckets + mirror + `_min_player_distance_m`/proximity guard + `VOLLEY_NET_DISTANCE_M` + keypoint compaction). It removes the `T5_STROKE_DRIVEN_SILVER=0` rollback — gated on Section B below.
- **DEFERRED (env-knob risk I couldn't clear locally):** Tier 2 #6 — trim the `INGEST_REPLACE_EXISTING`/`DEFAULT_REPLACE_ON_INGEST`/`STRICT_REINGEST` aliases (upload_app + ingest_worker). They could be set in the live Render env (not in the repo); removing a lookup would flip replace behaviour. **Do this with the job-def/Render env dump in hand** (same Tier-3 dependency). The constant `DEFAULT_REPLACE_ON_INGEST` and default `"1"` are fine; only the redundant env aliases are the target.
- **N/A:** Tier 2 #5 orphaned weights (`bounce_detector_v1*.pt` etc.) are gitignored disk-only — delete on the box if reclaiming space; not a code change.
- **bench_ball validation:** full run can't complete locally — `ml_pipeline/test_videos/880dff02_*.mp4` is absent (only `a798eff0_sa_video.mp4` is present). Validated the V3-removal ball path with `bench_ball --fixture a798eff0` [RESULT: see session note] + the V2 path is byte-identical in the diff + imports verified. The full 2-fixture bench_ball + the next Batch rebuild are the remaining gates.

---

## NEXT SESSION — do these in order

### A) (optional) finish the two leftover cleanup items
Only if you have the live job-def/Render env dump: trim the INGEST_REPLACE aliases (deferred above). Otherwise skip — the sprint is effectively complete.

### B) Verify on the next REAL upload (the governing gate — unchanged)
On the next `tennis_singles_t5` upload (full re-ingest): confirm `stroke_events.ball_hit_location_x/y`+`hitter_side_near`+`volley` populate, `player_identity_segments` populates, silver player_id is stable A/B, Pass-3 holds, **and `player_detections.stroke_class` populates** (the live swing path). THEN the bounce-driven retirement (HELD item) + accuracy/deploy decisions unlock.

### C) Training (the next big phase — Tomo has questions; don't start until B is proven)
GPU only. `submit_train_job --fact {serve|hit|bounce|swing}` (job-def rev 3, scale-to-0). Bounce recall is highest-leverage (gates bounce AND volley accuracy). Re-bench swing on GPU via `batch_train --bench swing`. Deploying retrained serve/bounce/swing weights = rule-#8 detection-image rebuild — **fold the two pending Batch-side cleanup commits (`f11b8ac`,`cc57ea3`) into that same rebuild.**

---

## HELD / train-last items (after B)
- swing `other` class weak (F1 0.591); serve far over-emission (336 far events on ea085d50, inherited verbatim — train, not silver filter); bounce recall low (119/407 — gates volley too); overnight retrained weights in S3 `_latest` UNDEPLOYED.

## Corpus / training env
- `ml_analysis.training_corpus` = 11 SA↔T5 pairs; 3 most recent (Jun 14–15) are sharp-far re-runs (only `ea085d50` on rev-80). More sharp-far uploads = the accuracy lever.
- Training: GPU Batch proven; `ten-fifty5-ml-train:3` (eu-north-1 only). Local CPU cannot train/bench torchvision; **local bench_ball is also impractically slow + needs `test_videos/` mp4s present.**

## Key docs
- `docs/north_star.md` §RULES + BRONZE-BUILD-COMPLETE banner · `docs/_investigation/bronze_silver_18_audit.md` · `docs/_investigation/t5_cleanup_inventory.md` (sprint playbook — Phase 1 + the rest now executed) · `.claude/training_harness_status.md` · `.claude/training_environment.md`.

---
**END OF PICKUP**
