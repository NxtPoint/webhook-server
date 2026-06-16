# SOP — Standard Operating Procedures

**Tier 2 — Operational reference. Always current.**
**Audience:** every Claude session, and Tomo when he wants to check what should happen.
**Purpose:** the "when X happens, do exactly Y" reference. Each procedure is self-contained. Links out to deeper docs where helpful.

---

## What requires Tomo (so everything else doesn't)

These are the ONLY actions an autonomous Claude session should not do without Tomo's explicit approval:

| Action | Why Tomo |
|---|---|
| Upload a video via the Media Room frontend | Email-gated to `tomo.stojakovic@gmail.com` for billing reasons |
| Browser-based AWS Console actions | Rare; CLI covers ~99% of cases. If something needs the console, write what you'd click and pause. |
| Spend > $20 in one operation | Big Batch reruns, training campaigns, paid services (Roboflow, etc.) |
| Destructive ops on prod data | `DELETE FROM`, dropping schemas, terminating EBS with non-trivial state, force-push to `main` |
| Product / strategic direction calls | "Should we pivot from TrackNetV3 to TOTNet?" / "Should we ship feature X?" |
| Touch billing tables (`billing.*`) on match delete | Hard rule from `CLAUDE.md` #4 — soft-delete only |

**Everything else, do autonomously.** Commit, push, deploy, run benches, spin EC2, run training, query DB read-only, edit docs.

---

## Routine 1 — Code change that touches Render-side only

Files: `*_app.py`, `client_api.py`, `build_silver_v2.py`, `gold_init.py`, `db_init.py`, anything in `tennis_coach/`, `support_bot/`, `coach_invite/`, `billing*`, `client_api.py`, `frontend/*`, `serve_detector/` (it runs on Render).

1. **Bench first** if the change touches `serve_detector/*` or `build_silver_v2.py`:
   ```bash
   .venv/Scripts/python -m ml_pipeline.diag.bench
   ```
   Expect: `ea1e500c=12/26`, `880dff02=23/24`. Don't proceed if red.
2. `git add` the changed files (specific paths — never `git add -A`).
3. `git commit -m "<topic>: <one-line what changed>"` with a HEREDOC body if needed.
4. `git pull --rebase origin main` (catches parallel-agent commits).
5. `git push origin main`.
6. Wait ~5 min for Render auto-deploy (services in `render.yaml`).
7. Validate via `/ops/diag/sql` or the dashboard, depending on what changed.

**No Docker, no ECR, no job-def revision.** Render handles its own deploys from `origin/main`.

---

## Routine 2 — Code change that touches the Batch container

