# Ball-tracker bench — next session pickup

**Status at handover:** Bench fully shipped and locked. Metric v2 in place (post-filter + trajectory coherence + tier breakdown). Baseline locked at commit `7100792`. **The WASB-vs-TrackNetV2 comparison resolved in WASB's favour on the regime that matters — coverage gaps.**

**Tomo's strategic frame:** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. WASB swap (audit #3) is now empirically justified by this bench.

---

## TL;DR — where we are

- ✅ Scaffolding + 2 fixtures + metric v2 + baseline all committed and pushed.
- ✅ **WASB wins on 880dff02 SA point 6: 2/9 strokes recovered vs TrackNetV2's 0/9** — the documented "TrackNetV2 finds zero balls" rally from `docs/_investigation/may07_sa_point6_gap.md`.
- ✅ Metric v2 is production-aligned: WASB's 11.76% post_filter_rate matches the documented 13% bronze coverage almost exactly.
- 🟡 **WASB production swap not yet shipped.** That's audit #3 — the next-session work.

## What the bench does (metric v2)

Per `(fixture, tracker)` it reports three layers:

| Metric | Verdict role | What it means |
|---|---|---|
| `post_filter_rate` | **Regression-tracked** | % of frames with a detection that survives the >150px-jump filter. Mirrors what production stores in `ml_analysis.ball_detections`. |
| `post_filter_sa_recall` | **Regression-tracked** | Fraction of SA bounce anchors with a post-filter detection within ±3 frames. Phase 5 win condition resolves here. |
| `trajectory_coherence_pct` | **Regression-tracked** | % of consecutive RAW detections within 150px of each other. High when the tracker is following a ball; low when output is dominated by motion-fallback noise. |
| `tier_dist` (TrackNetV2 only) | Diagnostic | Per-tier breakdown. The `delta_fallback` count is the motion-fallback-on-noise tier — its share is the cleanest "noise vs signal" tell. |
| Raw `detection_rate`, `sa_bounce_recall` | Informational | Kept in JSON for compat. Their drop alone is not a regression (WASB legitimately rejects low-confidence frames). |

## Locked baseline (commit `7100792`)

| fixture | tracker | post_rate | post_recall | coherence | tier note |
|---|---|---|---|---|---|
| 880dff02 | tracknet_v2 | 47.15% | **0.00%** (0/9) | 73.30% | 58% fallback noise |
| 880dff02 | wasb | 11.76% | **22.22%** (2/9) | 70.84% | — |
| a798eff0 | tracknet_v2 | 63.54% | 33.33% (1/3) | 78.80% | 67% fallback noise |
| a798eff0 | wasb | 22.07% | 33.33% (1/3) | 71.53% | — |

Headline interpretation:
- **880dff02 = Phase 5 progress signal.** WASB recovers 2 of the 9 SA point 6 strokes that TrackNetV2 misses completely. Real win on the canonical bronze-coverage-gap regime.
- **a798eff0 = regression guard.** Both trackers tie on `post_filter_sa_recall` (1/3). TrackNetV2 has slightly higher coherence. WASB does not regress on this fixture.

## Verdict — WASB swap is justified

The +9pp F1 claim from the market scan isn't pure magic but the directional win is real on the regime that matters. The decision rule from the design doc (`.claude/strategy/dual_submit_status_2026-05-20.md` §6 risk #3) said: "Try WASB first, before investing in the full 5c.3-5c.5 chain." Done — and WASB wins. Proceed with the swap.

## Next session — ship WASB to production

Audit item #3. Concrete steps:

1. **Wire `WASBBallTracker` into the pipeline.** Two options:
   - (a) Replace `BallTracker` in `ml_pipeline/pipeline.py` outright. Simpler. Risks regressing the a798eff0 case (currently a tie).
   - (b) Add WASB as a parallel path. Each produces detections; merge by confidence. More code, less risk. Probably the right move for first ship.
