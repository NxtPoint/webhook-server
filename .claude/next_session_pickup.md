# Next-session pickup — 2026-07-26 (pm) — silver recon bench + serve-gap anchor shipped

> **Two parallel threads.** This is the **SportAI (`tennis_singles`)
> business-analytics pipeline**. The **T5 ML pipeline** is parked at "bronze DEV
> complete, training is the incremental remainder" (`.claude/handover_t5.md`).

## ⚡ Executive summary (read first)

Built the **two-match silver reconciliation bench** and shipped the first sound
silver lever. All on `main`, pushed.

> **★ RULES OF THE GAME + STEP-2 RECONCILIATION DONE (Tomo, 2026-07-27).** The whole
> business runs off ONE filter (spine = `exclude_d IS NOT TRUE`). Canonical statement
> + the full step-2 walk are at the TOP of
> `docs/_investigation/silver_gold_filter_contract.md` (§"★ THE RULES OF THE GAME" +
> §"★ Step-2 reconciliation pass"). **Every dashboard page verified silver→gold AND
> page-to-page on both matches — everything reconciles.** Fixes shipped: serve-points
> alignment (`f560b6f`/`c0870a4` — closed a real Player-Perf-vs-Match-Analytics
> 62.2%-vs-65.7% break), tally-back buckets (`6561eda`), AI-Coach fed the buckets
> (`d392928`), heatmap error toggle + info panels (`ab71508`/`c273f58`). Verified
> invariants: winners+errors=points; 1st+2nd serve-pts=service-points (DF⊆2nd);
> every breakdown tallies to its topline. **Prod silver rebuilt** (all matches, all 3
> silver fixes). **Identity UPDATE ran** (Erin=466). Remaining: finer placement/depth
> heatmap detail is still PARKED; new-GT axes (depth/serve placement) need Tomo's
> video annotation to score accuracy (vs the current internal-consistency proof).

- **STEP 1 (GT signed off):** Tomo's video ground truth for **df594aea** (Erin v
  Yolanda, 100 pts) + the **c8b77210** 18/18 anchor are persisted in
  `ml_pipeline/ground_truth/recon_*.json`. Keyed on **`ball_hit_s`** (stable across
  silver rebuilds — the serial `id` churns). 466=Erin, 772=Yolanda; pt12=466.
- **STEP 2 (bench + baseline):** `ml_pipeline/diag/recon_bench.py` scores boundaries
  (exact/merges/splits/dropped) + winners for both matches; `--db prod|devenv`;
  `recon_baseline.json` is the locked both-match anti-overfit gate. devenv==prod.
- **STEP 3 (lever shipped, commit `aee78bc`):** **serve-gap point anchor** —
  `SILVER_SERVE_GAP_ANCHOR` (default ON, rollback `=0`). A new point now also
  anchors when consecutive serves are **>30s** apart. Fixed all **6 merges** on
  df594aea (exact-1:1 **86→98**, winners **63→72**); **c8b77210 held 18/18**; clean
  matches byte-identical. **The plan's assumption that merges were a bronze
  missed-serve ceiling was refuted** — the serves were detected; it was a silver
  point-anchor gap (a missing serve breaks deuce/ad alternation → same-side glue).

## ⚠️ ACTION REQUIRED (Tomo) — fix df594aea player identity in gold

