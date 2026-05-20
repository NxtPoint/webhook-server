# Dual-Submit Pipeline — Status & Build Plan (2026-05-20)

**Audience:** Tomo + future Claude sessions inheriting Phase 5c.
**Purpose:** Establish concretely what's built, what's missing, and what completion looks like for the dual-submit training-data pipeline. This is the **strategic moat** — every match recorded goes through both T5 and SportAI on the same video, giving free labeled training data.
**TL;DR:** ~60% of the code is built. The pipeline produces real T5/SA pairs and real labels. The blockers are **runtime ON/OFF (env var defaulted OFF)**, **no automation between pair-completion and label-export**, and **no corpus index** to know when the dataset is "ready enough" to retrain. Estimated total work to a fully automated loop: **3-5 working sessions** including this one. The market scan separately confirmed dual-submit dominates Roboflow ($7.5k-$15k for 10 matches) and is the only viable training-data path at our cost target.

---

## 1. What exists today (with file:line references)

### 1.1 The submit half — automated trigger

**`upload_app.py:230`** — env var declaration:
```python
AUTO_DUAL_SUBMIT_T5 = os.getenv("AUTO_DUAL_SUBMIT_T5", "0").lower() in ("1", "true", "yes", "y")
```

**`upload_app.py:1839` `_auto_dual_submit_t5(task_id)`** — fire-and-forget called from the SportAI ingest completion path. Behaviour:
- No-op if `AUTO_DUAL_SUBMIT_T5` is false (current default).
- Only triggers for `sport_type='tennis_singles'` (not practice, not technique, not already T5).
- Idempotent via `ml_analysis.video_analysis_jobs` lookup by `s3_key`.
- Errors swallowed — never propagates back to the SA flow.
- Calls `_manual_dual_submit_t5_core(s3_key, email, player_a_name, player_b_name)`.

**`upload_app.py:1899` `_manual_dual_submit_t5_core(s3_key, email, ...)`** — the shared core:
1. Idempotency check (skip if T5 job already exists for s3_key).
2. `_t5_submit(s3_key, sport_type='tennis_singles_t5')` — registers a new Batch job.
3. Creates a fresh `submission_context` row pointing at the same s3_key.
4. Returns the new t5_task_id.

**Triggered from two ingest paths** (`upload_app.py:2049` and `:2060`):
```python
threading.Thread(target=_auto_dual_submit_t5, args=(task_id,), daemon=True).start()
```

### 1.2 The submit half — manual retrigger

**`upload_app.py:3480` `POST /ops/dual-submit-t5`** — ops-key-protected manual trigger for historical SA tasks. Body: `{"sportai_task_id": "..."}`. Useful for retro-dual-submitting prod SA matches that completed before the auto-trigger was on.

**`upload_app.py:1963` `_manual_dual_submit_t5(sportai_task_id)`** — wraps the core, reads s3_key + player names from SA's submission_context. Returns `{status, t5_task_id, ...}`.

### 1.3 The label-export half — manual scripts

All in `ml_pipeline/training/`:

