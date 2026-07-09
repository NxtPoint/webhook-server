# James's GPU box — T5 training runbook (Tier-2 REFERENCE)

**Created:** 2026-07-09. **Owner:** Tomo. **Context:** memory `project_james_gpu_box`.
**Purpose:** stand up T5 *training* on James's free L40S box. Sibling of
`.claude/training_environment.md` (the AWS-Batch training path) — this doc is the
same trainers, run on James's box instead of AWS.

Access is **AnyDesk GUI only** (id `1274243906`) — an agent can't drive it; Tomo
runs these steps over AnyDesk (PowerShell on the box). The box is **outbound-only**
(nothing on the internet can connect *in*), so everything here is pull/push-out.

---

## THE HARD RULE — creds gate on password rotation

The box's Windows/AnyDesk passwords are currently reused + were shared in plaintext.
That is **fine for Phase 0** (below) because Phase 0 puts **zero credentials** on the
box. It is **NOT fine once our AWS/DB creds land** (Phase 1). So:

> **Rotate the Windows + AnyDesk passwords BEFORE running Phase 1.** Phase 0 needs no
> rotation and can run today.

When creds do land: use a **dedicated least-privilege IAM user** scoped to only our
S3 bucket/prefixes (+ any queue). **Do NOT put the Render `DATABASE_URL` on the box** —
pre-build the training dataset to S3 and give the box S3-only (avoids
`feedback_render_postgres_ip_allowlist` entirely).

---

## Phase 0 — prove the GPU + our torch work — ✅ DONE + VALIDATED 2026-07-09

**Result:** `python -c "import torch; ..."` printed **`2.3.1+cu121 True NVIDIA L40S`**.
The box runs our exact trainer torch on real CUDA — the wall the CPU dev box hits
(`cuda.is_available()` False) is cleared here.

**Box facts confirmed:** 1 × **NVIDIA L40S, 46 GB** (46068 MiB), driver 596.36,
CUDA 13.2 ceiling (runs our cu121 fine), **TCC compute mode** (headless — hence the
virtual display driver for the AnyDesk GUI), idle. It's **Windows Server**.

**Gotchas hit on this fresh server (do these in order next time):**
1. **No `winget`, no `py`/`python`** preinstalled → grab the python.org installer
   directly: `Invoke-WebRequest` the 3.11.9 amd64 exe, `Start-Process -Wait … /quiet
   InstallAllUsers=1 PrependPath=1`. Open a FRESH shell after (PATH).
2. **Script execution blocked** (Restricted policy) → `Set-ExecutionPolicy -Scope
   Process -Bypass -Force` before `Activate.ps1`.
3. **`fbgemm.dll` WinError 126** on `import torch` → missing **MSVC runtime**; install
   `vc_redist.x64.exe` (https://aka.ms/vs/17/release/vc_redist.x64.exe) `/install /quiet
   /norestart`, then re-run the check. This is THE fix for that error on a clean server.

The commands that got there (PowerShell over AnyDesk, venv at `C:\t5\venv`):

```powershell
# 1. Confirm the NVIDIA driver sees the card(s). L40S should list, 48GB each.
nvidia-smi

# 2. Install Python 3.11 (match the Batch image) if absent.
winget install -e --id Python.Python.3.11    # or python.org installer

# 3. Fresh venv + our exact CUDA torch pins.
py -3.11 -m venv C:\t5\venv
C:\t5\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install torch==2.3.1+cu121 torchvision==0.18.1+cu121 `
    --index-url https://download.pytorch.org/whl/cu121

# 4. The one check that matters:
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

**Pass = step 4 prints `2.3.1+cu121 True NVIDIA L40S`.** That single line proves the
box can run our trainers — the exact wall our CPU dev box hits
(`torch.cuda.is_available()` False + broken CPU wheel, per
`feedback_train_in_batch_image_not_dev_venv`) is cleared here on real CUDA.

If step 1 shows no card / a display-only driver, James needs the **L40S datacenter
driver** installed (the virtual-display driver he mentioned is separate).

---

## Phase 1 — real training — ✅ FIRST GPU TRAIN SUCCEEDED 2026-07-09

`batch_train --fact bounce --epochs 5 --no-upload` trained on the L40S after wiring the
DB role + S3 creds. Setup receipts / gotchas hit (in order):
- **`t5_train_ro` DB role:** created via `psql "$DATABASE_URL"` in the Render *service*
  shell (NOT pasting SQL into bash directly). `ALTER DEFAULT PRIVILEGES` needs `ON TABLES`
  (my first version omitted it). **The prod dbname is `sportai_db`** — a wrong dbname in the
  box DSN surfaces as `FATAL: password authentication failed`, not a "db not found" error
  (cost us two debugging rounds). Verify the ro login from the Render shell first to isolate
  DB-side from DSN-side. Password: letters+digits only (symbols break the `postgresql://`
  URL parse → same misleading auth error).
