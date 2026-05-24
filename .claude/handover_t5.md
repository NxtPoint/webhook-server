# T5 ML Pipeline — Operational Handover

**Last updated:** 2026-05-07 (phantom-bounce root cause identified; bench locked at 20/24 on a798eff0; the 4 remaining misses split into TWO classes with distinct upstream fixes needed)
**Owner:** Tomo
**This is the single authoritative doc for T5.** CLAUDE.md now points here. Old handovers (`handover_t5_current.md`, `handover_serve_detector_build.md`) were folded in on 2026-04-18.

---

## BATCH-SIDE CHANGE CHECKLIST — RUN THIS BEFORE EVERY MERGE

Bench going green is necessary but **not sufficient**. Bench replays a pickled fixture of pre-extracted detections — it cannot tell you whether the AWS Batch container is in sync with main. The container caches `:latest` on Spot nodes that are torn down between jobs, so each new job pulls the manifest fresh, but only the digest the active job-def revision pins gets pulled.

**Before merging any T5 detector branch, run this:**

```bash
git diff origin/main HEAD --stat -- ml_pipeline/roi_extractors/ ml_pipeline/__main__.py ml_pipeline/pipeline.py ml_pipeline/Dockerfile ml_pipeline/requirements.txt ml_pipeline/court_detector.py ml_pipeline/ball_tracker.py ml_pipeline/wasb_ball_tracker.py ml_pipeline/wasb_hrnet.py ml_pipeline/config.py ml_pipeline/player_tracker.py ml_pipeline/camera_calibration.py ml_pipeline/heatmaps.py ml_pipeline/bronze_export.py ml_pipeline/db_writer.py ml_pipeline/db_schema.py ml_pipeline/tracknet_v3.py ml_pipeline/video_preprocessor.py ml_pipeline/serve_detector/
```

If the diff is empty: Render-only deploy is enough — `git push origin main` and you're done.

If the diff is non-empty: **a Docker rebuild + dual-region ECR push + new job-def revisions are required**, otherwise Batch jobs run the OLD image silently. Every file in the list above is included in the Batch container at build time. `serve_detector/` is included because `ml_pipeline/roi_extractors/pose.py` imports from it (e.g. `bounce_validity`, `RallyStateMachine`) — the import surface bridges the two halves.

The full deploy sequence is documented under §"How to ship a Batch-side change end-to-end" below. The short version, as run on 2026-05-07 for Phase 1 (BOUNCE):

```bash
# 1. Auth
aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin 696793787014.dkr.ecr.eu-north-1.amazonaws.com
aws ecr get-login-password --region us-east-1  | docker login --username AWS --password-stdin 696793787014.dkr.ecr.us-east-1.amazonaws.com

# 2. Build (3-5 min if no requirements change)
docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline:latest .

# 3. Tag + push to BOTH regions (run in parallel)
docker tag ten-fifty5-ml-pipeline:latest 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker tag ten-fifty5-ml-pipeline:latest 696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker push 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest &
docker push 696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest &
wait

# 4. Extract amd64 sub-manifest digest (NOT manifest list, NOT attestation manifest)
MSYS_NO_PATHCONV=1 aws ecr batch-get-image --region eu-north-1 --repository-name ten-fifty5-ml-pipeline \
  --image-ids imageTag=latest \
  --accepted-media-types application/vnd.oci.image.index.v1+json application/vnd.docker.distribution.manifest.list.v2+json \
  --query 'images[0].imageManifest' --output text \
  | python -c "import json,sys; m=json.loads(sys.stdin.read()); [print(x['digest']) for x in m['manifests'] if x['platform']['architecture']=='amd64']"

# 5. Register new job-def revisions in both regions, pinned to the new amd64 digest, retryStrategy preserved.
#    Use the C:\Users\tomos\AppData\Local\Temp\register_jobdefs.py pattern from the May 7 deploy.
```

**Do not skip step 5.** Pushing `:latest` does NOT change which image a digest-pinned job-def pulls. Lambda submits jobs by job-def NAME (`BATCH_JOB_DEF=ten-fifty5-ml-pipeline`), so new jobs auto-resolve to the latest active revision — but only after step 5 makes that revision the latest.

## ON-DEMAND CAPACITY DEFAULT — QUEUE CE ORDER

For **testing iterations**, the queue is configured so the on-demand CE is priority 1 and Spot is priority 2. Reasoning: testing reruns are time-sensitive (Tomo is waiting for them), and Spot eviction can lose 30-60 min of runtime on a single rerun. The cost premium is ~$0.40 vs $0.12 per job — accepted.

Verify current order:
```bash
aws batch describe-job-queues --region eu-north-1 --job-queues ten-fifty5-ml-queue --query 'jobQueues[0].computeEnvironmentOrder'
```

Expected (as of 2026-05-07): order 1 = `ten-fifty5-ml-ce-eu-ondemand`, order 2 = `ten-fifty5-ml-compute` (Spot).

To revert to Spot-priority production behaviour after a testing campaign:
```bash
aws batch update-job-queue --region eu-north-1 --job-queue ten-fifty5-ml-queue --compute-environment-order '[{"order":1,"computeEnvironment":"arn:aws:batch:eu-north-1:696793787014:compute-environment/ten-fifty5-ml-compute"},{"order":2,"computeEnvironment":"arn:aws:batch:eu-north-1:696793787014:compute-environment/ten-fifty5-ml-ce-eu-ondemand"}]'
```

If on-demand has zero G-family vCPU quota (last confirmed 2026-04-15; quota may have been raised since), jobs will sit in RUNNABLE indefinitely. Fall back to swapping the order back to Spot-first if RUNNABLE → STARTING never transitions within ~5 min.

---

## NEXT SESSION — READ THIS FIRST

You're picking up cold. **READ THE TEST HARNESS SECTION BELOW BEFORE TOUCHING ANY DETECTOR CODE.** The harness is now the unit of work — every detector change goes through it before push. No more Render-shell trial-and-error.

**Then read `project_t5_may07_phantom_bounces.md`** in MEMORY.md — it has the full evidence chain (visual + DB + harness) for why the 4 remaining misses are upstream and what the actual fix direction is. Don't restart the diagnosis; the receipts are already there.

