# Next-session pickup — 2026-07-26 (pm) — silver recon bench + serve-gap anchor shipped

> **Two parallel threads.** This is the **SportAI (`tennis_singles`)
> business-analytics pipeline**. The **T5 ML pipeline** is parked at "bronze DEV
> complete, training is the incremental remainder" (`.claude/handover_t5.md`).

## ⚡ Executive summary (read first)

Built the **two-match silver reconciliation bench** and shipped the first sound
silver lever. All on `main`, pushed.

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

- **df594aea video TRIM — REWRITTEN, awaiting prod validation.** (`trim_status` is
  `failed`, not `accepted` — the am note drifted; the error is
  `Command timed out after 3600s`.) `run_ffmpeg_trim` now uses **one `-ss/-t` seek
  input per kept segment + `concat`**, so ffmpeg decodes only the ~30% kept
  instead of the whole source — measured **3.4× less decode work** (= the
  theoretical ceiling at a 30% keep ratio) on a real ffmpeg in Docker.
  Two constraints found while doing it, both now designed around: the source is
  **8.0 GB** (so download-once CANNOT be the default — it doesn't fit the 2 GB
  `/tmp`) and the worker is a **512 MB starter** instance (so ~86 simultaneous
  seek inputs risk OOM → passes of `TRIM_SEEK_INPUTS_PER_PASS`=12 joined with the
  concat demuxer, `-c copy`). Measured shape: **86 segments, 23.8 min kept of
  73.9 min (32%)**.
  **⚠ BUT the trim will still not fit the budget — ENCODE is now the binding
  constraint, and it needs a Tomo decision.** Measured on a 0.5 CPU / 512 MB box
  (= Render starter, what the video-worker runs on) against 1080p 29.97 footage
  matching the real source, `df594aea` keeps 1425 s:

  | output | preset | rate | df594aea encode | file size | verdict |
  |---|---|---|---|---|---|
  | 1080p | veryfast (current) | 0.17–0.23× | **104–141 min** | 17 MB/min | OVER |
  | 1080p | ultrafast | 0.44–0.51× | 46–54 min | 75 MB/min | tight, huge file |
  | 720p | veryfast | 0.34× | 69 min | 5 MB/min | OVER |
  | 720p | ultrafast | 0.49× | **49 min** | 28 MB/min | only sub-budget option |
  | 540p | veryfast | 0.42× | 56 min | 3 MB/min | OVER |

  **A 0.5 CPU instance cannot re-encode ~24 min of 1080p inside the hour at any
  preset.** Downscaling helps less than the pixel ratio suggests because the
  scale filter itself costs CPU. Options, in preference order:
  1. **Upgrade the video-worker Render plan** (starter 0.5 CPU → standard/pro).
     Roughly linear: 2 CPU puts 1080p veryfast at ~35–50 min, 720p at ~17–25 min.
     Costs money; keeps reel quality. **Recommended — this is a compute problem.**
  2. `TRIM_MAX_HEIGHT=720` + `VIDEO_PRESET=ultrafast` (both no-redeploy env
     flips): fits today at ~49 min, but ultrafast inflates the file ~5× (28 vs
     5 MB/min) so the reel gets big to store and stream.
  3. Raise `TRIM_ENCODE_TIMEOUT_S` — **explicitly rejected**, an hour-plus trim
     is the thing being fixed.
  (dev-box 0.5 CPU ≠ Render 0.5 CPU exactly — treat as ratios, verify on the
  first real run. Also confirm the worker's LIVE plan in the dashboard; the
  committed `plan: starter` may not match.)

  **TO DO — validate in prod** once the worker redeploys: re-fire
  `POST /ops/retrim {"task_id":"df594aea-78ef-47b1-8c10-60174a58d8b0"}` (header
  `X-Ops-Key`, main-API Render shell). Success = accepted→completed,
  `trim_output_s3_key=trimmed/df594aea-…/review.mp4`, `trim_duration_s` ≈ 1425 s.
  Watch video-worker Logs for the per-pass lines (`pass N/22 done … keep …s in
  …s`) — the first pass gives the real encode rate, so you can extrapolate
  immediately instead of waiting an hour for a timeout.
  Checks: `python -m video_pipeline.tests.test_trim_cmd` +
  `video_pipeline/tests/e2e_trim_docker.py` (real ffmpeg in Docker).
- **`video-worker.onrender.com` is a FOREIGN app** — still true, never
  external-health-check it (use the service's Render Logs). **Fixed:**
  `VIDEO_WORKER_BASE_URL` is now `sync: false` in `render.yaml` (main API +
  ingest worker) instead of carrying the squatted value, so a blueprint sync
  can't clobber the working dashboard URL. Tomo: confirm both services still
  hold the real URL after the next sync.
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
