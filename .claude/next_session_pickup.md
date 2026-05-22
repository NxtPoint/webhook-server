# Next-session pickup — morning of 2026-05-23

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-23 (Tomo's morning — picking up from overnight Claude session that closed 2026-05-22 ~late night)
**Phase active:** Phase 5c — dual-submit corpus pipeline. **5c.0 / 5c.1 / 5c.2 / 5c.3 LIVE + VERIFIED in prod.** Tonight's overnight work shipped Phase 5c.4 GROUNDWORK on a branch.
**Bench:** Serve `a798eff0=20/24, 880dff02=23/24` — green on main as of `ec357f7`.
**What shipped overnight (3 commits):**
- `99bc379` docs: CLAUDE.md trim under 40k chars + polling-gate rule + Phase 5c.3 commands
- `ec357f7` ops: `cron_sweep_t5_orphans.py` (the Render cron script — wiring is the only manual step left)
- `82930a7` (on `feature/phase-5c-4-bench-gate` branch — NOT merged): Phase 5c.4 groundwork — `bench_finetuned.py`, `--weights-path` threading, promotion playbook
**What's blocked:** Two things, both your decisions:
  1. Wire the Render Cron Job for `cron_sweep_t5_orphans.py` (UI-only, ~5 min).
  2. Review + merge the `feature/phase-5c-4-bench-gate` branch (no Batch deploy in this PR — just diag harness + docs; safe to merge anytime).
**Next session's first job:** Do (1) above so future auto-spawned T5 tasks unblock themselves. Then merge (2) or leave parked.

If the above is enough, stop reading. The rest of this file has details + verification commands.

---

## Item 1 — Wire the Render cron (the only thing tonight didn't fully close)

The script `cron_sweep_t5_orphans.py` is on `origin/main` (commit `ec357f7`). It POSTs to `/ops/sweep-t5-orphans` with `{"dry_run": false}` and exits non-zero on HTTP error. Pattern follows `cron_monthly_refill.py`.

**Render dashboard steps (~5 min):**

1. Render dashboard → **+ New** → **Cron Job**
2. Repository: same GitHub repo as the existing crons (`NxtPoint/webhook-server`)
3. Branch: `main`
4. Build command: `pip install --upgrade pip && pip install -r requirements.txt`
   (matches the pattern of the other crons; the script itself only uses stdlib + nothing in requirements.txt, but Render needs a non-empty build for the Python service to provision)
5. Command: `python cron_sweep_t5_orphans.py`
6. Schedule: `*/5 * * * *` (every 5 min)
7. Env vars:
   - `OPS_KEY` — copy from "Sport AI - API call" service env. **Same value** as that service uses.
   - (optional) `SWEEP_T5_ORPHANS_LIMIT` — override the server-side default of 50 max tasks per sweep
8. Create.

**Pre-creation sanity check** (do this BEFORE creating the cron, while you have a shell open):
```bash
# From any shell with OPS_KEY in env:
curl -sS -X POST https://api.nextpointtennis.com/ops/sweep-t5-orphans \
     -H "X-Ops-Key: $OPS_KEY" \
     -H "Content-Type: application/json" \
     -d '{"dry_run": true}'
```
Expected response shape:
```json
{"ok": true, "dry_run": true, "found": 0, "triggered": [], "sample": []}
```
If `found > 0` in the sample, there are stuck T5 tasks waiting — those will get picked up on the first scheduled cron tick after wiring. (You can hit `dry_run=false` manually now if you want to clean up immediately.)

**Post-creation verification:**
1. Render → the new cron's logs after the first scheduled tick.
2. Expect a one-line JSON response, `"ok": true`.
3. No `"triggered"` entries is normal (means no orphans existed at that tick).

That's the full Phase 5c.3 → 5c.3-prime closure. After this, no auto-spawned T5 task can sit in `queued` indefinitely.

---

## Item 2 — Review the Phase 5c.4 branch (your call: merge or leave parked)

Branch: `feature/phase-5c-4-bench-gate` (pushed to origin tonight, commit `82930a7`).

**What's in it:**

1. **Thread `weights_path` through `bench_ball` harness** — both `BallTracker` and `WASBBallTracker` already accept the kwarg in their constructors; the bench wrapper just didn't expose it. Now `bench_ball.py --weights-path <candidate.pt>` lets you bench a candidate finetune against fixtures without rebuilding the Docker image.
   - Guard: `--weights-path` + `--update-baseline` is hard-rejected — the committed baseline must reflect production weights.
2. **NEW `ml_pipeline/diag/bench_finetuned.py`** — runs the bench TWICE per (fixture, tracker) pair (production weights vs candidate) and emits a `PROMOTE` / `NEUTRAL` / `REJECT` verdict. Exit code follows verdict; safe to wire into a future CI gate.
3. **NEW `.claude/playbook_phase_5c4_weights_promotion.md`** — deploy playbook for AFTER a PROMOTE verdict. Documents the env-gated `TRACKNET_WEIGHTS` override pattern (matches the WASB swap from 2026-05-21), the Docker rebuild + dual-region ECR push + job-def rev sequence, and the rollback path (unset env var, no rebuild needed).

**Why on a branch and not main:**

- The harness/diag changes are safe to land any time — they don't touch detector code.
- The playbook references config.py and ball_tracker.py env-gating that ISN'T shipped yet. That's intentional — the playbook is forward-looking. Merging the playbook to main is fine; landing the env-gate code change is a separate decision that trips guardrail #8 (Docker rebuild required) and shouldn't happen overnight.

**Pre-flight before merging:**

```bash
# 1. Verify the branch
git fetch origin
git log --oneline origin/feature/phase-5c-4-bench-gate -5
git diff main..origin/feature/phase-5c-4-bench-gate --stat

# 2. Run the locked benches against the branch (should be green — no detector code touched)
git checkout feature/phase-5c-4-bench-gate
.venv/Scripts/python -m ml_pipeline.diag.bench
# Expect: a798eff0=20/24, 880dff02=23/24 [OK] No regressions

# 3. Smoke-test the new harness path (won't have weights to compare yet, but
#    confirms the CLI is wired correctly)
.venv/Scripts/python -m ml_pipeline.diag.bench_finetuned --help
.venv/Scripts/python -m ml_pipeline.diag.bench_finetuned --weights-path /tmp/nope.pt
# Expect: "ERROR: candidate weights file not found" + exit code 2

# 4. If everything looks good, merge
git checkout main
git merge --ff-only origin/feature/phase-5c-4-bench-gate
git push origin main
```

The Render deploy on this merge is harmless — diag tools aren't loaded by the running services.

---

## State at session end (2026-05-22 night → 2026-05-23 early hours)

`origin/main` at **`ec357f7` cron: cron_sweep_t5_orphans.py — Phase 5c.3 closure**. Recent commits:

```
ec357f7 cron: add cron_sweep_t5_orphans.py — Phase 5c.3 closure
99bc379 docs: trim CLAUDE.md under 40k chars + polling-gate rule + Phase 5c.3 harness commands
065bbcf session close 2026-05-22 night — Phase 5c END-TO-END VERIFIED
a1a7e96 ops: /ops/sweep-t5-orphans — fire ingest for stuck auto-spawned T5 tasks
b48230c harness: build-corpus --task <t5_task_id> filter
```

`origin/feature/phase-5c-4-bench-gate` at **`82930a7` phase 5c.4 groundwork: bench_finetuned + --weights-path threading**.

**Phase 5c artefacts in prod (unchanged from last session):**
- `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` on Render
- `ml_analysis.training_corpus` has 1 row (`78c32f53-...`, label_kind='ball_position', 161 labels)
- S3: `s3://nextpoint-prod-uploads/training/labels/78c32f53-5580-4a88-a4e7-7506e59b2b52_ball_positions.json`

**Serve bench (verified `ec357f7` 2026-05-22 night):** `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Batch state (unchanged):** eu-north-1 `:48`, us-east-1 `:30` — amd64 `bc8f7d72…`

---

## Read in this order before doing anything else

1. **This file** — you're already here.
2. `docs/north_star.md` §"Phase 5" — confirm phase status hasn't shifted.
3. `CLAUDE.md` §"Things not to do" — note new rule #10 about auto-spawn polling-gate pairing.
4. `.claude/playbook_phase_5c4_weights_promotion.md` IF you're about to attempt a finetune deploy (not relevant for Item 1 above; only matters if Phase 5c.4 work resumes).

Then run the locked bench to confirm the floor:

```bash
.venv/Scripts/python -m ml_pipeline.diag.bench
```

Expect: serve `a798eff0` 20/24, `880dff02` 23/24.

---

## Next move — pick one (recommended order: 1 → 2 → 3 → 4)

**Option 1: Wire the Render Cron Job for `cron_sweep_t5_orphans.py` (~5 min).** See Item 1 above. This is the last manual closure for Phase 5c.3. Strongly recommended as the first thing.

**Option 2: Review + merge `feature/phase-5c-4-bench-gate` (~10 min).** See Item 2 above. Safe; no Batch deploy implied by the merge. Lands the bench_finetuned harness + promotion playbook on main so future sessions can use them.

**Option 3: Re-capture `1d6feb3a` silver-bench fixture against post-fix Batch image (Tomo-side, ~15 min Render + 5 min local).** Still pending from last session — needs Render shell snapshot. If silver row count jumps 7 → 30+ that's direct evidence the chain-rejection fix is structurally repairing T5 bronze density.

**Option 4: Smoke-test `harness build-corpus` end-to-end (~15 min, requires 2+ corpus rows).** Still blocked on having only 1 corpus row in prod. Either wait for another organic upload, or manually trigger one to seed a second pair.

**Option 5: Phase 5c.4 actual training run.** With the harness shipped tonight (on the branch), the next concrete training step is collecting enough corpus rows (need ~5+ matches) and running `ml_pipeline/training/` to produce a candidate `.pt`. Once you have one, `bench_finetuned --weights-path <.pt>` is the gate.

---

## Open admin items (unchanged from last session except where noted)

- ~~`/ops/sweep-t5-orphans` shipped but NOT wired to a cron yet.~~ **Script shipped tonight (`ec357f7`); Render Cron Job entry still needs creation (Item 1 above).**
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS).
- Silver-bench has only 1 fixture (`1d6feb3a`). Adding `880dff02` would give a denser regression target.
- Edge: a brand-new auto-spawned T5 within the cron's `min_age_minutes=5` window will not be picked up until the next 5-min sweep tick. Acceptable for now (T5 takes >25 min on Batch; first sweep tick will always catch them in time).

---

## Things NOT to do (load-bearing, unchanged)

- **Don't merge `ball_tracker.py`, `wasb_ball_tracker.py`, `wasb_hrnet.py`, `config.py`, `pipeline.py`, `db_writer.py`, or `Dockerfile` changes without BATCH-SIDE CHANGE CHECKLIST.** (Tonight's harness commits don't touch these.)
- **Don't ship Phase 5c.4 actual config.py env-gate change overnight.** The branch tonight is deliberately groundwork-only — the env-gate code change trips guardrail #8 and needs daylight + bandwidth to deploy.
- **Don't rollback WASB without running the bench against TrackNetV2 first.**
- **Don't change `AUTO_DUAL_SUBMIT_T5` / `AUTO_LABEL_DUAL_SUBMIT_PAIRS` env-flag defaults to ON in code.**
- **Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD`.**
- **Don't auto-spawn a task without a paired server-side trigger** (new rule in CLAUDE.md "Things not to do" #10 — added tonight).
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables.

---

## Verification commands

```bash
# 1. Bench floor
.venv/Scripts/python -m ml_pipeline.diag.bench
# Expect: a798eff0=20/24, 880dff02=23/24

# 2. /ops/sweep-t5-orphans dry-run (needs OPS_KEY in env)
curl -sS -X POST https://api.nextpointtennis.com/ops/sweep-t5-orphans \
     -H "X-Ops-Key: $OPS_KEY" \
     -H "Content-Type: application/json" \
     -d '{"dry_run": true}'
# Expect: {"ok": true, "dry_run": true, "found": 0|N, ...}

# 3. Corpus row still intact (run against DATABASE_URL)
psql "$DATABASE_URL" -c "
SELECT t5_task_id::text, label_kind, label_count, role_breakdown, created_at
FROM ml_analysis.training_corpus
ORDER BY created_at DESC LIMIT 3;
"

# 4. Branch overview
git fetch origin
git log --oneline origin/feature/phase-5c-4-bench-gate -3
git diff main..origin/feature/phase-5c-4-bench-gate --stat
```

---

## Notes from the overnight session

- The overnight work was scoped specifically to avoid anything that requires Tomo to be awake for supervision: no Batch deploys, no Render env-var changes, no production data writes. Everything is either local diag harness OR a branch that requires explicit merge intent.
- The Phase 5c.4 branch was a clean "pickup option 4 from yesterday's pickup, modulo the deploy" — the pickup language said "Don't ship this without bandwidth", so the deploy parts of Option 4 are explicitly punted.
- CLAUDE.md was over the 40k-char warning threshold (41,879). Trimmed Support Bot section, /ops/diag/sql description, and Client API table to point to canonical docs — net 41,879 → 38,761. Three load-bearing additions in the same commit: rule #10 about auto-spawn polling gates, build-corpus harness commands, the new cron script reference.