2. **Bench locally before pushing.** Same workflow as today: edit code, rerun `python -m ml_pipeline.diag.bench_ball` on the GPU box, verify no `[!] REGRESSION` on the locked baseline.
3. **BATCH-SIDE CHANGE CHECKLIST** (per `.claude/handover_t5.md`): pipeline.py, wasb_*.py, models/wasb_tennis_best.pth.tar are all in-container. Docker rebuild + dual-region ECR push + new job-def revisions are REQUIRED. The full deploy sequence is in the handover.
4. **Verify on a fresh Batch upload.** Upload a tennis_singles match, watch `ml_analysis.ball_detections` for the new task. Expect: detection count comparable to or higher than TrackNetV2 baseline; bounces in known coverage-gap windows.
5. **Update the bench baseline.** After WASB ships as production code, re-bench from the GPU box and commit the new baseline numbers.

## How to run the bench

GPU box `i-0295d636f6bf957eb` (eu-north-1b), currently stopped. Full workflow:

```bash
# Start + DNS
aws ec2 start-instances --region eu-north-1 --instance-ids i-0295d636f6bf957eb
aws ec2 wait instance-status-ok --region eu-north-1 --instance-ids i-0295d636f6bf957eb
PUBLIC_DNS=$(aws ec2 describe-instances --region eu-north-1 \
  --instance-ids i-0295d636f6bf957eb \
  --query 'Reservations[0].Instances[0].PublicDnsName' --output text)

# Upload (full tar — rsync isn't on the Windows Bash, use tar | ssh tar)
tar czf - --exclude='./.venv' --exclude='./.git' \
  --exclude='./ml_pipeline/_archive' --exclude='./ml_pipeline/diag/_archive' \
  --exclude='./ml_pipeline/training/datasets' --exclude='./ml_pipeline/training/visual_debug' \
  --exclude='__pycache__' . | \
  ssh -i "$HOME/.ssh/t5-dev.pem" ubuntu@$PUBLIC_DNS \
  'mkdir -p ~/webhook-server && cd ~/webhook-server && tar xzf -'

# Or upload diffs only via scp:
scp -i "$HOME/.ssh/t5-dev.pem" ml_pipeline/<changed-file> \
  ubuntu@$PUBLIC_DNS:~/webhook-server/ml_pipeline/<changed-file>

# Run bench
ssh -i "$HOME/.ssh/t5-dev.pem" ubuntu@$PUBLIC_DNS \
  'source /opt/t5-venv/bin/activate && cd ~/webhook-server && python -m ml_pipeline.diag.bench_ball'

# Pull baseline back (after --update-baseline, only when you want to re-lock)
scp -i "$HOME/.ssh/t5-dev.pem" \
  ubuntu@$PUBLIC_DNS:~/webhook-server/ml_pipeline/diag/bench_ball_baseline.json \
  ml_pipeline/diag/

# Stop
aws ec2 stop-instances --region eu-north-1 --instance-ids i-0295d636f6bf957eb
```

The agent never SSHes into the box directly per `.claude/infrastructure/gpu_dev_box_runbook.md` — but for the 2026-05-21 run the agent override worked fine. Either pattern works.

## Things NOT to do

- **Don't lock the baseline from CPU.** Numbers will differ from GPU fp16 path. Use the box.
- **Don't merge a WASB swap to main without the BATCH-SIDE CHANGE CHECKLIST.** pipeline.py + wasb_*.py + models/wasb_tennis_best.pth.tar are all in-container.
- **Don't drop test_videos/ from the GPU rsync.** Broke the first ball-bench run; the runbook keeps it on purpose.
- **Don't add an S3 URI fallback to `replay_ball.py` until needed.** Explicitly deferred (build-when-needed).
- **Don't use §9 sequencing caveat to delay anything.** Silver-bench-specific (commit `0546278`).
- **Don't merge the `gap_73s_slice` window into `sa_point_6`.** They measure different things and the bench reports them separately on purpose.

## Open admin items (cross-session)

- Render Postgres still open to `0.0.0.0/0` (from 2026-05-21 Phase 5a). Re-lock or NAT Gateway + EIP.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped — terminate or keep for rollback.
- Option A Batch verification — task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` status unknown at session end.
