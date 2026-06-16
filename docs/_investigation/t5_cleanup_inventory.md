# T5 dead/legacy-code inventory — hardening-sprint plan

> **STATUS 2026-06-16: Render-side cleanup COMPLETE — every provably-dead Render-side symbol removed (commits 1f55b9b/0764df0/3f9c270/cc57ea3 + db_writer). No safe Render-side delete remains. Residual = Batch-side disk-only v1 weights (DEFER to a daylight Docker rebuild) + the HELD bounce-driven silver rollback. Verified by read-only audit.**

**Status:** REFERENCE / sprint backlog. Compiled 2026-06-15 from a 4-agent read-only
audit of the whole T5 surface (detection core, silver builders, model/train/diag
modules, ingest/routing/bronze-carry/ops). **Recommendations only — nothing has been
deleted.** Execute as a deliberate sprint with bench/verify gates per item; T5 is
churn-sensitive (silver Pass-1 + upload_app are load-bearing).

Each item: confidence (HIGH = provably unreferenced) · removal-risk · recommendation.

## ★ SPRINT PROGRESS

**SPRINT COMPLETE 2026-06-16 (Render-side).** A read-only audit confirms every
provably-dead Render-side symbol has been removed across commits `1f55b9b`,
`0764df0`, `3f9c270`, `cc57ea3`, and a `db_writer` commit. No safe Render-side
delete remains. The only residual items are Batch-side disk-only v1 weights
(DEFER — Docker rebuild + daylight, rule #8) and the deliberately-HELD bounce-driven
silver rollback (KEEP until stroke-driven is proven on a fresh real upload).

- **DONE (`1f55b9b`, verified compile + silver-rebuild ea085d50 unchanged + serve bench green):**
  Tier 1 doc fixes in `build_silver_match_t5.py` (the WRONG `_t5_pass1_load` docstring,
  the stale `T5_SERVE_FROM_EVENTS` log string + volley-stopgap comment) + Tier 0 dead
  helpers in that file (`_is_in_service_box`+`SERVE_BOX_TOLERANCE_M`, `SERVE_GAP_S`,
  `_parse_keypoints`). **The live-silver-path confusion sources are now fixed.**
- **DONE (`1f55b9b`/`0764df0`/`3f9c270`/`cc57ea3` + db_writer commit):** the 3 `db_writer`
  methods (`save_ball_detections`/`save_player_detections`/`save_match_analytics`);
  Tier 2 #2 `swing_type_events` dead path; Tier 2 #3 v1 `stroke_classifier` scaffold;
  Tier 2 #4 TrackNet V3 scaffold; Tier 3 `SAHI_BATCHED` prototype.
- **KEEP (not dead / held):** `_t5_pass1_load_bounce_driven` (HELD rollback until
  stroke-driven proven on a fresh real upload); `point_structure/point_boundaries.py`
  (used by `diag/audit_points.py`); `tracknet_v2_finetuned.pt` (train output path);
  `INGEST_REPLACE_EXISTING`/alias env chain (active fallback, can't prove dead);
  `player_tracker._run_yolo_court_crop`/`_run_yolo_far_baseline` (local-dev SAHI-disabled
  fallback); all env-gated rollback flags.
- **DEFER to Tomo (Batch-side, rule #8 — Docker rebuild + daylight only, low value /
  disk-only):** orphaned v1 weight files in `models/` (`bounce_detector_v1*.pt`,
  `swing_classifier_v1.pt`) — deleting changes the Docker `models/` COPY payload.

---

## TIER 0 — Zero-risk deletes (provably dead, no decision needed)

| Item | Evidence | Risk |
|---|---|---|
| ✅ **DONE** — `db_writer.save_ball_detections` / `save_player_detections` / `save_match_analytics` | 0 live callers repo-wide; Batch writes via S3 export → Render re-ingest (memory `feedback_probes_no_main_ball_in_ml_analysis`). NOTE: `save_player_detections` carries a D1 off-court-x drop — if still wanted it must move to `bronze_ingest_t5` first. REMOVED (db_writer commit). | LOW |
| ✅ **DONE** — `build_silver_match_t5._is_in_service_box` + `SERVE_BOX_TOLERANCE_M` | function 0 call sites; constant used only inside it. REMOVED (`1f55b9b`). | LOW |
| ✅ **DONE** — `build_silver_match_t5.SERVE_GAP_S` | 0 references (serve is bronze import now). REMOVED (`1f55b9b`). | LOW |
| ✅ **DONE** — `build_silver_match_t5._parse_keypoints` | 0 call sites in file (swing-inference callers deleted; stroke_detector has its own copy). REMOVED (`1f55b9b`). | LOW |
| `build_silver_match_t5` `dets_by_pid` / `raw_by_pid` bucket | built + returned but read by NEITHER Pass-1 path (docstring claim is stale) | LOW |
| `build_silver_match_t5._lookup_dominant_hand` call + `is_left_handed` var | computed but never read even in the bounce-driven path (swing inference deleted). The practice builder has its OWN copy — deleting here doesn't affect practice. | LOW |
| Local gitignored scratch: `training/datasets/match_90ad59a8*`, `swing_type_v1`/`v2*`, `training/visual_debug/`, `diag/visual_debug/`, `_dataset_cache/`, `training/labels/` | 0 tracked files; no live code reads them. Keep only `swing_type_v3_4class`. | LOW (disk only) |

## TIER 1 — Doc/comment fixes (no behaviour change, do alongside Tier 0)

| Item | Fix |
|---|---|
| `build_silver_match_t5._t5_pass1_load` docstring (~1280–1288) | **ACTIVELY WRONG** — says "BOUNCE-DRIVEN IS THE LIVE PROD PATH… stroke-driven MUST stay off." Stroke-driven is default-ON. Rewrite. |
| `T5_SERVE_FROM_EVENTS` stale refs | flag deleted 2026-06-07; stale log string `build_silver_match_t5:1003` ("T5 serve overlay (T5_SERVE_FROM_EVENTS)") + comments at `:110,858` and `build_silver_v2:493,542-547`. Fix string; prune comments. |
| `VOLLEY_NET_DISTANCE_M` "interim stopgap" docstrings (`:218,1269`) | stale — the volley bronze fact landed 2026-06-15; silver reads `stroke_events.volley` verbatim. Update. |
| `CLAUDE.md` "point_structure shared by silver builders" | FALSE — `point_boundaries.py` is imported only by `diag/audit_points.py`, never by the silver builders (they use `build_silver_v2` pass-3 SQL). Correct the claim. |
| `serve_model` "gate not met / not in Batch image" (CLAUDE.md README) | STALE — `serve_model` is LIVE: `SERVE_MODEL_ENABLED` default-on (`serve_detector/detector.py:544`), Batch infer stage runs (`__main__.py:465`), validated far 3/12→7/12. Update doc. |
| `SWING_CLASSIFIER_ENABLED` code default vs deployed | code default `1` (`pipeline.py:646`); rev-80 job-def set `=1` (deploy script) but some docs say `0`. Reconcile docs to rev-80 reality; consider matching the code default to deployed to kill the foot-gun. |
| bounce provenance label (`bounce_detector/models.py:27`, `detector.py:530`) | says `bounce_detector_v1` while v2 weights run — provenance bug, fix label to v2. |
| `docs/env_vars.md` | add the 6 undocumented Batch perf-lever flags (SAHI_BATCHED, PIPELINE_STAGE_OVERLAP, MOG2_DOWNSCALE, SAHI_SKIP_A_FAR_YMAX, ROI_BOUNCE_BATCH, BALL_BATCH_SIZE). |

## TIER 2 — Decision-gated removals (need Tomo's call, then a flag-gated cycle)

1. ⏸️ **KEEP / HELD** — **Retire the bounce-driven silver path** — the big win. `_t5_pass1_load_bounce_driven` is now the dormant fallback (`T5_STROKE_DRIVEN_SILVER=0` or no stroke_events). Retiring it (once all tasks are re-ingested with stroke_events) unlocks deletion of: the near/far/any/kp bucket construction in `_build_player_buckets`, the mirror fallback, the A4 keypoint patch, keypoint compaction (`_kps_to_array`), `_min_player_distance_m` + proximity guard + `BOUNCE_PLAYER_PROXIMITY_M`, `VOLLEY_NET_DISTANCE_M`, `_lookup_dominant_hand`. **Risk MED** (loses the env rollback) → keep as flag a cycle, delete after the stroke-driven path is proven on real uploads.
2. ✅ **DONE** — **`swing_type_events` dormant parallel path** — `detect_swing_types_for_task` (`upload_app.py:2570–2580`) → `stroke_classifier/detector_v2.py` → `ml_analysis.swing_type_events`. Writes NOTHING (Render ImportError; locally weights-gated to stopgap) and the table has NO consumer (silver reads `player_detections.stroke_class`, the Batch path). Remove the ingest block + `detector_v2.py` + the `swing_type_events` DDL together. **Risk LOW-MED** (already a no-op; lives in prod ingest → daylight commit). *(Correction to earlier note: it's empty because of the weights gate, not the training import — that import is lazy/guarded.)*
3. ✅ **DONE** — **v1 stroke_classifier** (`stroke_classifier/model.py`, `train.py`, `flow_extractor.py`, `export_training_data.py`) + the only callers `harness.py` `export-stroke-data`/`train-stroke` — superseded by v2 (`model_v2`/`inference_v2`); `__init__.py:27` itself says retire to `_legacy/`. **Risk MED.**
4. ✅ **DONE** — **TrackNet V3 scaffold** (`config.py` V3 block + `TRACKNET_V3_*` + `tracknet_v3.py` + its Dockerfile COPY) — `tracknet_v3.pt` not on disk; "keep WASB" is a locked decision (memory `project_ball_tracker_decision_keep_wasb`). **Risk LOW.**
5. ⏭️ **DEFER to Tomo (Batch-side, rule #8)** — **Orphaned model weights** in `models/` (gitignored, bloat the wholesale Docker COPY): `bounce_detector_v1*.pt` (4), `swing_classifier_v1.pt`. Deleting changes the Docker `models/` COPY payload → Docker rebuild + daylight only, low value (disk-only). **`tracknet_v2_finetuned.pt` is KEEP** (train output path, not orphaned). **Risk LOW (disk).**
6. ⏸️ **KEEP** — `INGEST_REPLACE_EXISTING` aliases `DEFAULT_REPLACE_ON_INGEST` / `STRICT_REINGEST` — active fallback chain, can't prove dead. Retain. **Risk LOW, cosmetic.**

## TIER 3 — CONFIRM-then-decide (blocked on one fact: the live job-def env dump)

The single highest-leverage unknown: **the env vars actually set on the current eu/us detection job-defs** (the `.claude/tmp/*` scratch files conflict with `docs/env_vars.md`). Pull it from AWS. It decides:

- ✅ **DONE** — **SAHI_BATCHED** confirmed not adopted → `_run_sahi_batched` + `_tile_offsets` (~130 lines, `player_tracker.py`) REMOVED (dead prototype).
- If **PIPELINE_STAGE_OVERLAP** is not adopted → the MOG2 worker-thread machinery (`_mog2_executor`, overlap branches, `_make_motion_mask`) is dead.
- If **MOG2_DOWNSCALE=1** (default) → the downscale branch in `_apply_mog2` is dead.
- ⏸️ **KEEP** — `SAHI_ENABLED` hardcoded `True` → the legacy 3-pass branch (`player_tracker.py:535–559`) + `_run_yolo_far_baseline` + `_run_yolo_court_crop` are the local-dev SAHI-disabled fallback. Retained (active fallback path).

## KEEP — intentionally unwired / dormant-correct (NOT cleanup targets)

- `hit_model/` — train-only/bench-only research line, locked `bench_baseline_hit.json`, gate-not-met (deliberately not in detection image).
- `serve_model/` — actually LIVE (default-on); only the docs are stale.
- bounce/swing armed-but-untrained CNN stopgaps — accurate STOPGAP markers, not stale.
- `build_silver_practice.py` — live, dev-only (serve_practice/rally_practice routing active).
- `point_structure/` — used by `diag/audit_points.py`; keep (just fix the "shared by silver" doc claim).
- `stroke_class` bronze-carry (`bronze_export`/`bronze_ingest_t5`) — currently NULL (classifier gated) but silver reads it; architecturally correct, goes live when the classifier re-enables.
- The new `stroke_events` columns (`ball_hit_location_x/y`, `hitter_side_near`, `volley`) are correctly NOT in the bronze-carry (Render-side writes, post-ingest).

## Suggested sprint order
1. Tier 0 deletes + Tier 1 doc fixes (one safe commit, bench green).
2. Pull the live job-def env → resolve all Tier 3 in one pass.
3. Tier 2 #2 (swing_type_events dead path) + #3 (v1 stroke_classifier) + #4 (TrackNet V3) — each its own commit.
4. Tier 2 #1 (retire bounce-driven) LAST — biggest, flag-gated, after stroke-driven is proven on real uploads.