| File | Purpose | Output | Labels/match |
|---|---|---|---|
| `label_serve_bounces.py` | Labels SERVE bounces only — reads `silver.point_detail` filtered to `serve_d=TRUE`, projects bounce position to pixels via court homography. | JSON with `{hit_frame, bounce_frame_est, pixel_x, pixel_y, role, court_x, court_y}`. | ~23 |
| `label_ball_positions.py` | Labels **all** ball positions — reads `bronze.ball_bounce` (already pixel-normalised in `image_x/image_y`, with exact `frame_nr`). No homography needed. **7× more labels than the serve-bounce path.** | Same schema as above for downstream compatibility. | ~160 |
| `extract_frames.py` | Pulls only the JPEGs needed (label-frame ±1 for TrackNetV2's 3-frame sliding window). Saves to `frame_NNNNNN.jpg`. | Local dir of JPEGs. | n/a |
| `build_serve_bounce_dataset.py` | Wires (label JSON + video) pairs → 640×360 rescaled labels + `extract_frames` calls → final `labels.json + frames/`. Multi-match concatenation supported. | `<dataset>/labels.json` + `<dataset>/frames/`. | n/a |
| `tracknet_dataset.py` | Torch `Dataset` class — feeds (3-frame stack, heatmap) pairs to the trainer. | n/a (Python class) | n/a |
| `train_tracknet.py` | Fine-tunes `BallTrackerNet`: freezes encoder, trains decoder only, weighted BCELoss (×100 positive class), Adam lr=1e-4, 80/20 split. Saves best-by-val-loss to `ml_pipeline/models/tracknet_v2_finetuned.pt`. | `.pt` checkpoint. | n/a |
| `export_labels.py` | Generic exporter — `export_ball_labels()` from `ml_analysis.ball_detections`, `export_sportai_labels()` from SA `player_swing`. | JSON. | varies |

### 1.4 The stroke-classifier dual-submit (separate flow)

`ml_pipeline/stroke_classifier/export_training_data.py` is a **separate** dual-submit consumer for the far-player optical-flow CNN. It:
1. Loads SA hits with stroke labels from `silver.point_detail`.
2. Loads T5 far-player bboxes from `ml_analysis.player_detections`.
3. Aligns by timestamp (±1s window).
4. Extracts optical flow around each hit, writes labeled examples.

This was scaffolded but not run end-to-end — see auto-memory `project_far_player_stroke_research.md` ("awaiting dual-submit training data").

### 1.5 What labeled data exists right now (local)

```
ml_pipeline/training/labels/
├── 8a5e0b5e_ball_positions.json       ← ~160 labels from label_ball_positions.py
├── 8a5e0b5e_serve_bounces.json        ← ~23 labels from label_serve_bounces.py (v1)
└── 8a5e0b5e_serve_bounces_v2.json     ← updated v2

ml_pipeline/training/datasets/
├── match_90ad59a8/                    ← prepared frames + labels
└── match_90ad59a8_v2/

ml_pipeline/training/models/            ← DOES NOT EXIST. No finetuned weights produced yet.
```

**One match labeled. Zero finetuned models.**

The labeled match is `8a5e0b5e-58a5-4236-a491-0fb7b3a25088` (T5) paired with `2c1ad953-b65b-41b4-9999-975964ff92e1` (SA) — same pair referenced throughout north_star.md as the validation set.

---

## 2. The architecture as a diagram

```
                       VIDEO UPLOADED
                            │
                            ▼
                  upload_app.py → SportAI submit
                            │
                            ▼
                   SportAI ingest completes
                            │
                            ▼  (AUTO_DUAL_SUBMIT_T5=1)
                  _auto_dual_submit_t5(task_id)
                            │
                            ▼
              _manual_dual_submit_t5_core(s3_key)
                            │
                            ▼
         _t5_submit(s3_key, 'tennis_singles_t5')
                            │
                            ▼
                  AWS Batch T5 pipeline
                            │
                            ▼
              T5 ingest completes (separate s3 task_id)
                            │
                            ▼
   ┌────────────────────────┴────────────────────────┐
   │                                                 │
SA silver.point_detail                  T5 ml_analysis.* tables
SA bronze.ball_bounce                   T5 silver.point_detail (model='t5')
   │                                                 │
   └────────────────────┬────────────────────────────┘
                        │
                        ▼  ◄── ⚠️ GAP: nothing watches for "both completed"
                        │
                  MANUAL TRIGGER REQUIRED
                        │
            python -m ml_pipeline.training.label_ball_positions \
                --task <t5_tid> --sportai <sa_tid> \
                --output ml_pipeline/training/labels/<tid>_ball_positions.json
                        │
                        ▼
              labels JSON written LOCALLY (not S3)
                        │
                        ▼  ◄── ⚠️ GAP: no corpus index
                        │
              python -m ml_pipeline.training.build_serve_bounce_dataset \
                --label-json ... --video ... --output-dir ...
                        │
                        ▼
                 dataset/ LOCAL
                        │
                        ▼  ◄── ⚠️ GAP: training is local-only; no GPU box yet
                        │
              python -m ml_pipeline.training.train_tracknet ...
                        │
                        ▼
              .pt file LOCAL → manual copy to ml_pipeline/models/
                        │
                        ▼  ◄── ⚠️ GAP: no model versioning, no A/B vs baseline
                        │
                Docker rebuild + ECR push + job-def revision
                        │
                        ▼
                   Production T5 uses new weights
```

Three explicit gaps marked. None of them are large code-wise; the missing piece is **glue + orchestration**.

---

## 3. What's missing — the gap list

| Gap | What's missing | Why it matters | Engineering size |
|---|---|---|---|
| **G1** | `AUTO_DUAL_SUBMIT_T5` is OFF in prod | Every SA match since this code shipped silently bypassed the dual-submit. Free labeled data we already paid SportAI for is being lost daily. | **30 min** — flip Render env var to `1`. Already gated to `tennis_singles`. Idempotent. |
| **G2** | No retro backfill of pre-flag matches | Months of SA matches in prod could be retroactively dual-submitted. The ops endpoint exists; nothing has scripted it across all eligible task_ids. | **2 hours** — write `ops/dual-submit-t5-backfill` endpoint or one-off script that iterates `bronze.submission_context WHERE sport_type='tennis_singles'` and calls `_manual_dual_submit_t5` per row. Costs Batch credits per match (~$0.15 spot G4dn each). |
| **G3** | No "pair completed" signal | After both pipelines complete on the same s3_key, nothing fires a labeling job. Has to be manually invoked per pair. | **3-4 hours** — `gold.vw_dual_submit_pairs` view (SA task + T5 task + completion status + has-labels flag); a hook in `_do_ingest_t5` end-of-flow that calls `label_ball_positions.py` for the matching SA pair if found. |
| **G4** | Labels live locally, not in S3 | Means training has to run from a checkout that has the JSON files. If GPU box is provisioned (Stream A), labels need re-exporting there or syncing. | **1 hour** — change `label_ball_positions.py --output` to accept `s3://` prefix; write to `s3://nextpoint-prod-uploads/training/labels/<tid>.json`. |
| **G5** | No corpus index | Currently you only know `ls ml_pipeline/training/labels/` what's labeled. No structured "we have N matches with M total labels, here's per-match counts." | **2 hours** — `ml_analysis.training_corpus` table (label_s3_key, video_s3_key, t5_task_id, sa_task_id, label_count, role_breakdown, created_at, validated_at, used_in_models JSONB); `INSERT` from the auto-label hook in G3. |
| **G6** | No automated dataset assembly | `build_serve_bounce_dataset.py` is invoked manually with explicit label + video paths. | **2 hours** — `harness build-corpus` subcommand that reads `ml_analysis.training_corpus`, picks all completed labels, pulls frames from S3-cached videos, builds the multi-match dataset. |
| **G7** | No GPU box for training | `train_tracknet.py` can run on CPU but is slow. Local Windows machine without a GPU adds hours per epoch. | **Stream A of this session** — g4dn.xlarge dev box already in plan. Once provisioned, point `train_tracknet.py` at S3-backed corpus. |
| **G8** | No model versioning / experiment tracking | `tracknet_v2_finetuned.pt` overwrites itself every run. No record of hyperparams, training corpus, val metrics. | **3-4 hours** — W&B (free tier 100 GB) integration in `train_tracknet.py`; model artifacts saved to S3 with `<corpus_hash>_<wandb_run_id>.pt` naming. **Or** if W&B is too much: just `s3://.../models/` with a `versions.jsonl` log. |
| **G9** | No A/B vs baseline before promotion | Promoting `tracknet_v2_finetuned.pt` to prod = full Docker rebuild and Batch image push. Need a "this finetuned model beats baseline on bench" gate before promotion. | **4-6 hours** — extend `bench.py` to take a `--weights-path` arg; run prod-current weights AND candidate weights on all fixtures; require candidate ≥ baseline on every fixture. Promote = update `ml_pipeline/models/tracknet_v2.pt` (or `_finetuned.pt`) symlink + commit + Docker rebuild + ECR push + job-def revision (per `.claude/handover_t5.md` BATCH-SIDE CHANGE CHECKLIST). |
| **G10** | Stroke-classifier dual-submit never run end-to-end | `stroke_classifier/export_training_data.py` exists but has no inbox of pair-completed events to consume. Same fix as G3 + a second labeling target. | **2 hours** marginal on top of G3 — add stroke-classifier label export as a second hook target. |

**Cumulative size:** G1+G2 (start now, no design needed): **2.5 hr**. G3+G4+G5+G6 (the automation core): **8 hr**. G7 (covered by Stream A): already in plan. G8+G9+G10 (the polish loop): **9-12 hr**.

**Total to fully automated loop including this session's GPU box: ~3-5 working sessions.**

---

## 4. Concrete build plan

### Phase 5c.0 — Turn it on (today, 30 min)
1. Confirm `_auto_dual_submit_t5` is safe to enable in prod (it is — idempotent, fire-and-forget, error-swallowed).
2. Set `AUTO_DUAL_SUBMIT_T5=1` on Render's main API service.
3. Verify next SA upload spawns a paired T5 job. Check `ml_analysis.video_analysis_jobs` for the new t5_job_id matching the s3_key.

**Done when:** one new SA upload triggers a paired T5 ingest end-to-end.

### Phase 5c.1 — Retro backfill (1 session, ~2 hr code + ~1 hr Batch run)
1. Query `bronze.submission_context WHERE sport_type='tennis_singles' AND deleted_at IS NULL` joined against existing T5 jobs to find unpaired SA tasks.
2. Sanity check: list shows N tasks. Discuss whether all are eligible (some old s3_keys might not exist; check before iterating).
3. Build a script (root-level or `cleanup/`) that iterates and calls `_manual_dual_submit_t5(task_id)` with a delay (or rate limit) to keep Batch queue depth manageable.
4. Spot prices in eu-north-1 mean ~$0.12-0.15 per match. Budget the run and confirm with Tomo before kicking off.

**Done when:** every eligible historical SA task has a paired T5 task in `ml_analysis.video_analysis_jobs`.

### Phase 5c.2 — Pair-completion hook + corpus index (1 session, 4-6 hr)
1. Add `gold.vw_dual_submit_pairs` view in `gold_init.py`:
   ```sql
   SELECT sa.task_id  AS sa_task_id,
          t5.task_id  AS t5_task_id,
          sa.s3_key,
          sa.status   AS sa_status,
          t5.status   AS t5_status,
          (sa.status='completed' AND t5.status='completed') AS pair_complete
   FROM bronze.submission_context sa
   JOIN ml_analysis.video_analysis_jobs t5 ON sa.s3_key = (SELECT s3_key FROM bronze.submission_context WHERE task_id = t5.task_id)
   WHERE sa.sport_type = 'tennis_singles' AND ...
   ```
   (Schema TBD — `s3_key` may be in `submission_context.meta_json` not a column; check before writing the view.)
2. Add `ml_analysis.training_corpus` table in `db_init.py::bronze_init()`:
   ```sql
   CREATE TABLE IF NOT EXISTS ml_analysis.training_corpus (
       id BIGSERIAL PRIMARY KEY,
       sa_task_id UUID NOT NULL,
       t5_task_id UUID NOT NULL,
       label_kind TEXT NOT NULL,             -- 'ball_position' | 'serve_bounce' | 'stroke_classifier'
       label_s3_key TEXT NOT NULL,           -- s3://nextpoint-prod-uploads/training/labels/<tid>_<kind>.json
       video_s3_key TEXT NOT NULL,           -- s3://nextpoint-prod-uploads/wix-uploads/<name>.mp4
       label_count INT NOT NULL,
       role_breakdown JSONB,                 -- {'NEAR': 12, 'FAR': 11} for serve bounce; null for ball_position
       created_at TIMESTAMPTZ DEFAULT now(),
       validated_at TIMESTAMPTZ,             -- nullable until a Claude or human checks it
       used_in_models JSONB,                 -- ['tracknet_v2_finetuned_2026-05-25.pt', ...]
       UNIQUE (sa_task_id, t5_task_id, label_kind)
   );
   ```
3. Hook into `upload_app.py::_do_ingest_t5` end-of-flow: after `notify_ses_ses_sent`, if the SA pair exists and pair_complete is now true, fire a thread that:
   a. Calls `label_ball_positions(t5=this, sa=pair)` with `--output s3://...`.
   b. Records the new row in `ml_analysis.training_corpus`.
   c. (Optional) Calls `label_serve_bounces` similarly.
4. Backfill: after Phase 5c.1, run the hook manually for all existing pair_complete=true pairs.

**Done when:** `ml_analysis.training_corpus` has ≥10 rows for `label_kind='ball_position'` ready to train.

### Phase 5c.3 — Corpus → dataset → training, on GPU box (1 session, 4-6 hr)
1. Add `harness build-corpus --output s3://.../datasets/<name>/` subcommand that:
   a. Reads `ml_analysis.training_corpus WHERE label_kind='ball_position' AND validated_at IS NOT NULL` (or all if no validation gate yet).
   b. For each row, pulls the label JSON + video from S3.
   c. Runs `build_serve_bounce_dataset.py` logic to assemble combined dataset.
   d. Writes the dataset to S3.
2. On the GPU box (Stream A): `train_tracknet.py --frames-dir s3://.../datasets/<name>/frames --labels s3://.../datasets/<name>/labels.json --output-s3 s3://.../models/`
3. Output: a versioned `.pt` in S3 with a manifest JSON of training corpus + val metrics.

**Done when:** one finetuned weights checkpoint exists in S3 with a manifest.

### Phase 5c.4 — Gate before promotion (1 session, 4-6 hr)
1. Extend `bench.py`: `--weights-path s3://.../models/<name>.pt` swaps the BallTracker weights for the bench run.
2. New `ml_pipeline/diag/bench_finetuned.py` script: runs bench on baseline AND candidate, reports delta per fixture.
3. Promotion gate: only update `ml_pipeline/models/tracknet_v2.pt` (committed via Docker COPY) if the candidate beats baseline on **every** fixture.
4. Document the promotion playbook in `.claude/handover_t5.md`.

**Done when:** one finetuned model has been bench-validated AND promoted to production AND a Batch run on `880dff02` shows improved ball coverage.

### Phase 5c.5 — Stroke classifier (1 session, 2-4 hr — can run in parallel with 5c.4)
1. Apply the same pair-completion hook (G3 extended) to also call `stroke_classifier/export_training_data.py`.
2. Train stroke classifier on the GPU box with the assembled optical-flow corpus.
3. A/B vs current stroke derivation in `build_silver_v2.py`.

**Done when:** far-player stroke classifications exist as a separate `model='t5_stroke_cnn'` column in silver.

---

## 5. Cost & timing reality check

- **Per dual-submit Batch run:** ~$0.12-0.15 on Spot G4dn.xlarge (eu-north-1). 50 historical matches retro-dual-submitted ≈ **$6-8**.
- **GPU box for training:** g4dn.xlarge ≈ $0.526/hr on-demand. Training takes ~2-4 hrs for a fresh finetune over 10 matches → ~$1-2 per training run. Spot cuts to $0.20/hr if we want to push it.
- **Storage:** ~50 MB / labeled match (frames JPEG + labels JSON), so 50 matches ≈ 2.5 GB. Negligible on S3.
- **W&B free tier:** 100 GB storage, unlimited collaborators on the team plan. Probably enough.

**Schedule:** if Tomo dedicates the next 3-5 working sessions to Phase 5c (one for "turn it on + retro backfill", one for "pair-completion hook + corpus", one for "build-corpus + first training run", one for "bench gate + promotion"), the loop is live in 3-5 weeks of calendar time at current cadence.

---

## 6. Risk register

1. **Training data quality** — SA's labels are not perfect. `bronze.ball_bounce` includes pre-/between-point bounces (the same phantom-bounce class Phase 1 closed for serve detection). Filtering these out at label-export time is non-trivial; without filtering, the trainer may learn to predict non-rally ball positions.
   - **Mitigation:** Add a `bronze.ball_bounce.is_rally` gate via existing serve_events + rally state machine before labeling. Adds complexity.
2. **Catastrophic forgetting** — fine-tuning the TrackNetV2 decoder may overfit to our camera angle and lose generalisation if a user uploads from a different vantage point.
   - **Mitigation:** Hold out 2 matches as a "diverse cameras" validation set. Only promote if both held-out matches improve too.
3. **WASB-tennis weights drop-in (from market scan §2)** may simply replace the need for finetune entirely.
   - **Implication:** **Try WASB first**, before investing in the full 5c.3-5c.5 chain. If WASB hits 86% F1 out of the box, the corpus pipeline's primary justification (finetune TrackNetV2/V3) collapses. The pipeline is still useful for **stroke classifier** training and for **TOTNet-style occlusion-aware retraining** if WASB plateaus.
4. **Batch cost growth** — auto-dual-submit doubles Batch cost per upload. At current volumes this is negligible, but if T5 ever serves real customers in parallel with SportAI, the dual-submit needs a fair-use cap.

---

## 7. Open questions for Tomo

1. **Is retro backfill worth the Spot cost?** Estimate of how many historical SA `tennis_singles` matches exist in prod. If <100, the $15 spend is trivial. If >1000, gate carefully.
2. **Validate before training?** Should there be a manual validation step (Claude or Tomo eyeballs labels per match before they go into the training corpus), or trust SA + filter heuristically?
3. **Try WASB first or train V3 first?** Market scan strongly suggests WASB is the cheap win. Pivot 5c.3 to "drop in WASB + measure" before "build training pipeline + finetune V2"?
4. **GPU box lifecycle:** keep `t5-dev-gpu` running 24/7 (~$380/mo) or start/stop per session ($1-2/session + 5 min boot)? For Phase 5c.3+ the start/stop model is fine if training runs are bounded.
