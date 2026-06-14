# T5 Training Environment — the GPU training runbook (Tier-2 REFERENCE)

**Created:** 2026-06-14. **Owner:** Tomo.
**Purpose:** THE single runbook for training the 5 T5 facts. "Submit one command,
get trained weights" — no box to start/stop, no SSH, no per-session struggle.
Do-it-once-do-it-right. On any drift, `.claude/next_session_pickup.md` wins for
*current phase*; this doc is canonical for *how training runs*.

Cross-refs (don't duplicate):
- Label pipelines + per-fact benches inventory → `.claude/training_harness_status.md` (sibling agent).
- Build-first/train-LAST philosophy + the 5 facts → `docs/north_star.md` §"RULES OF THE GAME".
- Batch deploy mechanics (ECR digest pinning, dual-region) → `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST".
- GPU dev box (the OLD interactive path, now superseded for training) → `.claude/infrastructure/gpu_dev_box_runbook.md`.

---

## THE DECISION — AWS Batch GPU one-off jobs (not the dev box)

Training runs as **one-off AWS Batch GPU jobs** on the *existing* detection
compute environments (Spot/on-demand g4dn + on-demand g5 behind
`ten-fifty5-ml-queue`, eu-north-1). Why this over the alternatives:

| Option | Verdict | Why |
|---|---|---|
| **(a) AWS Batch GPU jobs** | ✅ CHOSEN | Infra already exists + scales to 0 (no standing cost). Batch egress reaches the prod corpus DB (proven — a detection job wrote `ml_analysis` 2026-06-13; the 2026-05-21 IP-allowlist block is no longer in effect for Batch). The ML image already has CUDA torch+torchvision+cv2+all weights. One command: submit → train on T4/A10G → weights to S3. Reproducible, no box state. |
| (b) GPU dev box (`t5-dev-gpu`) | ❌ for training | Requires manual start/stop/SSH/rsync every session — exactly the start/stop struggle to kill. Kept as an *interactive* experiment box, not the training path. |
| (c) cheap on-demand cloud GPU | ❌ | Redundant — (a) already gives on-demand GPU with zero idle cost and DB+S3 access wired. |

**The CPU dev box cannot train.** `torch.cuda.is_available()` is False AND the
local `torch==2.11.0+cpu` install is broken (`ImportError: NP_SUPPORTED_MODULES`
from `torch._dynamo` fires on *any* `torch.optim`/model-forward call). Dataset
*builders* (pure cv2/numpy/sqlalchemy/boto3) run fine on CPU; the train loops do
not. This is the core reason a GPU environment is mandatory.

---

## The 5 facts and their trainers

| Fact | Trainer module | Model | Compute | Reads | Status |
|---|---|---|---|---|---|
| **serve** | `ml_pipeline.serve_model.train` | coord MLP | CPU-fast / any GPU | prod DB (`db_init.engine`) | ✅ train-ready |
| **hit** | `ml_pipeline.hit_model.train` | coord MLP | CPU-fast / any GPU | prod DB | ✅ train-ready |
| **bounce** | `ml_pipeline.training.train_bounce_detector` | 1D temporal CNN | CPU-fast / any GPU | prod DB | ✅ train-ready |
| **swing** | `ml_pipeline.training.train_swing_type` | R(2+1)D-18 optical-flow | **GPU-bound** | built dataset (corpus JSON + S3 720p video) | ✅ train-ready |
| **identity** | — none — | rule-based v1 (ADR-03) | — | — | ⛔ NO TRAIN PATH (see below) |

**identity has no trainer by design.** v1 is a deterministic changeover rule
(`ml_pipeline/identity_detector/`). v2 OSNet re-ID is future and needs a
*player-crop* `label_kind` in `ml_analysis.training_corpus` that does not exist
yet. Don't build a trainer until that label pipeline lands — there is nothing to
train on. (Documented, not a gap to "fix" now.)

Corpus today (`ml_analysis.training_corpus`, 8 SA↔T5 pairs, verified 2026-06-14):
`ball_position`=2992 labels · `serve`=404 · `stroke_classifier`=2301.

---

## ONE-COMMAND-PER-FACT (the whole point)

After the one-time setup below, each fact trains on GPU with a single command
from this CPU dev box (it just submits — the GPU work happens on Batch):

```bash
.venv/Scripts/python -m ml_pipeline.training.submit_train_job --fact serve
.venv/Scripts/python -m ml_pipeline.training.submit_train_job --fact hit
.venv/Scripts/python -m ml_pipeline.training.submit_train_job --fact bounce
.venv/Scripts/python -m ml_pipeline.training.submit_train_job --fact swing   # the GPU-hungry one
```

Each prints a Batch `jobId`. The job runs `ml_pipeline.training.batch_train
--fact <f>` in the training image, trains on the GPU, and uploads weights to
`s3://nextpoint-prod-uploads/training/weights/<fact>/_latest/<weights.pt>`
(+ a versioned copy + a `meta.json` sidecar with the trainer's val metrics).

Monitor: Tomo self-serves run status from the frontend, or:
```bash
.venv/Scripts/python -m ml_pipeline.training.submit_train_job --status <jobId>
```

For **swing**, the job FIRST builds the optical-flow dataset (downloads the 720p
trimmed corpus videos + relabels 4-class from `bronze.player_swing`), then
trains. Pass `--skip-dataset` to reuse a dataset already built into the image.

### Run the same trainer directly (GPU dev box / a working CUDA torch)

`batch_train.py` is the identical entrypoint locally:
```bash
python -m ml_pipeline.training.batch_train --fact bounce --epochs 50 --no-upload
python -m ml_pipeline.training.batch_train --fact swing  --epochs 50          # uploads to S3
```

---

## Weights-sync flow (train → S3 → models/ → deploy)

Per `feedback_model_inference_belongs_in_batch_not_render`: weights are
git-ignored and inference runs in the **detection** Batch image, so a trained
weight must travel train-job → S3 → local `models/` → detection rebuild.

```
[Batch train job] --upload--> s3://…/training/weights/<fact>/_latest/<w>.pt
        │
        ▼  (pull down for the next detection rebuild)
.venv/Scripts/python -m ml_pipeline.training.submit_train_job --fact <fact> --download
        │   → writes ml_pipeline/models/<w>.pt (git-ignored)
        ▼
[rebuild DETECTION image] → models/ COPY layer bakes the new weight
        │
        ▼
register new ten-fifty5-ml-pipeline job-def rev (rule #8 dual-region) → live
```

`bench` (serve) / `bench_hit` / `bench_bounce` / `bench_swing_type` are the
gates to run BEFORE shipping a retrained weight — see
`.claude/training_harness_status.md`.

---

## ONE-TIME SETUP — ✅ DONE 2026-06-14 (proven end-to-end on GPU)

**Status: COMPLETE + VALIDATED.** Image built + pushed to ECR
(`ten-fifty5-ml-train`, amd64 digest `sha256:72a01cf2…`), job-def
**`ten-fifty5-ml-train:1`** registered (pinned to that digest, cloned from the
detection job-def's role/env). A `--fact bounce --epochs 5` GPU smoke
**SUCCEEDED** (jobId `b96acf06…`, ~3 min: provision → image pull → corpus
dataset build → train → weights to `s3://…/training/weights/bounce/_latest/`;
val F1 0.47, recall 0.81). Training is now genuinely one command per fact.
⚠️ The smoke left a 5-epoch bounce model in `bounce/_latest/` — do NOT
`--download` it over the deployed `bounce_detector_v2_7match.pt`; the next REAL
(full-epoch, fresh-corpus) bounce train overwrites it. Re-run the steps below
only to REBUILD the image after a trainer change.

The submit/schedule/upload mechanics are proven (job role has
`AmazonS3FullAccess`; GPU jobs schedule on the queue). **Requires Docker running**
(the agent does this per `feedback_agent_handles_deploys`).

```bash
# 0. Auth Docker to ECR (eu primary; add us-east-1 for failover parity).
aws ecr get-login-password --region eu-north-1 | docker login --username AWS \
    --password-stdin 696793787014.dkr.ecr.eu-north-1.amazonaws.com

# 1. Ensure the DETECTION image is present locally (the train image builds FROM it):
docker pull 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker tag  696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest \
            ten-fifty5-ml-pipeline:latest

# 2. Build the TRAINING image (thin layer on top — fast, no torch reinstall):
docker build -f ml_pipeline/Dockerfile.train -t ten-fifty5-ml-train:latest .

# 3. Create the ECR repo (once) + push:
aws ecr create-repository --region eu-north-1 --repository-name ten-fifty5-ml-train 2>/dev/null || true
docker tag ten-fifty5-ml-train:latest \
    696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-train:latest
docker push 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-train:latest

# 4. Extract the amd64 sub-manifest digest (buildx pushes a manifest LIST —
#    same gotcha as the detection image, see handover_t5.md step 6):
MSYS_NO_PATHCONV=1 aws ecr batch-get-image --region eu-north-1 \
    --repository-name ten-fifty5-ml-train --image-ids imageTag=latest \
    --accepted-media-types application/vnd.oci.image.index.v1+json \
        application/vnd.docker.distribution.manifest.list.v2+json \
    --query 'images[0].imageManifest' --output text \
  | python -c "import json,sys;m=json.loads(sys.stdin.read());[print(x['digest']) for x in m['manifests'] if x['platform']['architecture']=='amd64']"

# 5. Register the training job-def pinned to that digest (clones the detection
#    job-def's role/log/retry/timeout + carries DATABASE_URL/S3_BUCKET/AWS_REGION):
.venv/Scripts/python -m ml_pipeline.training.submit_train_job \
    --register-jobdef --digest sha256:____FROM_STEP_4____
```

After step 5, `ten-fifty5-ml-train` exists and `submit_train_job --fact <f>` works.

**Re-build when a trainer changes:** edit a trainer → rebuild + push the train
image (steps 2-4) → register a new `ten-fifty5-ml-train` rev (step 5). The
DETECTION image is untouched (separate Dockerfile/job-def) so a training change
never risks the prod detection pipeline.

---

## Files (this environment)

| File | Role |
|---|---|
| `ml_pipeline/training/batch_train.py` | Unified per-fact GPU entrypoint: dataset(if swing)→train→S3 upload. ENTRYPOINT of the train image. |
| `ml_pipeline/training/submit_train_job.py` | Local helper: submit a Batch job / `--download` weights / `--status` / `--register-jobdef`. |
| `ml_pipeline/Dockerfile.train` | Training image — `FROM` detection image + `db_init.py` + `hit_model/` + `training/`. |
| `ml_pipeline/requirements-train.txt` | Training-dep overlay (currently empty — all deps inherited; add training-only deps here). |
| `ml_pipeline/serve_model/train.py` | serve trainer (coord MLP, reads prod DB). |
| `ml_pipeline/hit_model/train.py` | hit trainer (coord MLP, reads prod DB). |
| `ml_pipeline/training/train_bounce_detector.py` | bounce trainer (1D CNN, reads prod DB). |
| `ml_pipeline/training/train_swing_type.py` | swing trainer (R(2+1)D, GPU) — **owned by main session; CALL it, don't edit.** |
| `ml_pipeline/training/build_swing_type_dataset.py` | swing optical-flow dataset builder — **owned by main session.** |

---

## Readiness per fact (verified on the CPU box 2026-06-14)

- **serve / hit / bounce** — dataset build from prod DB VERIFIED end-to-end
  (serve: 6109 anchors/214 pos; hit: 41246 candidates/1978 pos across 9 tasks;
  bounce manifest builds). Train loop blocked ONLY by the broken local torch
  (`NP_SUPPORTED_MODULES`) — runs clean on the Batch image's torch 2.3.1+cu121.
- **swing** — dataset build VERIFIED end-to-end on CPU (1 match: 106 labels →
  96 flow hits incl. 11 `other`; loads into `SwingTypeDataset` as
  `(2,16,112,112)` + handedness + 4-class label). Train loop is GPU-bound (and
  also hits the local broken-torch wall) → must run on Batch GPU.
- **identity** — no trainer (rule-based; OSNet v2 needs a player-crop label_kind
  that doesn't exist).

## What's left for fully-seamless training of all 5

1. **One-time image build/push + job-def register** (steps above) — needs Docker
   running. Until done, `submit_train_job --fact …` has no job-def to target.
2. **Smoke-validate one cheap GPU job** end-to-end (e.g. `--fact bounce` ~3 min
   GPU) once the image exists — confirms train→S3 upload on real GPU.
3. **swing dataset across all 8 matches** — the smoke built 1 match; the full
   build runs inside the swing job (or pre-bake into the image with
   `--skip-dataset`). Current swing corpus (2301 stroke labels) is approaching
   but below the ADR-02 ~2-3k target; accuracy is train-LAST, gated on more
   full-res uploads (north_star DoD).
4. identity v2 — out of scope until a crop label_kind exists.