- **S3 creds ARE required even for `--no-upload`:** the corpus *registry* is in the DB
  (`ml_analysis.training_corpus`) but the actual per-task **labels live in S3**
  (`training/labels/<task>_ball_positions.json`). No creds → every task skipped → "manifest
  is empty". So the box needs BOTH the `t5_train_ro` DSN AND the `t5-train-james-box` S3
  key (scoped IAM: GetObject on `training/labels/*` + `training/corpus/*`, GetObject/PutObject
  on `training/weights/*`; ListBucket NOT needed — trainer fetches by exact key).
- Env on the box are session-only `$env:` vars (safer for the smoke); persist via an AWS
  credentials file + a scheduled task for the unattended setup (Phase 1 step E, TODO).

### The original plan (kept for reference)

Native Windows, no Docker needed (trainers are torch + cv2 + numpy + sqlalchemy +
psycopg + boto3 — all pip-installable). `batch_train.py --fact <f>` is the SAME
entrypoint as AWS Batch (`.claude/training_environment.md`), runnable directly on this
box. Facts: serve/hit/bounce read prod DB; swing needs a built dataset (corpus JSON +
720p S3 video) and is GPU-bound.

**Two decisions before you start (see chat):**
- **(D1) Repo transfer:** git clone with a fine-grained read-only PAT (best for later
  `git pull` sync) vs AnyDesk file-transfer a zip (no GitHub cred). Recommend git.
- **(D2) DB access:** the smoke reads prod PG, so the box's **egress IP must be
  allowlisted on Render PG** (`feedback_render_postgres_ip_allowlist`) and it needs a
  `DATABASE_URL`. Use a **read-only role scoped to the T5 read schemas**, NOT the master
  URL — don't put the crown-jewels DSN on someone else's box. (Agent to supply the exact
  CREATE ROLE / GRANT SQL once we confirm which schemas the trainers read.)

### Step A — repo + deps
```powershell
# Git: install from https://git-scm.com/download/win (browser is on the taskbar), defaults.
cd C:\t5
git clone https://<PAT>@github.com/NxtPoint/webhook-server.git
cd C:\t5\webhook-server
C:\t5\venv\Scripts\Activate.ps1
# Training deps ONLY (do NOT reinstall torch/torchvision — Phase 0 already has the CUDA build):
pip install opencv-python==4.9.0.80 numpy==1.26.4 scipy==1.13.1 boto3==1.34.131 `
    sqlalchemy==2.0.31 "psycopg[binary]==3.1.19" pandas matplotlib seaborn tqdm
```

### Step B — DB reachability (D2)
```powershell
# 1. Get the box's egress IP → add it to Render PG's allowlist (Render dashboard).
Invoke-RestMethod https://api.ipify.org      # no VPN on this box, so this is the real egress IP
# 2. Set the read-only DSN for THIS shell only (not persisted, not in git):
$env:DATABASE_URL = "postgresql://t5_train_ro:<pw>@<host>.render.com/<db>?sslmode=require"
```

### Step C — the smoke (DB-read only, NO S3 creds, nothing uploaded)
```powershell
python -m ml_pipeline.training.batch_train --fact bounce --epochs 5 --no-upload
```
Pass = it reads the corpus from PG, builds the bounce dataset, trains 5 epochs **on the
L40S**, prints val metrics, writes `ml_pipeline/models/bounce_detector_v2_7match.pt`
locally. `--no-upload` deliberately avoids clobbering the deployed S3 `_latest` weight.
(If an `import` fails for a missing dep, `pip install` it and re-run — the trainer import
chain may pull one package beyond the list above; that's expected first-run iteration.)

### Step D — real uploading run (adds least-priv S3, after the smoke is green)
```powershell
$env:AWS_ACCESS_KEY_ID="…"; $env:AWS_SECRET_ACCESS_KEY="…"   # dedicated least-priv IAM user
$env:AWS_REGION="eu-north-1"; $env:S3_BUCKET="nextpoint-prod-uploads"
python -m ml_pipeline.training.batch_train --fact bounce --epochs 50
```
Uploads to `s3://nextpoint-prod-uploads/training/weights/bounce/_latest/`. The rest of
the weights-sync flow (download → detection rebuild → new job-def rev, rule #8) is
unchanged from `.claude/training_environment.md`.

### Step E — unattended
Wrap the trainer in a Windows Scheduled Task (Task Scheduler, "run whether logged on or
not") so runs survive reboot + AnyDesk logoff. AnyDesk is only for setup/monitoring.

---

## Phase 2 — detection inference as a pull-worker (LATER, keep AWS primary)

Only if we want to offload AWS inference cost. Box long-polls a queue (SQS or an S3
"pending" list) — outbound — claims a job, pulls video from S3, runs the pipeline,
writes the JSON export + status back to S3. That matches our existing S3-in/S3-out
interface so the Render re-ingest is unchanged. Run our Linux Batch image under
**WSL2 + Docker Desktop + NVIDIA Container Toolkit** for 1:1 parity. AWS stays the
reliable path; the home box is overflow/cost-saver, never the sole path.

---

## Open questions (from Tomo/James)

1. GPU count (confirm L40S, 48GB each)?  2. CPU cores / RAM / free SSD?
3. WSL2 + Docker + NVIDIA Container Toolkit installable (Phase 2)?  4. Can it run an
unattended service surviving reboot?  5. IP static or dynamic? (doesn't block us —
we stay outbound-only.)
