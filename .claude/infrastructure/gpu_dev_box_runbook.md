# GPU Dev Box Runbook — `t5-dev-gpu`

**Created:** 2026-05-20 (Stream A of the 2026-05-20 infrastructure session).
**Owner:** Tomo. **Region:** eu-north-1 (Stockholm).
**Purpose:** Interactive ML development for the T5 pipeline — Phase 5 ball-coverage experiments, WASB drop-in A/B, TrackNet training, dual-submit corpus assembly.

---

## What's provisioned (one-time, already done)

| Resource | Identifier |
|---|---|
| EC2 instance | `i-0fb3983fa555c16e3` (`t5-dev-gpu`), g4dn.xlarge, eu-north-1a |
| AMI | `ami-0db574be841d285ac` (Deep Learning OSS Nvidia Driver AMI GPU PyTorch 2.7, Ubuntu 22.04, 2026-04-28) |
| EBS root | 50 GB gp3, `DeleteOnTermination=true` |
| Security group | `sg-0f46e482a47124570` (`t5-dev-sg`) — inbound SSH/22 from `31.14.252.13/32` only |
| Key pair | `t5-dev` (private key at `C:\Users\tomos\.ssh\t5-dev.pem`, `chmod 600`) |
| IAM role | `t5-dev-instance-role` — read on `s3://nextpoint-prod-uploads`, write on `fixtures/*` + `training/*` |
| Instance profile | `t5-dev-instance-profile` |
| Tags | `Name=t5-dev-gpu`, `Project=T5`, `Owner=Tomo`, `Purpose=dev` |

**Cost:** g4dn.xlarge on-demand = $0.526/hr while running. EBS = ~$0.005/hr = ~$3.70/mo while stopped. **Default to start/stop per session.**

---

## Start / stop / SSH

All commands use `aws` CLI with the default `nextpoint-uploader` creds (admin) on Windows. The instance public DNS / IP **changes on every start** — re-query after start.

### Start the box

```bash
aws ec2 start-instances --region eu-north-1 --instance-ids i-0fb3983fa555c16e3
aws ec2 wait instance-status-ok --region eu-north-1 --instance-ids i-0fb3983fa555c16e3
PUBLIC_DNS=$(aws ec2 describe-instances --region eu-north-1 \
  --instance-ids i-0fb3983fa555c16e3 \
  --query 'Reservations[0].Instances[0].PublicDnsName' --output text)
echo "PUBLIC_DNS=$PUBLIC_DNS"
```

Typical wait: ~2-3 min from `start-instances` to status_ok.

### SSH in

```bash
ssh -i ~/.ssh/t5-dev.pem ubuntu@$PUBLIC_DNS
```

If you've changed home networks: the SG only allows your previous IP. Update via:
```bash
# Get current IP
NEW_IP=$(curl -s ifconfig.me)
echo "$NEW_IP/32"
# Revoke old rule (only the one rule exists)
aws ec2 revoke-security-group-ingress --region eu-north-1 \
  --group-id sg-0f46e482a47124570 \
  --protocol tcp --port 22 --cidr <OLD_IP>/32
# Authorize new rule
aws ec2 authorize-security-group-ingress --region eu-north-1 \
  --group-id sg-0f46e482a47124570 \
  --protocol tcp --port 22 --cidr $NEW_IP/32
```

### Stop the box (halt compute charges)

```bash
aws ec2 stop-instances --region eu-north-1 --instance-ids i-0fb3983fa555c16e3
```

### Terminate completely (nukes the EBS volume too — last resort)

```bash
aws ec2 terminate-instances --region eu-north-1 --instance-ids i-0fb3983fa555c16e3
# To fully clean up, also delete: SG, IAM role/policy/profile, key pair
# Commands at end of this doc.
```

---

## What's already installed on the box

After provisioning, the following are in place (verified 2026-05-20):

- Ubuntu 22.04 LTS, kernel 6.8 with NVIDIA driver 580.126.09
- CUDA 12.x runtime
- Python 3.10.12
- venv at `/opt/t5-venv` with:
  - `torch==2.5.1+cu121` (CUDA-enabled, confirmed `torch.cuda.is_available()` on Tesla T4)
  - `torchvision`
  - `opencv-python-headless==4.13.0`
  - `numpy`, `boto3`