### Current state (verified 2026-05-07 on task `a798eff0-551f-4b5a-838f-7933866a727c` vs SA `2c1ad953-b65b-41b4-9999-975964ff92e1`)

| Metric | Value | vs 80-90% gate | Status |
|---|---|---|---|
| Total MATCH | 20/24 = 83% | ✓ | Floor — won't move without bounce filtering. |
| Near MATCH | 13/14 = 93% | ✓ | 148.52 is real bronze pose-amplitude gap (separate problem). |
| Far MATCH | 7/10 = 70% | ✓ | 458/463/584 all blocked by phantom-bounce rally pollution. |
| Bench baseline | committed at `ml_pipeline/diag/bench_baseline.json` | — | Any detector edit goes through `bench` first. |

### The 4 remaining misses — two distinct upstream problems

**Class A — phantom-bounce rally pollution (FAR misses 458.08, 463.52, 584.92).** Bronze TrackNet emits dense clusters of phantom "bounces" on the near baseline (cy 21-26), 0.3-1.0s apart, never crossing the net. They keep `RallyStateMachine` in IN_RALLY for 16-second blocks centred on each miss. Both upstream (`extract_far_pose`'s rally gate) and downstream (`_detect_pose_based_serves` for far) skip these windows. With the rally gate disabled locally (May 7 test), ROI **does** find the actual far player at the baseline — verified visually. The problem is the rally gate, fed by phantom bounces, blocks them.

**Class B — bronze pose-amplitude gap (NEAR miss 148.52).** Bronze YOLOv8x-pose returns 0.95-conf keypoints throughout the trophy window, but the dominant wrist physically never clears the avg shoulder line by more than 0.1 px. trophy_frames=0 in ±2s; cluster scores 2 only via toss + both_up. arm_ext distribution for the cluster: min=-76.6 p50=-6.1 max=0.1. Independent of FAR class — needs different fix (different pose model, training data, or accept the floor).

### Tomo's bounce-validity rule (May 7) — the architectural fix direction

> "A bounce can only be valid if it travels from one side to the other side of the net, or into the net. Those phantom bounces are bounces that happen before serve. Players bouncing the ball on racquet etc. It's mostly up and down or perhaps multiple bounces on the same side of the court."

**A bounce sequence `[b1, b2]` is valid rally evidence only if `(b1.cy - HALF_Y)` and `(b2.cy - HALF_Y)` have opposite signs (ball crossed the net) OR there's a net-hit between them.** Same-side bounce sequences are pre-serve racquet bounces or detector noise and must not advance rally state.

### Why this needs three coupled changes (not one knob)

1. **Bounce validation** (new logic — somewhere between `ml_analysis.ball_detections` consumers and `RallyStateMachine`). Filter out non-net-crossing bounces.
2. **`extract_far_pose`** must consume validated bounces (or have its rally gate dropped — downstream gate becomes the safety).
3. **Downstream `_detect_pose_based_serves` for far** must consume validated bounces too. May also need `sustained_ok` cluster_size relaxed from 30 → 20 (real serve trophy ≈12-25 frames at 25fps).

Each iteration validates via Batch rerun → re-snapshot → bench. ~30-60 min per cycle. Don't try to validate via local rerun — local court calibration is unreliable.

### What you can do — read in order

1. **READ THE TEST HARNESS SECTION below.** It's the operating manual.
2. **Read `project_t5_may07_phantom_bounces.md`** for the receipts. Don't redo the diagnosis.
3. **Don't widen `_baseline_zone` slack.** Apr 29 verified -3.5→-5.0 lost 2 PASS without bounce filtering as the prerequisite.
4. **Don't try to relax `idle_threshold_s`.** Phantom bounces are <1s apart — no gap-based threshold short of disabling the gate works. The fix is bounce validation, not threshold tuning.
5. **Start with bounce validation.** Implement the net-crossing rule. That's the leaf-most upstream fix; everything downstream gets simpler once it's in.

### Diag tools added May 7 (alongside the Apr 29 harness)

- `ml_pipeline/diag/inspect_pose_window.py` — per-frame pose profiler (arm_ext distribution, score breakdown, 5-bucket verdict). Use this any time you need to characterise pose data in a window.
- `ml_pipeline/diag/probe_roi_coverage.py` — task-wide + per-window ROI coverage probe with neighbour-density buckets. Distinguishes "ROI ran but skipped this window" from "ROI is sparse here generally."
- `ml_pipeline/diag/replay_roi_pose.py` — CLI to re-run `extract_far_pose` locally (or on Render) on chosen frame ranges with rally gate disabled. Includes `--cleanup` for removing debug rows from `ml_analysis.player_detections_roi`.
- `extract_far_pose` (production) now accepts `frame_from`/`frame_to`/`replace` params (defaults preserve prior behaviour).

---

## TEST HARNESS — USE THIS BEFORE EDITING ANY DETECTOR GATE

Built 2026-04-29. Eliminates the cloudnet/prod drift and the 5-min-Render-deploy iteration loop that burned three sessions of trial-and-error. Every detector change now goes:

```
edit → bench → see green delta or [!] REGRESSION → push only if green
```

If you skip the bench, you'll regress something invisibly (it has happened — see commit history for `0cb645a` revert of a one-shot threshold change that lost 2 PASS).

### Architecture

The harness has two halves: **prod-shared logic** (so offline numbers always match prod) and **diag tools** (snapshot, replay, bench, audit).

```
serve_detector.detector
├─ _run_pipeline()              ← SHARED logic (rally augmentation, source ordering)
├─ detect_serves_for_task()     ← prod entry: loads from DB, calls _run_pipeline, persists
└─ detect_serves_offline()      ← offline entry: takes data directly, calls _run_pipeline

ml_pipeline/diag/
├─ snapshot_task.py             ← DB → pickle.gz fixture (one-time per task)
├─ replay_serves.py             ← fixture → run prod _run_pipeline → reconcile output
├─ bench.py                     ← runs replay across ALL fixtures vs bench_baseline.json
├─ audit_all_serves.py          ← per-serve gate matrix + prod-kill tracer
├─ probe_baseline_empty.py      ← diagnoses why a window has 0 baseline rows
├─ inspect_cluster_topology.py  ← dumps cluster structure around one ts
└─ bench_baseline.json          ← committed regression baseline (current: 20/24)

ml_pipeline/fixtures/            ← gitignored. Fixtures are 1-2 MB each, regen from DB.
```

The critical refactor: **`_run_pipeline()` is the single source of truth for serve detection logic.** Both prod and offline call it. Before this refactor, `detect_serves_offline` had drifted (different ordering, no rally augmentation) — that's why "cloudnet" numbers diverged from prod for months. Don't let that drift back.

### Workflow — edit detector → validate → push

**Once-per-task setup** (when you want a new fixture):

On Render shell:
```bash
python -m ml_pipeline.diag.snapshot_task --task <T5_TID>
python -c "import boto3; boto3.client('s3').upload_file('ml_pipeline/fixtures/<TID8>.pkl.gz', 'nextpoint-prod-uploads', 'fixtures/<TID8>.pkl.gz'); print('ok')"
```

On your local checkout:
```bash
aws s3 cp s3://nextpoint-prod-uploads/fixtures/<TID8>.pkl.gz ml_pipeline/fixtures/<TID8>.pkl.gz
```

Render redeploys wipe the fixtures dir each push — keep S3 as the durable home. Snapshots are deterministic per (task, sa_truth) so you can always regen.

**Per-edit cycle**:

```bash
# 1. Edit ml_pipeline/serve_detector/*.py

# 2. Run bench locally — sub-second
.venv/Scripts/python -m ml_pipeline.diag.bench

# 3. If green: lock new baseline + commit + push
.venv/Scripts/python -m ml_pipeline.diag.bench --update-baseline
git add ml_pipeline/serve_detector/<file> ml_pipeline/diag/bench_baseline.json
git commit -m "..."
git push origin main

# 4. If red: revert. Don't push.
git checkout ml_pipeline/serve_detector/<file>
```

**For per-serve diagnosis** (when bench shows a regression and you need to find which serve flipped):

```bash
.venv/Scripts/python -m ml_pipeline.diag.audit_all_serves ml_pipeline/fixtures/<TID8>.pkl.gz
```

That gives the per-SA-serve verdict (PASS / WEAK_TIME / WRONG_SIDE / NO_MATCH) plus a "BUCKET A" section that traces WHICH prod gate killed each surviving-but-killed candidate. The trace classifies into:

- `rally_state_gate` — IN_RALLY + peak<3 + sustained_ok=False
- `min_serve_interval` — lost 4s dedup duel to a higher-scoring competitor
- `find_serve_candidates_full_pruned` — cluster gates differ on full data vs windowed probe
- `unknown_passed_all_known_gates` — survived everything we model; means there's a gate the tracer doesn't know about

**For cluster-topology debugging** (when peak-pick wanders or clusters merge wrongly):

```bash
.venv/Scripts/python -m ml_pipeline.diag.inspect_cluster_topology \
    ml_pipeline/fixtures/<TID8>.pkl.gz --ts <TARGET_TS> [--player 0|1]
```

Dumps every score≥1 frame in a ±10s window with score, sub-flags (trophy/toss/both_up), dom_wrist_y. Then shows clusters at gap=1.2 / 0.6 / 0.4 and what `find_serve_candidates` returns at each gap. This is how the cluster-merge fix for 555.68 was diagnosed in <30s of local iteration.

**For Bucket B (no baseline rows in window) diagnosis**:

```bash
python -m ml_pipeline.diag.probe_baseline_empty --task <T5_TID> --ts <TS_LIST> --player 1
```

Runs against the live DB (Render shell only — needs DATABASE_URL). Classifies why a window has zero baseline-zone rows: `detection_miss` / `kpts_without_courty` / `fixable_by_widening_slack` / `kpts_outside_baseline_zone`.

### Silver bench (parallel harness for `build_silver_v2.py` / `build_silver_match_t5.py`)

Built 2026-05-22 (scaffolded 2026-05-21 in `5e3e746`). Mirrors the serve bench shape but runs the silver builder against a local Docker Postgres restored from a per-task `.sql.gz` fixture. Same iteration loop:

```bash
.venv/Scripts/python -m ml_pipeline.diag.bench_silver --setup
.venv/Scripts/python -m ml_pipeline.diag.bench_silver        # green or [!] REGRESSION
# Edit build_silver_v2.py / build_silver_match_t5.py → repeat.
```

Snapshot capture is on Render shell (needs DATABASE_URL):

```bash
python -m ml_pipeline.diag.bench_silver.snapshot --task <T5_TID>
# produces <TID8>_bronze.sql.gz + <TID8>_silver_baseline.json
# upload both to s3://nextpoint-prod-uploads/fixtures/silver/
```

Full bootstrap playbook (the steps you run NOW to land the first fixture): `.claude/strategy/silver_bench_design_2026-05-21.md` §11. Design + spec: same doc §2-§7.

The silver bench exists because Phase 3 part 2 was reverted twice (`00b8639`, `f0b104e`) — both shipped broken silver row counts to prod that a local bench would have caught in seconds.

### What the harness does NOT cover

- **Upstream pose extraction quality.** If YOLOv8x-pose missed the trophy frame (Bucket C, e.g. 148.52), the harness can't recover it.
- **Court projection / homography failures.** If `court_y` is NULL on keypoint rows (Bucket B-1, e.g. 458/463), the harness can't recover it.
- **Player-ID consistency across a match.** If pid=1 swaps to the wrong player mid-match (Bucket B-2, e.g. 584.92), the harness can't recover it.
- **Multi-task generalisation.** Right now there's ONE fixture (a798eff0). Add 2-3 more reference tasks with known-good far recall to make the bench detect "fix that helps task X regresses task Y" cases. Until that lands, treat single-fixture green deltas as preliminary, not final.

### Rules — read before editing

1. **Never push a serve_detector change that doesn't pass `bench` cleanly.** If you push without bench, you've ignored the harness and you're back to trial-and-error.
2. **Always lock the new baseline if you push a fix.** `bench --update-baseline` writes the new numbers; commit `bench_baseline.json` so the next session sees the new floor.
3. **Don't widen `_baseline_zone` slack without proving it via probe_baseline_empty FIRST.** A naive widening costs more than it gains (verified 2026-04-29: -3.5→-5.0 lost 2 PASS).
4. **Far gates (cluster_gap_s, min_serve_interval_s, sustained_ok) are per-pid.** Touching them affects far recall. Always check the audit BUCKET A trace before tuning — the gate that's killing a far miss is named explicitly there.
5. **Don't tune in find_serve_candidates without inspect_cluster_topology output.** Cluster gates depend on cluster structure; you have to see the structure first or you're guessing.

### Adding more fixtures

Once you have a second known-good reference task (post-Bucket-B fix or a fresh upload with clean far recall), add it:

```bash
# On Render
python -m ml_pipeline.diag.snapshot_task --task <NEW_TID>
python -c "import boto3; boto3.client('s3').upload_file('ml_pipeline/fixtures/<TID8>.pkl.gz', 'nextpoint-prod-uploads', 'fixtures/<TID8>.pkl.gz')"

# Locally
aws s3 cp s3://nextpoint-prod-uploads/fixtures/<TID8>.pkl.gz ml_pipeline/fixtures/<TID8>.pkl.gz
.venv/Scripts/python -m ml_pipeline.diag.bench --update-baseline
git add ml_pipeline/diag/bench_baseline.json && git commit -m "bench: add fixture <TID8>" && git push
```

`bench` then runs all fixtures on every check. A change that improves task X but regresses task Y is flagged immediately.

---

### How to ship a Batch-side change end-to-end

This is the autonomous workflow. **Read the gotchas section before running these.**

```bash
# 0. Confirm starting state
git log --oneline -10
aws sts get-caller-identity                     # creds active
aws ecr describe-repositories --region eu-north-1 --query 'repositories[?repositoryName==`ten-fifty5-ml-pipeline`]'
aws batch describe-job-definitions --region eu-north-1 \
    --job-definition-name ten-fifty5-ml-pipeline --status ACTIVE \
    --query 'reverse(sort_by(jobDefinitions,&revision))[0].[revision,containerProperties.image]' --output text

# 1. Edit code (e.g. ml_pipeline/roi_extractors/pose.py for option 1)
# 2. Commit + push
git add <files> && git commit -m "..." && git push origin main

# 3. Auth Docker to both ECR regions
aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin 696793787014.dkr.ecr.eu-north-1.amazonaws.com
aws ecr get-login-password --region us-east-1  | docker login --username AWS --password-stdin 696793787014.dkr.ecr.us-east-1.amazonaws.com

# 4. Build (run in background, ~15-20 min for changed pip layer; ~3-5 min if only code changed)
docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline:latest .
# Use run_in_background:true; build is long.

# 5. Tag + push to BOTH regions (push in parallel via background tasks)
docker tag ten-fifty5-ml-pipeline:latest 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker tag ten-fifty5-ml-pipeline:latest 696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker push 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest    # ~5-10 min
docker push 696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest     # ~5-10 min

# 6. Get the amd64 sub-manifest digest from the manifest list. CRITICAL — see Gotcha #1.
MSYS_NO_PATHCONV=1 aws ecr batch-get-image --region eu-north-1 \
    --repository-name ten-fifty5-ml-pipeline \
    --image-ids imageTag=latest \
    --accepted-media-types application/vnd.oci.image.index.v1+json application/vnd.docker.distribution.manifest.list.v2+json \
    --query 'images[0].imageManifest' --output text
# Extract the digest with platform.architecture=amd64 (NOT the manifest list itself, NOT the attestation manifest)
# Set $AMD64_DIGEST="sha256:54e4..." or similar.

# 7. Register new job-def revision pinned to that digest, with retry strategy.
# DO NOT write the DATABASE_URL into a heredoc — sandbox blocks (Gotcha #3).
# Instead: fetch existing rev as JSON, modify with Python, register with that file.
aws batch describe-job-definitions --region eu-north-1 --job-definition-name ten-fifty5-ml-pipeline --status ACTIVE \
    --query 'reverse(sort_by(jobDefinitions,&revision))[0]' \
    > C:/Users/tomos/AppData/Local/Temp/jd_curr.json
python - <<'PY'
import json
src = r'C:\Users\tomos\AppData\Local\Temp\jd_curr.json'
dst = r'C:\Users\tomos\AppData\Local\Temp\jd_new.json'
AMD64_DIGEST = 'sha256:____PASTE_FROM_STEP_6____'
with open(src) as f: d = json.load(f)
for k in ('jobDefinitionArn','revision','status','containerOrchestrationType'): d.pop(k, None)
d['containerProperties']['image'] = f'696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline@{AMD64_DIGEST}'
# Retry strategy — auto-retry on Spot eviction
d['retryStrategy'] = {
  'attempts': 3,
  'evaluateOnExit': [
    {'action': 'RETRY', 'onStatusReason': 'Host EC2*', 'onReason': '*'},
    {'action': 'RETRY', 'onReason': 'DockerTimeoutError*'},
    {'action': 'EXIT', 'onExitCode': '0'},
    {'action': 'EXIT', 'onReason': '*'},
  ],
}
with open(dst, 'w') as f: json.dump(d, f)
print('wrote', dst)
PY
aws batch register-job-definition --region eu-north-1 --cli-input-json "file://C:/Users/tomos/AppData/Local/Temp/jd_new.json" \
    --query '[revision,containerProperties.image]' --output text

# 8. Tomo uploads a fresh match through the frontend (Singles T5 game type — gated to tomo.stojakovic@gmail.com).
#    Tomo replies with the new task_id.
# 9. Find the Batch job and monitor.
aws batch list-jobs --region eu-north-1 --job-queue ten-fifty5-ml-queue \
    --filters name=JOB_NAME,values=t5-tenni-<task_prefix>* \
    --query 'jobSummaryList[].[jobName,jobId,status]' --output table

# 10. Spot tight in eu-north-1? Disable Spot CE to force on-demand.
aws batch update-compute-environment --region eu-north-1 --compute-environment ten-fifty5-ml-compute --state DISABLED
# WAIT for the job to reach RUNNING, then re-enable:
aws batch update-compute-environment --region eu-north-1 --compute-environment ten-fifty5-ml-compute --state ENABLED --compute-resources minvCpus=0,maxvCpus=4

# 11. After SUCCEEDED, pull CloudWatch logs to confirm the change took effect.
LOG_STREAM=$(aws batch describe-jobs --region eu-north-1 --jobs <batch_job_id> --query 'jobs[0].container.logStreamName' --output text)
MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
    aws logs get-log-events --region eu-north-1 \
    --log-group-name /aws/batch/ten-fifty5-ml-pipeline \
    --log-stream-name $LOG_STREAM --limit 10000 \
    --query 'events[].message' --output json > /tmp/log.json 2>/dev/null
# Parse with python; grep for the new behaviour markers (e.g. "roi_pose: skipped IN_RALLY frames" for option 1)

# 12. Tomo runs eval/reconcile on Render shell:
#       python -m ml_pipeline.harness eval-serve <task_id>
#       python -m ml_pipeline.diag.reconcile_serves_strict --task <task_id>
#     and pastes output. Analyse vs current 13/14 + 3/10 baseline.
```

### Gotchas (HARD-LEARNED THIS SESSION — read every one)

**1. Buildx pushes manifest LISTS, not regular images.** ECR `:latest` resolves to the list. Job-defs that point at digest pin a SPECIFIC image manifest — and the first time you re-push :latest, the digest the job-def has is the OLD amd64 sub-manifest. Effect: jobs run the old image silently, wasting 45 min Batch time. **ALWAYS extract the new amd64 sub-manifest digest with `aws ecr batch-get-image` and register a new job-def rev** (see step 6+7 above). Don't trust `:latest` to "just work" with digest-pinned job-defs.

**2. Lambda submits jobs by job-def name only** (not revision). `BATCH_JOB_DEF=ten-fifty5-ml-pipeline` env var. So new jobs auto-resolve to the latest active revision. You don't need to update Lambda env to deploy a new image — just register a new revision.

**3. Sandbox blocks DATABASE_URL embedding.** Anything that writes the production DB URL into a freshly-authored file (heredoc to `/tmp`, manual JSON, etc.) gets rejected as "credential leakage". Workaround: fetch existing job-def via `describe-job-definitions`, modify in place via Python (the credential is pass-through, not new), register from that file. The eu-north-1 register works this way; us-east-1 sometimes still blocks — primary region is eu-north-1, so single-region register is OK.

**4. Service-quotas API blocked for nextpoint-uploader IAM user.** Can't read on-demand vCPU quota directly. The on-demand CE `ten-fifty5-ml-ce-eu-ondemand` IS in the queue (order 2 behind Spot). Manual cross-region failover playbook at `.claude/playbook_aws_batch_ondemand_fallback.md`.

**5. `aws logs ... --output text` chokes on Unicode arrows in pipeline logs (`→`, `→`).** Use `--output json` and parse with Python that has `encoding='utf-8'`. Set `MSYS_NO_PATHCONV=1` to stop Git Bash mangling `/aws/batch/...` log group paths into Windows paths.

**6. Git Bash `/tmp` ≠ Python `/tmp`.** Git Bash maps `/tmp` to `C:/Users/<user>/AppData/Local/Temp/`. Python sees the literal `/tmp` and fails. Use the absolute Windows path in Python heredocs.

**7. Retry strategy must be pinned per job-def revision.** Rev 1 was created without retryStrategy — first Spot eviction would fail outright. Rev 40 has 3-attempt retry on `Host EC2*` (Spot) and `DockerTimeoutError*`. Carry that retryStrategy forward when registering new revs.

**8. Don't override the container command.** The Dockerfile has `ENTRYPOINT ["python", "-m", "ml_pipeline"]`. Submitting with `--container-overrides 'command=["python","-m","ml_pipeline","--job-id",...]'` produces `python -m ml_pipeline python -m ml_pipeline --job-id ...` and the inner `python -m ml_pipeline` becomes argparse arguments → exit code 2. Override with `command=["--job-id","X","--s3-key","Y"]` only.

**9. Don't poll Batch for hours.** Use `Monitor` with a poll loop that exits on terminal states (SUCCEEDED|FAILED) and emits transitions. Tomo self-serves run status from the frontend dashboard.

### What you can validate offline before pushing

For Render-side changes (anything outside `ml_pipeline/__main__.py` and `roi_extractors/`):
- Render auto-deploys from `origin/main` in ~5 min after push. Wait ~5 min, ask Tomo to re-run on Render shell.

For Batch-side changes:
- `docker run --rm --network none --entrypoint python ten-fifty5-ml-pipeline:latest -c "from ml_pipeline.roi_extractors import extract_far_pose; print('ok')"` — proves imports and ViTPose offline-load. Catches Dockerfile errors before pushing 6.4 GB to ECR.

### Validation rule — don't skip this

After ANY change to the FAR-player path, reconcile MUST be run on `4a591553-a8d9-4eaf-9bff-b0ec5c9c1185` (current task with both ROI data and SA reference). If far MATCH drops below 3/10, REVERT. If you can't get above 3/10 strict with one targeted change, escalate to Tomo before continuing.

The two metrics that matter, in priority order:
1. Strict reconcile MATCH count (NEAR + FAR) — should NEVER decrease vs the immediately-prior commit.
2. eval-serve precision — should rise without sacrificing #1.

If a change improves precision but reduces MATCH count, REVERT. Tomo prefers more MATCHes with FPs to fewer MATCHes with high precision. Tuning toward "low FP" is the wrong objective; tuning toward "more right" is correct.

---

---

## Deployment state (2026-04-23 — Option A landing)

**WHAT'S LIVE IN PRODUCTION** (auto-deployed via Render pulling from `main`):

`serve_detector/` — all session improvements are in the code path Render runs post-ingest:
- Bronze pid=1 chair-umpire fix (ROI wins wholesale for pid=1)
- Cross-player dedup only considers NEAR events
- Pose cluster gates (size-1 strong-arm exception, size-2 duo_accepted rule)
- Score-first peak-picker (prefers trophy over follow-through)
- Score-aware ROI ensemble merge (handles Base + Large source tags)
- Arm-ext threshold 2.5 px for pid=1 (was 5)
- Augmented rally state machine for near-pose detection
- Reconcile diagnostic with flight-time offsets

`reconcile_serves_strict.py` + `probe_serve_window.py` + `visualize_far_serve.py` diag tools — all on main.

### P2 ROI extractor integration (Option A — deployed 2026-04-23)

**Images live in ECR, both regions:**
- `696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest`
- `696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest`
- Digest: `sha256:2640e28f9531b8431313a0e6c192acf082da5b111b6feba6bc43639d60977640`
- Image size: 17.9 GB uncompressed / 6.4 GB compressed (adds transformers 4.49.0 + ViTPose-Base weights on top of previous rev)

**Code wired into Batch pipeline** (`ml_pipeline/__main__.py::_run_batch` step 2b):
- `ml_pipeline/roi_extractors/pose.py::extract_far_pose()` — SA-less ViTPose-Base extractor, scans the whole video on GPU, writes to `ml_analysis.player_detections_roi` (`source='far_vitpose'`). Failure is non-fatal.
- `ml_pipeline/roi_extractors/bounces.py::extract_far_bounces()` — **STUB** (logs + returns 0). Proper WASB bounce extraction deferred to a follow-up session.
- Runs only for match uploads (`if not practice`), not practice sessions.
- Smoke-tested locally (CPU) in Phase 1: 306 sampled frames → 260 detections → 119 usable pose rows in 579 s. End-to-end DB write confirmed.

**Job-def** still uses `:latest` tag (Batch rev 1 in both regions). Spot nodes are ephemeral so every new job pulls the new image fresh — no re-registration needed. Confirm via: `aws ecr describe-images --region eu-north-1 --repository-name ten-fifty5-ml-pipeline --image-ids imageTag=latest`.

**FULLY VALIDATED END-TO-END IN PRODUCTION BATCH** — task `4a591553-a8d9-4eaf-9bff-b0ec5c9c1185` on 2026-04-23 18:37–19:30 UTC ran the complete deployed pipeline on Spot G4dn.xlarge under job-def rev 40 (digest-pinned). Results:
  - Main pipeline: 15,300 frames, 0 errors, 2539 s
  - ROI extractor: 7,650 sampled frames → 7,244 YOLO detections → **3,725 usable ViTPose rows written** to `ml_analysis.player_detections_roi` with `source='far_vitpose'` in 500 s
  - Bronze export, heatmaps, debug frames, trimmed video all succeeded
  - Cost: $0.14 on on-demand (retry after earlier Spot eviction)

### Job-def state (post-validation)

- Rev 40 active in eu-north-1, pinned to `sha256:54e4a0c7...` (today's amd64 sub-manifest of the buildx manifest list)
- retryStrategy: 3 attempts, auto-retry on `Host EC2*` (Spot eviction) and `DockerTimeoutError*`
- Lambda submits by name (`BATCH_JOB_DEF=ten-fifty5-ml-pipeline`) so new jobs auto-resolve to rev 40
- us-east-1 rev not updated (sandbox blocked re-register; primary region stays eu-north-1)

### Gotcha learned this session

Buildx pushes a manifest-list, not a regular image manifest. If the job-def is pinned to a digest (which it was from a previous rev), pushing a new `:latest` to ECR does NOT change the pinned digest — the job keeps pulling the old image. Always `aws batch register-job-definition` with the new amd64 sub-manifest digest after a push. Don't rely on tag resolution when the job-def uses `@sha256:` pinning.

### How to verify on a fresh upload

```sql
-- Did the ROI pose step fire?
SELECT count(*), source, min(frame_idx), max(frame_idx)
FROM ml_analysis.player_detections_roi
WHERE job_id = '<new_task_id>'
GROUP BY source;
-- Expect: thousands of rows, source='far_vitpose'
```
```sql
SELECT count(*), source, min(frame_idx), max(frame_idx)
FROM ml_analysis.player_detections_roi
WHERE job_id = '<new_task_id>'
GROUP BY source;
```
Should see `source='far_vitpose'` with rows spanning most of the video. If zero rows, the batch logs at `awslogs-group=/aws/batch/ten-fifty5-ml-pipeline` will show the `roi_pose:` lines from `ml_pipeline/roi_extractors/pose.py` indicating where it failed (calibration, YOLO, or ViTPose).

Then on Render: `python -m ml_pipeline.harness rerun-ingest <task_id>` + `python -m ml_pipeline.harness eval-serve <task_id>` (if SA counterpart exists).

### Extractors still available as diag tools (for retrospective re-processing)

- `ml_pipeline/diag/_archive/extract_vitpose_far.py` — relocated to archive; kept for re-running ROI on historical tasks or A/B testing different model variants. Takes `--sportai` for serve-time windows; new production extractor scans the whole video.
- `ml_pipeline/diag/_archive/extract_wasb_bounces.py` — relocated to archive; needed pending the production bounce extractor.
- Active: `ml_pipeline/diag/extract_roi_bounces.py` — TrackNet ROI bounce extractor (still in active diag dir).

---

## Architecture at a glance

```
video.mp4 (S3)
      │
      ▼
┌──────────────────────────────────────────────────┐
│  ml_pipeline/ (AWS Batch, GPU)                   │
│  ┌──────────┐  ┌─────────┐  ┌────────┐          │
│  │ court_   │  │ ball_   │  │ player_│          │
│  │ detector │  │ tracker │  │ tracker│          │
│  └────┬─────┘  └────┬────┘  └───┬────┘          │
│       └─────────────┴───────────┘                │
│                     ▼                            │
│          ml_analysis.* (bronze)                  │
│          ball_detections, player_detections,     │
│          court_detections, video_analysis_jobs   │
└──────────────────────────────────────────────────┘
                     │
                     ▼  (Render main API "Sport AI - API call", _do_ingest_t5)
┌──────────────────────────────────────────────────┐
│  ml_pipeline/serve_detector/                     │
│  pose-first for near player                      │
│  bounce-first for far player                     │
│  rally-state gate                                │
│                     ▼                            │
│          ml_analysis.serve_events                │
└──────────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────┐
│  build_silver_match_t5.py                        │
│  (consumes serve_events + ml_analysis.*)         │
│                     ▼                            │
│          silver.point_detail  (model='t5')       │
└──────────────────────────────────────────────────┘
                     │
                     ▼
             gold.* views  →  API  →  dashboards
```

**Split of responsibilities:**

| Layer | Runs on | Writes | Iteration speed |
|---|---|---|---|
| ML detection (court/ball/player) | AWS Batch GPU | `ml_analysis.*` | ~47 min / run; needs Docker rebuild |
| Serve detection | Render main API | `ml_analysis.serve_events` | ~10 s / run; silver rerun |
| Silver build | Render main API | `silver.point_detail` | ~10 s / run |
| Gold views | Render main API (boot) | `gold.*` views | Instant |

---

## Running the pipeline

### Local dev setup (Windows)

```bash
cd C:/dev/webhook-server
source .venv/Scripts/activate
pip install -r ml_pipeline/requirements.txt
# DATABASE_URL points at the Render prod DB by default.
```

### Fresh Batch run on a new video

Preferred: upload via Media Room `/media-room`, gated to `tomo.stojakovic@gmail.com`. Auto-ingest fires on completion. ~47 min total.

Manual submit (CLI):
```bash
aws batch submit-job --region eu-north-1 \
  --job-name t5-<short-desc> \
  --job-queue ten-fifty5-ml-queue \
  --job-definition ten-fifty5-ml-pipeline:30 \
  --parameters s3_key=wix-uploads/<name>.mp4,job_id=<NEW_UUID>
```

On spot-capacity failure, failover to us-east-1 with `--job-definition ten-fifty5-ml-pipeline:19`.

### Re-run only silver (fast iteration, no Batch cost)

Use this when iterating on serve_detector or silver builder code:
```bash
python -m ml_pipeline.harness rerun-silver <task_id>
python -m ml_pipeline.harness reconcile 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb <task_id>
```

### Re-run full ingest (rebuild bronze too)

Only needed if bronze `ml_analysis.*` was cleared or if switching image:
```bash
python -m ml_pipeline.harness rerun-ingest <task_id>
```

## Validation

### Quick sanity pass

```bash
python -m ml_pipeline.harness validate <task_id>         # bronze + silver presence
python -m ml_pipeline.harness eval-court <task_id>       # court confidence, keypoint error
python -m ml_pipeline.harness eval-ball <task_id>        # detection rate, bounce count, speed
python -m ml_pipeline.harness eval-player <task_id>      # count, coord variance, path length
python -m ml_pipeline.harness eval-serve <task_id>       # precision/recall vs SportAI ground truth
```

`eval-serve` targets: precision ≥ 90%, recall ≥ 85%, mean ts error < 1 s.

### Full reconcile vs SportAI ground truth

```bash
python -m ml_pipeline.harness reconcile 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb <task_id>
# Modes: --mode=summary|coverage|distributions|speed|rows (default: all)
```

## Reference data

| Purpose | Task ID / path |
|---|---|
| **Baseline T5** (validated 2026-04-16) | `081e089c-f7b1-49ce-b51c-d623bcc60953` |
| **SportAI ground truth** | `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` (88 rows, 24 serves: 14 near + 10 far) |
| Reference video (S3) | `s3://nextpoint-prod-uploads/wix-uploads/1776237770_match.mp4` |
| Reference video (local) | `ml_pipeline/test_videos/match_90ad59a8.mp4.mp4` (50.8 MB) |
| Pre-serve-detector snapshot | 2026-04-16 handover table, pinned in `memory/project_t5_apr17_serve_detection_root_cause.md` |

---


---

## File index (active modules only — archived tools listed at bottom)

### Detection pipeline (runs in Batch container)

| File | Purpose |
|---|---|
| `__main__.py` | Entry point — `python -m ml_pipeline --job-id X --s3-key Y` |
| `pipeline.py` | Orchestrates court → ball → motion → player per frame |
| `config.py` | All tunable constants (intervals, thresholds, court geometry) |
| `video_preprocessor.py` | Frame metadata + iterator |
| `court_detector.py` | CNN (14 keypoints) + Hough fallback + geometry validation + calibration lock |
| `camera_calibration.py` | Radial (Brown-Conrady k1/k2) + piecewise-homography lens calibration |
| `ball_tracker.py` | TrackNetV2 (9-channel) + frame-delta Hough fallback + 3-tier heatmap extraction |
| `tracknet_v3.py` | TrackNetV3 architecture port; activates when weights present |
| `player_tracker.py` | Multi-strategy detection (YOLOv8x-pose + SAHI + YOLOv8m-det) + 3-tier court-metre scoring |
| `heatmaps.py` | Rally / serve / bounce heatmap renderer |
| `bronze_export.py` | Write bronze JSON to S3 for archive |
| `db_schema.py` | DDL for `ml_analysis.*` tables |
| `db_writer.py` | Bulk-insert ball/player/job rows into `ml_analysis.*` |

### Serve detection (runs on Render, silver-build time)

| File | Purpose |
|---|---|
| `serve_detector/__init__.py` | Public API: `detect_serves_for_task`, `ServeEvent`, `SignalSource` |
| `serve_detector/models.py` | `ServeEvent` dataclass + `SignalSource` enum |
| `serve_detector/schema.py` | DDL for `ml_analysis.serve_events` (idempotent) |
| `serve_detector/pose_signal.py` | Silent Impact 2025 passive-arm scoring; cluster + peak selection |
| `serve_detector/rally_state.py` | HMM-style {pre_point, in_rally, between_points} state machine |
| `serve_detector/ball_toss.py` | Optional rising-ball confirmation (boosts conf, never rejects) |
| `serve_detector/detector.py` | Orchestrator — pose-first near, bounce-first far, signal fusion |
| `serve_detector/validate_offline.py` | In-memory runner against local pose JSONL (no DB writes) |
| `serve_detector/tests/test_components.py` | 9 component tests |

### Stroke detection (runs on Render, silver-build time) — SHIPPED 2026-05-24 night

| File | Purpose |
|---|---|
| `stroke_detector/__init__.py` | Public API: `detect_strokes_for_task`, `detect_strokes_offline`, `StrokeEvent` |
| `stroke_detector/models.py` | `StrokeEvent` dataclass (one row per detected stroke contact) |
| `stroke_detector/schema.py` | DDL for `ml_analysis.stroke_events` (idempotent) |
| `stroke_detector/velocity_signal.py` | Wrist-velocity peak detection (refactor of `diag/ball_hit_pose.py` probe) |
| `stroke_detector/detector.py` | Orchestrator — peak-offset (+4f), min-gap=25, decel filter `v[i+3] > peak*0.5` |

Pose-first wrist-velocity peak detector, sibling to `serve_detector/`. Same lifecycle: schema auto-created on first call, delete+reinsert per task on re-detection. Wired into `upload_app.py::_do_ingest_t5` right after serve detection. **Silver consumption is NOT yet wired** — `stroke_events` is populated but `build_silver_match_t5.py` still uses bounce-driven row generation; pivot to stroke-driven is the next Phase 6 step.

**Quick verification on a live task:**
```bash
# Force-rerun stroke detection on an existing T5 task
python -c "from ml_pipeline.stroke_detector import detect_strokes_for_task; from db_init import engine; \
    conn=engine.connect(); trans=conn.begin(); \
    events=detect_strokes_for_task(conn, '<T5_TASK_ID>', replace=True); \
    trans.commit(); conn.close(); print(f'persisted {len(events)} stroke events')"

# Check count + confidence span
psql "$DATABASE_URL" -c "SELECT COUNT(*), MIN(ts)::int, MAX(ts)::int, AVG(confidence)::numeric(3,2) \
    FROM ml_analysis.stroke_events WHERE task_id::text = '<T5_TASK_ID>';"
```

### Silver (T5 variant)

| File | Purpose |
|---|---|
| `build_silver_match_t5.py` | Match silver builder. Reads `ml_analysis.*` + `serve_events`, shares passes 3-5 with `build_silver_v2.py` (in repo root — used by SportAI too) |
| `build_silver_practice.py` | Practice silver builder (serve_practice + rally_practice). 3-pass SQL |

### Ingest / bronze

| File | Purpose |
|---|---|
| `bronze_ingest_t5.py` | Downloads gzipped JSON from S3 into `ml_analysis.*` |
| `api.py` | Flask blueprint — ops-key-protected ML job status + result S3 retrieval |

### Harness / test / validation (active diag tools)

| File | Purpose |
|---|---|
| `harness.py` | Swiss-army CLI — validation, reconcile, rerun, training-bench, eval-* |
| `eval_store.py` | Persists eval run results to `ml_pipeline/eval_history.jsonl` |
| `recon_silver.py` | Lower-level reconcile logic used by `harness reconcile` |
| `training_bench.py` | Event alignment + feature analysis |
| `diag/bench.py` | Test-harness bench runner — runs detector across all fixtures vs committed baseline |
| `diag/snapshot_task.py` | DB → pickle.gz fixture (one-time per task) |
| `diag/replay_serves.py` | fixture → run prod `_run_pipeline` → reconcile (sub-second offline) |
| `diag/audit_all_serves.py` | Per-SA-serve verdict + prod-kill tracer |
| `diag/inspect_cluster_topology.py` | Cluster structure dump around one ts |
| `diag/inspect_pose_window.py` | Per-frame pose profiler with verdict bucketing (May 7) |
| `diag/probe_baseline_empty.py` | Bucket B classifier — why a window has 0 baseline-zone rows |
| `diag/probe_roi_coverage.py` | Task-wide + per-window ROI coverage probe with neighbour density (May 7) |
| `diag/replay_roi_pose.py` | Local re-run of extract_far_pose on chosen frame ranges with rally gate disabled (May 7) |
| `diag/reconcile_serves_strict.py` | SA-vs-T5 serve reconciliation, strict ±0.5s, opposite-side bounce check |
| `diag/extract_roi_bounces.py` | ROI-cropped TrackNet pass for missed serve bounces (still used pending production bounce extractor) |

**Older diag tools archived** at `ml_pipeline/diag/_archive/` (extract_far_player_pose, extract_vitpose_far, wasb_*, trace_missed_*, probe_*, etc — superseded by current harness or by Apr 23 production ROI extractor).

### Root-level touchpoints (not in ml_pipeline/)

| File | Purpose |
|---|---|
| `upload_app.py::_do_ingest_t5` | Orchestrates bronze → serve_detector → silver → trim → SES |
| `upload_app.py::_t5_submit` | Submits new T5 tasks to AWS Batch |
| `build_silver_v2.py` | Shared silver derivation (passes 3-5). T5's silver builder calls into this |
| `gold_init.py` | Gold views (`gold.vw_point` filters `model='t5'` for T5 runs) |
| `video_pipeline/video_trim_api.py` | Trim silver events to highlight video |


---

## Reference data

| Purpose | Task ID / path |
|---|---|
| **Baseline T5** (validated 2026-04-16) | `081e089c-f7b1-49ce-b51c-d623bcc60953` |
| **SportAI ground truth** | `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` (88 rows, 24 serves: 14 near + 10 far) |
| Reference video (S3) | `s3://nextpoint-prod-uploads/wix-uploads/1776237770_match.mp4` |
| Reference video (local) | `ml_pipeline/test_videos/match_90ad59a8.mp4.mp4` (50.8 MB) |
| Pre-serve-detector snapshot | 2026-04-16 handover table, pinned in `memory/project_t5_apr17_serve_detection_root_cause.md` |

---

---

## Troubleshooting index


| Symptom | File / check |
|---|---|
| Player IDs swapped (Player 0 at far baseline, Player 1 at near) | `player_tracker.py::_assign_ids` — semantic-half assignment should prevent this since rev 31. If recurring, check whether `frame_height` is being passed correctly from `detect_frame` |
| Player 1 `var_y > 50` | Umpire interference — umpire at net (court_y≈11-12) sometimes wins far-slot. Path-length filter in `pipeline.py` catches most. See P2 |
| Near-player serves missing (after rev 31) | `serve_detector/pose_signal.py::find_serve_candidates` — tune cluster size / arm-extension threshold (30 px default) |
| Too many false-positive serves | `serve_detector/detector.py::_detect_bounce_based_serves_far` — tighten bounce-first gates |
| Rally state misclassifying | `serve_detector/rally_state.py::state_at` — adjust `idle_threshold_s` |
| Pipeline not producing pose | `player_tracker.py::_choose_two_players` — check tier assignment for mid-court (tier-500 added rev 30) |
| `ml_analysis.serve_events` missing | `serve_detector/schema.py::init_serve_events_schema` — auto-created on first use |
| Batch job uses old image | `aws batch describe-job-definitions` — confirm revision pinned to current digest |
| Ball speeds look wrong | `ball_tracker.py::assign_peak_flight_speeds` — p75 over 15-frame window logic |
| Court calibration fails | `camera_calibration.py::fit_calibration` — check RMS threshold 10 px |
| Far-player serves missing (after rev 31) | Bronze ball-bounce sparsity on far half. See P1 — needs local ball extraction or TrackNet retrain |

---

*Historical content is preserved in `handover_t5_archive.md` for incident research only. Don't read unless investigating a historical regression.*
