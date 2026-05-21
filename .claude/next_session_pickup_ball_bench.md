# Ball-tracker bench — next session pickup

**Status at handover:** Bench infrastructure committed and pushed to `origin/main`. **No baseline locked yet** — needs a GPU run on the t5-dev-gpu box. The two open work items in this thread are (a) lock the TrackNetV2 baseline on both fixtures, (b) run WASB through the same bench to decide the swap.

**Tomo's strategic frame for this work:** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. The ball-tracker bench is the safety net for the WASB swap (audit #4 → #3 gating), which is the highest-leverage bronze-coverage win on the punch list.

---

## TL;DR — where we are

- ✅ Scaffolding committed (replay + bench + snapshot + 2 fixtures + doc corrections). 4 commits on origin/main, latest `0546278`.
- ✅ Bench CLI is end-to-end verified locally (imports clean, fixture loads, video decodes via `VideoPreprocessor` at the correct 25fps-sampled index space).
- ✅ Fixtures focused on the Phase 5 win condition (880dff02 SA point 6) not the serve-detection misses I originally proposed.
- ✅ `.claude/strategy/infrastructure_audit_2026-05-20.md` §9 caveat clarified to apply only to silver bench (not ball bench). Doc fix in same push.
- 🟡 **Baseline not yet locked** — needs GPU run.
- 🟡 **WASB not yet benched against TrackNetV2.**

---

## What's committed (4 commits, all on `origin/main`)

| Commit | Files | Purpose |
|---|---|---|
| `e487204` | `CLAUDE.md` | PowerShell shell note, session-file pickup hint, bench in T5 commands |
| `0d9c9ee` | `ml_pipeline/diag/{replay_ball,bench_ball,snapshot_task_ball}.py` + `ml_pipeline/fixtures_ball/a798eff0.json` | Bench scaffolding |
| `4867ccc` | `ml_pipeline/fixtures_ball/{a798eff0,880dff02}.json` | Fixtures focused on Phase 5 win condition |
| `0546278` | `.claude/infrastructure/gpu_dev_box_runbook.md` + `.claude/strategy/infrastructure_audit_2026-05-20.md` | Runbook keeps test_videos in rsync; §9 caveat narrowed to silver-only |

## What the bench measures

Per `(fixture, tracker)`:

| Metric | Verdict role |
|---|---|
| `detection_rate` | Headline — drop is a regression |
| `sa_bounce_recall` | Phase 5 progress — drop is a regression. WASB's +9pp F1 claim resolves here. |
| `tier_dist` | Diagnostic only (TrackNetV2's per-tier counters) |
| `runtime_sec` | Tracked, not used in verdict |

A red bench is a drop on `detection_rate` OR `sa_bounce_recall`, on either tracker.

## Fixtures (both reference `ml_pipeline/test_videos/a798eff0_sa_video.mp4`)

**`a798eff0.json` — regression guard for serve-bench compatibility.** 3 windows / 938 frames / 3 SA anchors. Warmup + SA point 5 rally + far_miss_458. Exists so tracker changes that break the serve bench surface early.

**`880dff02.json` — Phase 5 progress guard.** 3 windows / 1105 frames / 9 SA anchors. Warmup + SA point 6 [5599, 6003] (the canonical "TrackNetV2 finds zero balls" 9-stroke rally per `docs/_investigation/may07_sa_point6_gap.md`) + a 400-frame slice from inside the 73.2s coverage gap. **This is where the WASB swap is justified or rejected.**

The 9 sa_bounce_frames on 880dff02 SA point 6 are computed from the stroke-spacing table in the investigation doc: `[5624, 5649, 5686, 5712, 5811, 5848, 5887, 5929, 5978]`.

## Baseline status — UNLOCKED

`ml_pipeline/diag/bench_ball_baseline.json` does not exist yet. The bench will run with no baseline and print all-zeros deltas; `--update-baseline` writes the file. Lock TrackNetV2 first, then run WASB to compare against the now-locked TrackNetV2 row.

| fixture | tracker | detection_rate | sa_bounce_recall |
|---|---|---|---|
| a798eff0 | tracknet_v2 | TBD | TBD |
| a798eff0 | wasb | TBD | TBD |
| 880dff02 | tracknet_v2 | TBD | TBD (expected ~0 on sa_point_6 — that's the whole point) |
| 880dff02 | wasb | TBD | **TBD — must be materially > 0 on sa_point_6 for the swap to be worth it** |

## Next session — the run

GPU box: `i-0fb3983fa555c16e3` in eu-north-1, stopped by default. Workflow per `.claude/infrastructure/gpu_dev_box_runbook.md`. The agent/Tomo split: Tomo drives the box; agent writes code + reads pasted output.

**Tomo (one-time per session):**
```bash
aws ec2 start-instances --region eu-north-1 --instance-ids i-0fb3983fa555c16e3
aws ec2 wait instance-status-ok --region eu-north-1 --instance-ids i-0fb3983fa555c16e3
PUBLIC_DNS=$(aws ec2 describe-instances --region eu-north-1 \
  --instance-ids i-0fb3983fa555c16e3 \
  --query 'Reservations[0].Instances[0].PublicDnsName' --output text)

# Rsync (the runbook command — test_videos NOT excluded as of commit 0546278)
rsync -avz --progress \
  --exclude '.venv/' --exclude '.git/' \
  --exclude 'ml_pipeline/_archive/' --exclude 'ml_pipeline/diag/_archive/' \
  --exclude 'ml_pipeline/training/datasets/' \
  --exclude 'ml_pipeline/training/visual_debug/' \
  --exclude '__pycache__/' \
  -e "ssh -i $HOME/.ssh/t5-dev.pem" \
  /c/dev/webhook-server/ ubuntu@$PUBLIC_DNS:~/webhook-server/

ssh -i ~/.ssh/t5-dev.pem ubuntu@$PUBLIC_DNS
```

**On the box:**
```bash
cd ~/webhook-server
source /opt/t5-venv/bin/activate
python -c "import torch; print('cuda:', torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# Sanity-check both fixtures decode + both trackers load
python -m ml_pipeline.diag.replay_ball ml_pipeline/fixtures_ball/a798eff0.json --tracker tracknet_v2
# Full bench, both trackers, both fixtures, write baseline JSON
python -m ml_pipeline.diag.bench_ball --update-baseline
cat ml_pipeline/diag/bench_ball_baseline.json
```

Expected runtime: ~1-3 min on Tesla T4 for 2,043 total frames × 2 trackers.

**End of session (rsync DOWN before stopping — the baseline JSON is the only artefact we need to keep):**
```bash
rsync -avz -e "ssh -i $HOME/.ssh/t5-dev.pem" \
  ubuntu@$PUBLIC_DNS:~/webhook-server/ml_pipeline/diag/bench_ball_baseline.json \
  /c/dev/webhook-server/ml_pipeline/diag/
git add ml_pipeline/diag/bench_ball_baseline.json
git commit -m "ball-bench: lock initial baseline (tracknet_v2 + wasb on a798eff0 + 880dff02)"
git push origin main
aws ec2 stop-instances --region eu-north-1 --instance-ids i-0fb3983fa555c16e3
```

## Decision rule (post-baseline-lock)

After both trackers run, read the table. The WASB swap is justified IFF:

1. WASB's `sa_bounce_recall` on `880dff02.sa_point_6` is materially > TrackNetV2's (which should be near zero).
2. WASB's `detection_rate` inside `880dff02.gap_73s_slice` is materially > TrackNetV2's.
3. WASB does NOT regress on the a798eff0 fixture (regression guard).

If (1) and (2) hold and (3) is clean, plan the production swap. If WASB regresses on (3), investigate before deciding — the swap might still be net-positive but it stops being a free lunch.

## Things NOT to do

- **Don't lock the baseline locally on CPU.** Will work but takes ~30 min for 2,043 frames + the numbers will be model-fp32-on-CPU, slightly different from the production-fp16-on-GPU path. Use the GPU box.
- **Don't add an S3 URI fallback to `replay_ball.py` yet.** Considered and explicitly deferred (build-when-needed). The local-path-in-fixture coupling is fine for now.
- **Don't drop the test_videos/ rsync exclude back in.** The first bench attempt broke for this reason; the runbook now keeps it on purpose.
- **Don't use the §9 sequencing caveat to delay locking the ball-bench baseline.** The caveat is silver-bench-specific (clarified in commit `0546278`).
- **Don't merge the `gap_73s_slice` window into `sa_point_6`.** They measure different things (recall vs raw detection rate in a coverage void) and the bench reports them separately on purpose.

## Open admin items (cross-session)

- **Render Postgres still open to `0.0.0.0/0`** (from the 2026-05-21 Phase 5a Stage 2 session). Re-lock to home IP or build NAT Gateway + EIP. See `feedback_render_postgres_ip_allowlist`.
- **Batch verification of Option A** — task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` was uploaded earlier in this session; Tomo to confirm SUCCEEDED + verify `ml_analysis.ball_detections WHERE job_id = '6a8a344f-...' AND source = 'roi_prod'` is non-zero.