Trigger: `git diff origin/main HEAD --stat` against any of:
- `ml_pipeline/roi_extractors/`
- `ml_pipeline/__main__.py`
- `ml_pipeline/pipeline.py`
- `ml_pipeline/Dockerfile`
- `ml_pipeline/requirements.txt`
- `ml_pipeline/court_detector.py`
- `ml_pipeline/ball_tracker.py`
- `ml_pipeline/player_tracker.py`
- `ml_pipeline/camera_calibration.py`
- `ml_pipeline/heatmaps.py`
- `ml_pipeline/bronze_export.py`
- `ml_pipeline/db_writer.py`
- `ml_pipeline/db_schema.py`
- `ml_pipeline/tracknet_v3.py`
- `ml_pipeline/video_preprocessor.py`
- `ml_pipeline/serve_detector/` (it's imported by `roi_extractors/pose.py`, so in-container too)

If diff is non-empty:

1. **Run bench first** if any `serve_detector/*` touched. Same command + expectations as Routine 1.
2. Commit + push to `origin/main` (same as Routine 1 steps 2-5).
3. Authenticate to both ECR regions:
   ```bash
   aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin 696793787014.dkr.ecr.eu-north-1.amazonaws.com
   aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 696793787014.dkr.ecr.us-east-1.amazonaws.com
   ```
4. Build the image:
   ```bash
   docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline:latest .
   ```
   (~3-5 min if no requirements change; ~15-20 min if requirements changed.)
5. Tag + push to both regions in parallel:
   ```bash
   docker tag ten-fifty5-ml-pipeline:latest 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
   docker tag ten-fifty5-ml-pipeline:latest 696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
   docker push 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest &
   docker push 696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest &
   wait
   ```
6. Extract the amd64 sub-manifest digest (NOT the manifest list; NOT the attestation manifest):
   ```bash
   MSYS_NO_PATHCONV=1 aws ecr batch-get-image --region eu-north-1 \
     --repository-name ten-fifty5-ml-pipeline \
     --image-ids imageTag=latest \
     --accepted-media-types application/vnd.oci.image.index.v1+json application/vnd.docker.distribution.manifest.list.v2+json \
     --query 'images[0].imageManifest' --output text \
     | python -c "import json,sys; m=json.loads(sys.stdin.read()); [print(x['digest']) for x in m['manifests'] if x['platform']['architecture']=='amd64']"
   ```
7. Register a new job-def revision pinned to that digest, preserving retry strategy. Full pattern in `.claude/handover_t5.md` §"How to ship a Batch-side change end-to-end" step 7.
8. **Ask Tomo** to upload a fresh match via the frontend to trigger a Batch job on the new image.
9. After job completes (~30-60 min, monitor via CloudWatch), validate via SQL.

**Reference:** `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST" + §"How to ship a Batch-side change end-to-end" (longer version with all gotchas).

---

## Routine 3 — Running a bench locally (serve detector)

When: before pushing any `serve_detector/*` or `build_silver_v2.py` change.

```bash
.venv/Scripts/python -m ml_pipeline.diag.bench
```

Output: per-fixture verdict. Green = matches baseline. Red = regression on that fixture.

If green and you want to lock new numbers (because you've intentionally improved): `bench --update-baseline`, then commit `bench_baseline.json`.

If red: revert the offending change, reproduce locally, fix, re-bench. Do NOT skip or weaken the gate. The whole point is to catch silent regressions before push.

---

## Routine 4 — Running an experiment on the GPU dev box

When: WASB integration A/B, TrackNet finetune, anything that needs CUDA on real data.

1. Start the box (if stopped):
   ```bash
   aws ec2 start-instances --region eu-north-1 --instance-ids i-0295d636f6bf957eb
   aws ec2 wait instance-status-ok --region eu-north-1 --instance-ids i-0295d636f6bf957eb
   PUBLIC_DNS=$(aws ec2 describe-instances --region eu-north-1 \
     --instance-ids i-0295d636f6bf957eb \
     --query 'Reservations[0].Instances[0].PublicDnsName' --output text)
   ```
2. Rsync the project up (excludes per `.claude/infrastructure/gpu_dev_box_runbook.md`):
   ```bash
   rsync -avz --progress \
     --exclude '.venv/' --exclude '.git/' \
     --exclude 'ml_pipeline/_archive/' --exclude 'ml_pipeline/diag/_archive/' \
     --exclude 'ml_pipeline/training/datasets/' --exclude 'ml_pipeline/training/visual_debug/' \
     --exclude '__pycache__/' \
     -e "ssh -i $HOME/.ssh/t5-dev.pem" \
     /c/dev/webhook-server/ ubuntu@$PUBLIC_DNS:~/webhook-server/
   ```
3. SSH in, venv auto-activates via `.bashrc`:
   ```bash
   ssh -i $HOME/.ssh/t5-dev.pem ubuntu@$PUBLIC_DNS
   cd ~/webhook-server
   # Run experiment
   ```
4. After run completes, rsync results back DOWN before stopping (in case the box gets evicted):
   ```bash
   rsync -avz -e "ssh -i $HOME/.ssh/t5-dev.pem" \
     ubuntu@$PUBLIC_DNS:~/webhook-server/ml_pipeline/diag/ \
     /c/dev/webhook-server/ml_pipeline/diag/
   ```
5. Stop the box (compute bill stops):
   ```bash
   aws ec2 stop-instances --region eu-north-1 --instance-ids i-0295d636f6bf957eb
   ```

**Reference:** `.claude/infrastructure/gpu_dev_box_runbook.md`.

---

## Routine 5 — A phase ships (status transitions to DONE)

1. Update `docs/north_star.md` phase ladder — status DONE with date.
2. Write the 3-line "what shipped" entry under the phase.
3. Update the progress metrics table at the bottom of north_star.md.
4. Move related kickoff / planning / characterisation docs to `.claude/_archive/`:
   ```bash
   git mv .claude/phase<N>_kickoff.md .claude/_archive/
   git mv .claude/phase<N>_*.md .claude/_archive/   # if any
   ```
5. Overwrite `.claude/next_session_pickup.md` with the post-ship state (see Routine 7 below).
6. Commit message: `docs: phase <N> DONE <date>; archive planning docs`.

---

## Routine 6 — A phase is parked (work didn't pan out, falsified)

1. Update `docs/north_star.md` phase ladder — status PARKED with one-line reason.
2. **Keep the characterisation/receipts doc** in `.claude/` (not archived). It's a falsified-hypothesis record; future sessions need to NOT re-attempt the same thing.
3. Add a "Don't" entry to `CLAUDE.md` §"Things not to do" referencing the parked phase if it's a load-bearing falsification.
4. Update `.claude/next_session_pickup.md`.

---

## Routine 7 — Closing a session

Run through the close checklist in `.claude/session_protocol.md`. The short version:

1. ☐ Overwrite `.claude/next_session_pickup.md` with current state.
2. ☐ Update `docs/north_star.md` if any phase status changed.
3. ☐ Archive any docs that became historical (per Routines 5 / 6).
4. ☐ Add a memory entry if a generalisable pattern emerged (use the `auto memory` system per `CLAUDE.md`).
5. ☐ `git pull --rebase origin main` then `git push origin main`.
6. ☐ Output a 2-line session summary: what landed + what's next.

---

## Routine 8 — Diagnosing a production issue via SQL

When: you need to read prod data but don't want to ask Tomo to paste DB output.

```bash
curl -sS -X POST https://api.nextpointtennis.com/ops/diag/sql \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT task_id, sport_type FROM bronze.submission_context ORDER BY created_at DESC LIMIT 5","limit":5}'
```

Read-only — `INSERT/UPDATE/DELETE/DROP/TRUNCATE/ALTER/CREATE/GRANT/REVOKE` and friends are denied. 5-second `statement_timeout`. PII row contents are not logged.

Full reference: `CLAUDE.md` §"Diagnostics & Ops" — covers auth, allowed/denied SQL, error responses.

---

## Routine 9 — Flipping an env var on Render

When: enabling a feature flag (`AUTO_DUAL_SUBMIT_T5=1`, etc).

**Tomo only.** Render env vars require dashboard access; no CLI. Claude proposes the change, Tomo flips it via the Render UI, Claude validates after deploy.

If Claude is autonomous and the env var change is critical, **don't proceed** — wait for Tomo. Render env-var changes are infrequent enough that this isn't a real bottleneck.

---

## Routine 10 — Adding a new feature flag / experiment toggle

1. Add the env var read at the top of the relevant module: `MY_NEW_FLAG = os.getenv("MY_NEW_FLAG", "0") in ("1", "true", "yes")`.
2. Default to OFF (`"0"`) — features ship dark.
3. Document the flag in `docs/env_vars.md` under the appropriate service.
4. Commit + push.
5. **Tomo** sets the flag on Render when ready to enable.

---

## What's NOT in this doc (and where to find it)

- **Architectural overview** — `CLAUDE.md` §"Architecture Overview"
- **Load-bearing don'ts** — `CLAUDE.md` §"Things not to do"
- **T5 phase status** — `docs/north_star.md`
- **Current session state** — `.claude/next_session_pickup.md`
- **Test harness details** — `.claude/handover_t5.md` §"TEST HARNESS"
- **GPU box workflow** — `.claude/infrastructure/gpu_dev_box_runbook.md`
- **Ops endpoint catalogue** — `docs/ops_runbook.md`
- **Env var matrix** — `docs/env_vars.md`
- **Schema DDL locations** — `CLAUDE.md` §"Testing & Code Quality"
- **Doc tier system** — `.claude/docs_hygiene.md`
- **Session boot/close checklists** — `.claude/session_protocol.md`

---

## Maintenance contract

This doc gets updated when an SOP changes — not when a one-off task happens. If you find yourself reading this doc and the routine doesn't match what you're doing, either:
- (a) Your task is one-off — that's fine, do it and don't update the SOP
- (b) The SOP is stale and needs updating — fix it in the same commit as your task

The whole point of a single SOP doc is that **you only have to look in one place** for "how do we do X here?"
