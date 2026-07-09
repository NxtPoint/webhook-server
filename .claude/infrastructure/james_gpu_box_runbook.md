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

## Phase 1 — real training (AFTER password rotation + least-priv creds)

Native Windows, no Docker needed for training (the trainers are torch + cv2 + numpy +
sqlalchemy + boto3 — all pip-installable). WSL2/Docker is only for Phase 2 (inference).

1. **Get the repo on the box.** Private GitHub → either a scoped read-only PAT clone,
   or AnyDesk file-transfer a zip of `C:\dev\webhook-server`. Then:
   ```powershell
   cd C:\t5\webhook-server
   C:\t5\venv\Scripts\Activate.ps1
   pip install -r ml_pipeline\requirements.txt   # torch already installed in Phase 0
   ```
2. **Give the box S3-only creds** (dedicated least-priv IAM user; env vars, not in git).
   Prefer pre-building the dataset to S3 so no `DATABASE_URL` is needed.
3. **Smoke one fact** — identical entrypoint to the AWS path (`.claude/training_environment.md`):
   ```powershell
   python -m ml_pipeline.training.batch_train --fact bounce --epochs 5 --no-upload
   ```
   Success = it trains and prints val metrics. Then a real run **with** `--upload`
   pushes weights to `s3://nextpoint-prod-uploads/training/weights/<fact>/_latest/`
   exactly like the Batch job — the rest of the weights-sync flow (download → rebuild
   detection image → new job-def rev) is unchanged from the AWS runbook.
4. **Unattended:** wrap the trainer in a Windows scheduled task / service so runs
   survive reboot + AnyDesk logoff.

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