- AWS CLI v2 (uses instance role automatically — no creds needed for S3)

**To activate the venv on SSH login:**
```bash
source /opt/t5-venv/bin/activate
```

Add to `~/.bashrc` to auto-activate on every SSH:
```bash
echo 'source /opt/t5-venv/bin/activate' >> ~/.bashrc
```

---

## Syncing the T5 project to the box

The project is private (`NxtPoint/webhook-server`), so `git clone` requires credentials. **Two paths:**

### Path A — `rsync` from Windows local checkout (recommended for first run)

From Git Bash on Windows:
```bash
# Excludes: .venv (huge, will reinstall on box), .git (not needed), _archive
# (deprecated code), training/datasets (heavy intermediate data), visual_debug
# (debug images), __pycache__ (Python bytecode).
#
# NOTE: ml_pipeline/test_videos/ is INCLUDED on purpose — the bench fixtures
# point at local mp4 paths there (e.g. a798eff0_sa_video.mp4, match_90ad59a8.mp4).
# Excluding it broke the first ball-tracker bench attempt 2026-05-21. If you have
# a custom oversized video in that dir that you really don't want on the box,
# exclude that one file specifically rather than the whole directory.
rsync -avz --progress \
  --exclude '.venv/' --exclude '.git/' \
  --exclude 'ml_pipeline/_archive/' \
  --exclude 'ml_pipeline/diag/_archive/' \
  --exclude 'ml_pipeline/training/datasets/' \
  --exclude 'ml_pipeline/training/visual_debug/' \
  --exclude '__pycache__/' \
  -e "ssh -i $HOME/.ssh/t5-dev.pem" \
  /c/dev/webhook-server/ ubuntu@$PUBLIC_DNS:~/webhook-server/
```

Estimated size: ~400 MB (model weights ~270 MB, test_videos ~50-100 MB). Takes a few minutes on first run; rsync incrementals after.

### The agent-and-Tomo split (how to run bench loops without burning context)

When an agent (this one or any session) is building a bench / training / experiment that needs the GPU box, the workflow is:

1. **Agent (in chat):** writes code locally on Windows, commits to git, pushes to origin/main. Does NOT touch the box.
2. **Tomo (one-time per session):** starts the box, rsyncs the project up, SSHes in, runs whatever the agent says, pastes output back to the agent.
3. **Agent (in chat):** reads the pasted output, decides next step, commits any new code (including new baseline JSONs the run produced).
4. **Tomo (end of session):** rsyncs any output artefacts (baseline JSONs, training logs) DOWN before stopping the box, then stops it.

```bash
# Tomo end-of-session: pull down any new artefacts before stopping
rsync -avz -e "ssh -i $HOME/.ssh/t5-dev.pem" \
  ubuntu@$PUBLIC_DNS:~/webhook-server/ml_pipeline/diag/ \
  /c/dev/webhook-server/ml_pipeline/diag/
aws ec2 stop-instances --region eu-north-1 --instance-ids i-0fb3983fa555c16e3
```

The agent never SSHes into the box directly. The box is a tool Tomo wields, not an environment the agent lives in. Keeps the agent's context window clean and avoids hidden state.

### Path B — `git clone` with a GitHub PAT

1. Create a fine-scoped PAT at https://github.com/settings/tokens?type=beta with read access to `NxtPoint/webhook-server` only, 7-day expiry.
2. On the box:
   ```bash
   git clone https://<PAT>@github.com/NxtPoint/webhook-server.git ~/webhook-server
   ```
3. Weights are **git-ignored** — still need Path A or S3 sync for those.

For active development, **Path A is friction-free** (no PAT rotation, picks up your local edits immediately). Path B is right if you want a clean checkout to test against `origin/main`.

### Sync model weights from local

If using Path B (git clone) or a fresh box, model weights need a separate sync:

```bash
# From Windows Git Bash
scp -i $HOME/.ssh/t5-dev.pem \
  /c/dev/webhook-server/ml_pipeline/models/*.pt \
  /c/dev/webhook-server/ml_pipeline/models/*.pth \
  /c/dev/webhook-server/ml_pipeline/models/*.tar \
  ubuntu@$PUBLIC_DNS:~/webhook-server/ml_pipeline/models/
```

Confirmed weights present in local `ml_pipeline/models/`:
- `tracknet_v2.pt` (TrackNet V2 baseline)
- `tracknet_v2_finetuned.pt` (local finetune, no manifest)
- `court_keypoints.pth` (court detector CNN)
- `yolov8m.pt`, `yolov8m-pose.pt`, `yolov8x-pose.pt` (player tracking)
- `wasb_tennis_best.pth.tar` (WASB — already downloaded, not yet integrated)

---

## Install project requirements on the box

After sync:
```bash
ssh -i ~/.ssh/t5-dev.pem ubuntu@$PUBLIC_DNS
cd ~/webhook-server
source /opt/t5-venv/bin/activate
# Pipeline-specific deps. ml_pipeline/requirements.txt has the right list.
pip install -r ml_pipeline/requirements.txt
```

Likely the pipeline needs `sqlalchemy`, `psycopg`, and a few CV deps that aren't in the base venv. `pip install -r` will catch them all.

---

## The first validation (what to run after first sync)

This was originally Stream A's deliverable but the BallTracker test needs the project code synced first (above). Run after that:

```bash
ssh -i ~/.ssh/t5-dev.pem ubuntu@$PUBLIC_DNS
source /opt/t5-venv/bin/activate
cd ~/webhook-server

# 1. Pull a sample video from S3 (using instance IAM role — no creds needed)
mkdir -p ~/sample_videos
# Look up the s3_key for the target task — check bronze.submission_context.meta_json
# For task 880dff02 the wix-uploads/ key is in DB; use Render shell or hardcode.
# Example placeholder:
SAMPLE_KEY="wix-uploads/<some_video>.mp4"
aws s3 cp "s3://nextpoint-prod-uploads/$SAMPLE_KEY" ~/sample_videos/880dff02.mp4
ls -lh ~/sample_videos/880dff02.mp4

# 2. Run a 750-frame BallTracker test, log per-frame source labels
python - <<'PY'
import sys, time, pickle, json
sys.path.insert(0, "/home/ubuntu/webhook-server")
from ml_pipeline.ball_tracker import BallTracker
import cv2

tracker = BallTracker()
cap = cv2.VideoCapture("/home/ubuntu/sample_videos/880dff02.mp4")
results = []
t0 = time.time()
for idx in range(750):
    ok, frame = cap.read()
    if not ok: break
    det = tracker.detect_frame(frame, idx)
    if det is not None:
        # Source tag tracking — BallTracker._diag has per-tier counters; record which fired
        diag = tracker._diag.copy()
        results.append({
            "frame_idx": idx,
            "x": det.x, "y": det.y,
            "diag_snapshot": diag,
        })
cap.release()
elapsed = time.time() - t0
print(f"detected={len(results)} of 750 in {elapsed:.1f}s -> {750/elapsed:.1f} fps")
print(f"final diag={json.dumps(tracker._diag, indent=2, default=int)}")

# 3. Save pickle and upload to S3
out = "/tmp/balltracker_880dff02_first750.pkl"
with open(out, "wb") as f:
    pickle.dump({"task_id": "880dff02", "n_frames": 750, "results": results, "diag": tracker._diag}, f)
import boto3
boto3.client("s3").upload_file(out, "nextpoint-prod-uploads", "fixtures/balltracker_880dff02_first750.pkl")
print("uploaded -> s3://nextpoint-prod-uploads/fixtures/balltracker_880dff02_first750.pkl")
PY

# 4. Confirm GPU was actually used
nvidia-smi
```

**Target:** <60 s wall clock on 750 frames (≥12 fps end-to-end).

---

## What the box is good for (recommended uses)