Gold has **Erin/Yolanda SWAPPED**. `gold.vw_player` maps names→track-ids from
`submission_context.first_server`; the upload marked `first_server=Erin` but
**Yolanda served first**, so gold pinned Erin=772 (should be Erin=466). Every
dashboard shows the two players swapped. Fix (write on Render DB — psql/dashboard,
`/ops/diag/sql` is read-only):
```sql
UPDATE bronze.submission_context SET first_server='player_b'
WHERE task_id='df594aea-78ef-47b1-8c10-60174a58d8b0';
```
`vw_player` is a live view → corrects ALL dashboards instantly, no rebuild (silver's
serve detection is geometric, doesn't use this field). Truth: **Erin=466,
Yolanda=772** (the recon GT was right). **Design fragility:** identity rests on that
one user-entered field — a wrong tick swaps everyone. Durable fix = a post-upload
"confirm which player is which id" step (future feature), or anchor identity on
something sturdier than `first_server`.

## Bugs fixed + gold/dashboard phase (2026-07-26 pm, cont.)

**Two silver bugs fixed** (both default-ON, env rollback, both-match gate green, serve bench green):
- `3b0eed1` **deuce/ad + serve-location use the FIXED centre 5.485**, not drifting
  `AVG(x)` (`SILVER_SERVE_SIDE_FIXED_MIDLINE`) — audit P1 closed. Neutral on point
  structure (no serve fell in either match's drift band), corrects serve placement.
- `26fff51` **serve false-positive guard**: demote a serve struck <3s after a
  DIFFERENT-player serve — it's a return (`SILVER_SERVE_FP_GUARD`, `SERVE_FP_GAP_S`=3).
  df594aea split **1→0**, exact **98→99**, winners **72→73**, silver points now
  **== 100 GT points exactly**. 0336b82b (same footage) −2 (its 2 return-FP splits).

**Gold reconciliation (tf_readonly now has gold SELECT):** all 10 gold views respect
the spine (`exclude_d IS NOT TRUE`). Row-level views (`vw_point`, `shot_placement`)
match the spine EXACTLY on both matches; `match_kpi` aggregates tie out (serves
72+62=134, points 101, service pts 51+50=101). The ONE defect was the identity swap
(above), NOT a filter bug — the "one filter through all dashboards" discipline holds.

**Ring-fence (spine vs GT in-play, df594aea):** **87% purity** (52 between-point
ghosts leak into gold) / **91% recall** (32 over-excluded). The bronze ghost ceiling
(same trailing-shot problem); clean on well-tracked matches.

**Serve-placement dashboard fixes** (`frontend/match_analysis.html`):
- `7ff1ab3` **faults hidden by default** + grey **"Faults: Hide/Show"** toggle (were
  plotted scattered AND mis-counted as wins when the point was won).
- `2ef0624` **service lines were drawn ~0.9m too shallow** (`cy(COURT_L-SVC_LINE)`=17.37
  vs true `NET_Y+SVC_LINE`=18.285 — the 6.40/17.37→5.485/18.285 bug the audit fixed in
  silver, still live in the FRONTEND court draw). FIXED all 3 service lines. Plus
  **SOFT-CLAMP** (exp saturation) folds out-of-box bounces into a band just inside the
  line — no pile-up; serve tab→service box, return/rally→court, player-position tabs
  unclamped. Residual out-of-box = bronze near-serve y-precision. **Root cause of "so
  many serves outside the box": ~14/48 were the mis-drawn line, ~13 bronze.**

## ⚠️ Follow-ups (not blocking)

1. **Prod silver rebuild** — the fix only changes silver on **re-ingest /
   `rerun-silver`**. Existing prod silver (df594aea + all matches) is unchanged
   until rebuilt on Render, so dashboards won't reflect it yet. Rebuild df594aea
   when convenient. Recon bench `--db prod` shows the OLD numbers until then; the
   gate uses **devenv** (already lever-on).
2. **CI** — `build_silver_v2.py` is a bench.yml trigger; the serve bench was
   validated green locally (unchanged: ea1e500c 12/26, 880dff02 23/24). Eyeball the
   GitHub Actions run to confirm (no `gh` on this box).

## STEP 4 — outcome layer measured; true-last-shot lever prototyped + REJECTED (bronze-limited)

Bench now scores **DF / ace / net-error** vs the owner's BK annotations (commit
`bae91df`). df594aea: DF 3/8 (+2 FP), ace 0/2 (+9 FP), net-err 2/10; c8b77210 perfect
(DF 1/1, ace 1/1, 0 FP → gates precision). **Outcome layer is at the BRONZE CEILING
for badly-tracked matches** — proven, not assumed:
- **Aces** = untracked returns (bronze); `bounce_plausible_d` doesn't separate 9 FP
  from 2 real. **Bounce-past-net** = wrong coordinate (bronze).
- **true-last-shot lever** (would fix winner+net-err+DF together): 29 points corrupted
  by a between-point "ghost" last shot. **Perfect remover fixes 29/29, breaks 0**
  (architecture is right) — but **NO signal separates ghosts from real last shots**
  (`dbg_discarded` 0/29, `is_in_rally` collapsed, gaps overlap). Best heuristic fixes
  6/29 while breaking 3 → **rejected, nothing shipped**.
- **Unlocks (all upstream):** is_in_rally recovery / bounce recall / **`ball_impact_type`
  populated** (reserved now — WATCH after each SportAI version bump). Re-run the bench then.

## Where the accuracy stands + remaining SILVER-side candidates (not the outcome axis)

`docs/_investigation/silver_recon_bench_plan.md` §OUTCOME + §STEP 4 have the full triage.
- **df594aea after the serve-gap fix:** boundaries exact **98/100** (merges 0, splits 1,
  dropped 1); **winners 72/98 = 73.5%**; the residual is bronze (above).
- **NOT a blanket "silver is done"** — still-unexamined silver items for a future session:
  - **1 split (true 44):** a serve false-positive (a return flagged `serve_d`) — a sound
    guard (two serves 1.07s apart, different players, can't both be 1st serves) is
    possible; edges into serve-derivation. NOT attempted.
  - **deuce/ad midline P1** (`build_silver_v2:653`) — splits on drifting AVG vs fixed 5.485.
  - **placement zones / depth / aggression / stroke-type** — never reconciled vs GT;
    headroom unknown, not zero.
  - **1 dropped (true 9):** all shots `exclude_d` — exclusion-relax territory.

## How to run the recon bench

```bash
.venv/Scripts/python -m ml_pipeline.diag.recon_bench            # score both vs prod
.venv/Scripts/python -m ml_pipeline.diag.recon_bench --diff     # + per-point diffs
.venv/Scripts/python -m ml_pipeline.diag.recon_bench --db devenv        # a local rebuild
.venv/Scripts/python -m ml_pipeline.diag.recon_bench --db devenv --update-baseline
```
Verification loop for any silver-derivation change: seed devenv → rebuild silver →
`recon_bench --db devenv` must show c8b77210 unchanged (18/18) AND df594aea
neutral-or-better → `bench` (serve) green → ship with env rollback. **devenv (port
55433) already has all 5 matches seeded + lever-on silver built.**

## Known-broken (pre-existing, not mine)

- **bench_silver `1d6feb3a` is RED** — baseline expects 7 rows, builder makes 101
  (89 serves). Stale T5 fixture drift, unrelated to the anchor change (proven inert
  with the flag on/off). Someone should re-snapshot or investigate the T5 builder
  on that fixture; don't blindly `--update-baseline` (could mask a real regression).

## Parked (unchanged from the am session)

- **VIDEO TRIM — SOLVED 2026-07-27. Now an AWS Batch (Fargate) per-use job.**
  `df594aea`: **8.6 min wall, 2.19x realtime, ~$0.14**, `trim_status=completed`,
  1136.3s reel from a 74-min/8.0 GB source (55 min of dead time cut). The same
  match on the Render worker ran **121 min and never finished**. Full path proven
  in prod: main API -> Batch submit -> Fargate encode -> S3 -> callback -> DB.
  - **Module `video_pipeline/fargate_trim/` — its README is the runbook.**
    `TRIM_BACKEND=batch` (default); `http` rolls back to the Render worker;
    `VIDEO_TRIM_ENABLED=0` disables trims entirely.
  - **`ffmpeg_trim_worker.py` is baked into the ECR image** — changing it needs a
    Docker rebuild + ECR push. A Render deploy alone will NOT update it.
  - Four bugs fixed getting here (all shipped): whole-source decode -> per-segment
    seek inputs (3.4x less decode); two sweeps killing healthy long trims at 30 min
    + re-fires stacking ffmpeg until OOM (worker lock + 2h windows); a
    `max(60, ...)` floor that made `TRIM_ENCODE_TIMEOUT_S` bound nothing; and
    **every ace being silently cut from every reel** (single ball-hit -> span 0.00s
    -> dropped by `MIN_POINT_DURATION_S`; 9 of 100 points on df594aea, all
    serve-only). Checks: `video_pipeline/tests/test_trim_{cmd,lock}.py`,
    `test_timeline_aces.py`, `e2e_trim_docker.py` (last two need pandas/ffmpeg ->
    run in the container, see their docstrings).
  - **OPEN / TODO:**
    1. **Suspend the `nextpoint-video-worker` Render service** — redundant, $25/mo.
    2. **Re-trim df594aea once more** — the completed run predates the ace fix, so
       it has 91 clips; a re-trim yields the full **100 including 7 aces**.
    3. **`299013b3` is an orphan** stuck at `accepted` (re-fired 17:55, killed by a
       deploy) — one `/ops/retrim` clears it.
    4. Confirm the reel actually PLAYS in the Locker Room (nobody has watched it).

- **quality_tier calibration:** both c8b77210 (good) and df594aea (bad) read
  `medium` — thresholds don't discriminate; df594aea should read `low`.

## Reference matches

| task | who | note |
|---|---|---|
| `c8b77210-542c-4ef9-b026-9d800c932817` | Tomo v Jimbo | **18/18 anchor** — protect from orphan sweep |
| `df594aea-78ef-47b1-8c10-60174a58d8b0` | Erin v Yolanda | recon GT match; badly tracked (ball 0.29) |
| `0336b82b` | Erin v Yolanda (earlier run) | same footage, different SA run |

## Open audit P1s (unchanged — none touched)

deuce/ad midline · hollow-ingest billing · NULL-as-0% frontend · serve-strategy
double-count · soft-delete on `vw_player`. (See prior pickup / audit closeout.)

## Canonical docs

- Task plan + outcome → `docs/_investigation/silver_recon_bench_plan.md`
- Pipeline logic + filter contract → `docs/_investigation/silver_gold_filter_contract.md`
- Recon bench → `ml_pipeline/diag/recon_bench.py`; GT → `ml_pipeline/ground_truth/recon_*.json`
- Serve bench (mandatory pre-`build_silver_v2`/`serve_detector` push): `.venv/Scripts/python -m ml_pipeline.diag.bench`
