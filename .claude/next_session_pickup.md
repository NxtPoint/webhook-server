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

## ⚠️ Two follow-ups (not blocking)

1. **Prod silver rebuild** — the fix only changes silver on **re-ingest /
   `rerun-silver`**. Existing prod silver (df594aea + all matches) is unchanged
   until rebuilt on Render, so dashboards won't reflect it yet. Rebuild df594aea
   when convenient. Recon bench `--db prod` shows the OLD numbers until then; the
   gate uses **devenv** (already lever-on).
2. **CI** — `build_silver_v2.py` is a bench.yml trigger; the serve bench was
   validated green locally (unchanged: ea1e500c 12/26, 880dff02 23/24). Eyeball the
   GitHub Actions run to confirm (no `gh` on this box).

## Where the accuracy stands + what's left (all documented in the plan doc)

`docs/_investigation/silver_recon_bench_plan.md` §OUTCOME has the full triage.
- **df594aea after the fix:** boundaries exact **98/100** (merges 0, splits 1,
  dropped 1); **winners 72/98 = 73.5%**.
- The **26 winner disagreements are mostly a bronze ceiling** — ~10 bounce ~0.1m
  past the net (filter-contract forbids tightening the bounce test), ~3 tracking
  stopped early. Only ~13 "trailing-extra" (silver kept a between-point shot 2-8s
  past the true end) are a *possible* future sound exclusion lever — delicate, must
  not re-merge; NOT attempted this session.
- **1 split (true 44):** a serve false-positive (a return flagged `serve_d`) — a
  sound guard is possible but it's serve-derivation territory. NOT attempted.
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
  **TO DO — validate in prod** once the worker redeploys: re-fire
  `POST /ops/retrim {"task_id":"df594aea-78ef-47b1-8c10-60174a58d8b0"}` (header
  `X-Ops-Key`, main-API Render shell). Success = accepted→completed,
  `trim_output_s3_key=trimmed/df594aea-…/review.mp4`, `trim_duration_s` ≈ 1425 s,
  in **minutes not an hour**. Watch video-worker Logs for `pass N/8 done …`.
  If it still times out, the residual cost is *encode* on 0.5 CPU, not decode →
  lower `VIDEO_PRESET` / resolution; do NOT raise the timeout.
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