1. **WASB integration A/B** — the weights are already in `ml_pipeline/models/wasb_tennis_best.pth.tar`. Wire a `WASBTracker` class that mirrors the `BallTracker` interface, run both on the same sample frames, measure coverage delta. Don't need Batch round-trips for this.
2. **Per-component bench harness for `ball_tracker.py`** — cached frame stacks → run `BallTracker` locally on every code change → seconds per iteration.
3. **TrackNet V2 fine-tune training** (`train_tracknet.py`) — once `AUTO_DUAL_SUBMIT_T5` is on and the corpus has ≥10 matches.
4. **Stroke classifier training** (`stroke_classifier/train.py`) — same dependency on dual-submit corpus.
5. **TOTNet experimentation** — clone the TOTNet repo, train against dual-submit corpus when WASB plateaus.

---

## Gotchas / what NOT to do

- **Don't leave the instance running overnight.** $0.526/hr × 12 hr = $6.30 wasted. Always `stop` when done.
- **Don't terminate without confirming.** Termination nukes the 50 GB EBS volume and re-syncing the project + weights takes 10-15 min.
- **Don't write secrets to the box.** No DB URLs, no API keys. Use the instance role for S3 (already does); for anything else, SSM Parameter Store would be the next step (not provisioned).
- **Don't disable IMDSv2.** The instance is configured with `HttpTokens=required` to prevent SSRF-style metadata theft.
- **Don't open the SG to 0.0.0.0/0.** Always single-IP. If your IP changes, update the SG (commands above).
- **Don't `pip install --user`** — installs leak between sessions and break the venv. Always activate the venv first.

---

## Full cleanup (when permanently retiring this box)

```bash
INSTANCE_ID=i-0fb3983fa555c16e3
SG_ID=sg-0f46e482a47124570

# 1. Terminate instance (also kills EBS via DeleteOnTermination)
aws ec2 terminate-instances --region eu-north-1 --instance-ids $INSTANCE_ID
aws ec2 wait instance-terminated --region eu-north-1 --instance-ids $INSTANCE_ID

# 2. Delete SG
aws ec2 delete-security-group --region eu-north-1 --group-id $SG_ID

# 3. Delete IAM instance profile + role
aws iam remove-role-from-instance-profile \
  --instance-profile-name t5-dev-instance-profile \
  --role-name t5-dev-instance-role
aws iam delete-instance-profile --instance-profile-name t5-dev-instance-profile
aws iam delete-role-policy --role-name t5-dev-instance-role --policy-name s3-read-uploads-and-write-training
aws iam delete-role --role-name t5-dev-instance-role

# 4. Delete key pair
aws ec2 delete-key-pair --region eu-north-1 --key-name t5-dev
rm ~/.ssh/t5-dev.pem
```

---

## Resource IDs at a glance (for scripting)

```bash
export T5_DEV_INSTANCE_ID=i-0fb3983fa555c16e3
export T5_DEV_SG_ID=sg-0f46e482a47124570
export T5_DEV_KEY_PATH="$HOME/.ssh/t5-dev.pem"
export T5_DEV_AMI_ID=ami-0db574be841d285ac
export T5_DEV_SUBNET_ID=subnet-07afaad4add38d2ab
export T5_DEV_VPC_ID=vpc-0173f4ef06c2c3660
export T5_DEV_INSTANCE_PROFILE=t5-dev-instance-profile
export T5_DEV_IAM_ROLE=t5-dev-instance-role
```

---

## Status at handover (2026-05-20)

- [x] Instance provisioned (g4dn.xlarge, Tesla T4 verified)
- [x] SSH from home IP works
- [x] PyTorch 2.5.1+cu121 venv at `/opt/t5-venv`, CUDA confirmed
- [x] cv2 + boto3 installed
- [x] S3 access via instance IAM role verified
- [x] Instance **stopped** (compute charges halted)
- [ ] **Project not yet synced to box** — pending Tomo's preference (rsync vs PAT clone)
- [ ] **BallTracker validation not yet run** — depends on project sync + sample video s3_key
- [ ] **Pickle for parallel agent not yet produced** — depends on BallTracker validation

**Total session spend: ~$0.25** (instance ran ~30 min during foundation verification, then stopped).
