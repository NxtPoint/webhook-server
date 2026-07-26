# Silver reconciliation bench — two-match accuracy harness (task plan)

**Status:** ✅ STEPS 1-3 DONE (2026-07-26). Bench built + baseline locked + first
sound lever shipped default-ON. **Owner context:** Tomo hand-mapped the full Erin v
Jolanda match on video.

## ✅ OUTCOME (2026-07-26)

**STEP 1 — GT signed off.** Extracted from the (fixed) Excel and persisted in-repo,
keyed on `ball_hit_s` (STABLE across silver rebuilds — the serial `id` churns and
broke the join). Tomo signed off: 100 true points, 466=Erin/772=Yolanda, pt12=466.
- `ml_pipeline/ground_truth/recon_df594aea.json` (`signed_off: true`)
- `ml_pipeline/ground_truth/recon_c8b77210.json` (the 18/18 anchor, winners from prod)

**STEP 2 — bench built + baseline locked.** `ml_pipeline/diag/recon_bench.py` —
scores point BOUNDARIES (exact-1:1 / merges / splits / **dropped** — a true point
whose every shot silver excluded) and point WINNERS (id-based) for both matches.
Swappable DB source (`--db prod` tf_readonly / `--db devenv`). devenv==prod parity
verified. `recon_baseline.json` is the locked both-match anti-overfit gate.

**STEP 3 — first sound lever SHIPPED (serve-gap point anchor).** The first-look
called the 6 merges "mostly a bronze missed-serve ceiling." **Measurement refuted
that:** all 6 boundaries HAD a detected serve — silver just didn't start a new point
because point-anchoring keyed only on serve SIDE/SERVER change, and a single missing
serve breaks deuce/ad alternation → two same-side serves glue (the 2nd mislabelled
"2nd serve" 47-71s later). Fix: also anchor on a >30s consecutive-serve gap
(`SILVER_SERVE_GAP_ANCHOR`, default ON, commit `aee78bc`). Result: df594aea merges
**6→0**, exact-1:1 **86→98**, winners **63→72**; c8b77210 held **18/18**; clean
matches byte-identical; serve bench green.

**Still open (documented, NOT chased — realistic bronze-ceiling residue):**
- **Winner accuracy ~73%** (72/98). Triaged the 26 disagreements: ~10 "same ending"
  (silver has the true last shot but bounce coord is ~0.1m past the net → wrong
  outcome = **bronze bounce accuracy**, the filter-contract doc forbids tightening),
  ~3 "stopped early" (tracking ends 10-21s before the true last shot = **bronze**),
  ~13 "trailing extra" (silver kept a between-point shot 2-8s after the true end).
  The trailing-extra subset is the only remaining *possible* sound silver lever
  (tighter trailing-shot exclusion) — delicate, must not re-merge; NOT attempted.
- **1 split** (true 44 → 2 silver points): a serve FALSE-POSITIVE (466's return
  flagged `serve_d`). That's the geometric serve derivation, not point structure;
  a sound guard (two serves 1.07s apart by different players can't both be 1st
  serves) is possible but edges into serve-detector territory — NOT attempted.
- **1 dropped point** (true 9): all its shots `exclude_d`. Exclusion-relax territory.
- **Prod rebuild needed:** existing prod silver is unchanged until re-ingest /
  `rerun-silver`. df594aea + other matches must be rebuilt on Render for the fix to
  reach dashboards (Tomo runs `/ops/*`).
- **bench_silver `1d6feb3a` is pre-existing RED** (baseline expects 7 rows, builder
  makes 101 — stale T5 fixture drift, unrelated; my change proven inert on it).

## ✅ STEP 4 (2026-07-26) — outcome layer measured + true-last-shot lever PROTOTYPED and rejected

Extended the recon bench to score **double-fault / ace / net-error** vs the owner's
BK annotations (commit `bae91df`). Baseline: c8b77210 DF 1/1, ace 1/1, 0 FP
(complete GT → gates precision); df594aea DF 3/8 (+2 FP), ace 0/2 (+9 FP),
net-err 2/10 (partial GT → gates recall, baselines FP count).

**Everything sorts into two piles, and we PROVED which is which (measure, not assume):**
- **Bronze ceiling — do NOT touch (SportAI will lift it):**
  - **Aces** (9 FP): all are a serve with *zero tracked return* → look unreturned.
    `bounce_plausible_d` doesn't separate the 9 fakes from the 2 real aces. Untracked
    returns = bronze.
  - **Bounce-past-net** (~2 net errors, several winners): rule correct, coordinate wrong.
- **The one silver-addressable axis = true-last-shot identification** (fixes winner +
  net-error + DF together). **Prototyped and REJECTED — bronze-limited:**
  - 29 points have a between-point "ghost" as their last shot (the corruptor).
  - Simulation: a **perfect** ghost-remover fixes **29/29, breaks 0** — so the
    *architecture* is right (given the true last shot, all outcomes fall out).
  - But **no available signal separates the 29 ghosts from the 71 correct last shots**:
    `dbg_discarded` 0/29 (SportAI itself keeps them), `dbg_conf_ball_hit` null,
    `is_in_rally` collapsed (match is 93% flagged warm-up), gap overlaps (ghost
    0.6–4.6s vs real 1.0–16.3s), NULL-bounce 69% vs 32%. Best heuristic
    (`ball_player_distance>1.0`) fixes only 6/29 while breaking 3 correct points.
  - Shipping any heuristic = "improve bad, compromise good" → **rejected. Nothing shipped.**

**What unlocks the residual 29 points (all upstream — re-run the bench after any of these):**
1. `is_in_rally` recovery on badly-tracked matches → "last in-rally shot = point end" becomes sound.
2. Better floor-bounce recall → "no bounce after this shot = point over" works.
3. **`ball_impact_type` populated** (currently reserved/`None`) — would hand us net/floor/out
   labels directly. **WATCH-ITEM:** check it after every SportAI version bump.

**Are we at the silver ceiling? Precisely:** on the axes we measured — point
BOUNDARIES, WINNERS, DF, ACE, NET-ERRORS — **yes for badly-tracked matches** (silver
logic is proven *correct* on c8b77210: 18/18, DF/ace 1/1, 0 FP), and the residual is
bronze. It is **NOT** a blanket "silver is done": still un-examined silver-side items —
(a) the **1 split** (serve false-positive guard), (b) the **deuce/ad midline P1**
(`build_silver_v2:653` splits on a drifting AVG vs fixed 5.485), (c) placement
**zones / depth / aggression / stroke-type** columns never reconciled against GT
(headroom unknown, not zero). Those are separate axes for a future session.

---

## Original plan (below, for reference)

## Goal

Build a **two-match reconciliation bench** that scores `silver.point_detail`
against hand-verified video ground truth for BOTH:
- `c8b77210` (Tomo v Jimbo) — the **18/18** clean match (the anti-overfit anchor).
- `df594aea` (Erin v Jolanda) — a full match, badly-tracked camera, ~100 points.

Then iterate silver logic to get df594aea as accurate as possible **without moving
c8b77210 off 18/18**. Lock baselines so we can't regress.

## Iron rules (the owner's integrity line — do not cross)

1. **No manufacturing answers.** Only ship a silver change that is derivable from
   sound tennis/geometry logic. Never tune to one match's quirks.
2. **The ceiling is bronze/camera — silver must not try to beat it.** Missing data
   (player/ball out of frame, white patches in ball x/y, missed far serves, line
   calls the kids overrode) is a bronze-accuracy ceiling (RULE 6 / bronze-first).
   Silver cannot invent it. Expect df594aea to top out well below 100%.
3. **Both-match constraint = anti-overfit.** A change must be neutral-or-better on
   c8b77210 (stays 18/18) AND df594aea. Helps-bad-but-hurts-good ⇒ reject.
4. Validate in `devenv` (both matches seeded); keep `bench` green; never ship
   derived logic on a hunch (several were refuted by measurement in prior sprints).

## Known bronze/camera limitations on df594aea (the ceiling — NOT silver bugs)

Owner-reported from the video (his camera setup, acknowledged imperfect):
- Trams cut off; not enough space behind the baseline.
- Players running back/wide fall **out of frame** → tracking stops → a point can
  look like it ended early. **This is the prime candidate for a SOUND silver fix**
  (gap-bridging) — see levers below.
- Ball hit high above the frame → data stops mid-rally.
- `ball_bounce` x/y imperfect; visible white/no-data patches.
- Some **far serves (Erin)** not detected → those points missing (~5 points
  dropped total, per owner). Bronze serve-detector gap, not silver.
- Line calls: a few out-balls played on, a few in-balls called out (no cheating) —
  genuinely ambiguous; don't try to encode.

Owner estimate: ~80% already correct, ~100 points (silver has **95**). "Materially
right." The job is to close the *sound-logic* portion of the gap, not all of it.

## Ground truth — CLEAN, ready to use (Tomo finalised 2026-07-26)

The GT lives in `C:\Users\tomos\OneDrive\Documentos\Tenfifty5\erin_v_yolanda recon.xlsx`
(Sheet1, 611 rows = the silver shot export). Players: **Erin = low id (~427),
Yolanda ~770.** Tomo added **column A = `true point` (1–100)** — one value per
shot — and **column B = `time`**. Columns then shift +2: **`id` (silver
point_detail id) = column C**, `player_id` = column E.

**Extraction is now trivial and unambiguous** (no colour parsing): join
`col A (true point)` ↔ `col C (silver id)`. 390 shots carry a true point (the
spine; excluded shots are blank), covering **100 true points**. Verified end-to-end:
all 100 true points join to silver, 0 uncovered.

**Step 1 remaining:** (a) persist this GT in-repo
(`ml_pipeline/ground_truth/recon_df594aea.json` = `{silver_id: true_point}`) plus a
c8b77210 GT (the 18/18 winners). (b) **Ask Tomo to add a true WINNER per point**
(Erin/Yolanda) — the recon above scores point *boundaries*; winner accuracy (the
18/18-style metric) needs his winner per point, or validate winners on the 86 clean
points against the video.

## First-look reconciliation baseline (2026-07-26, read-only)

Joining the clean GT to prod silver:

- **86 / 100 points are exactly 1:1** (silver point_number == true point). Better
  than the owner's ~80% estimate.
- **0 true points with no silver coverage.**
- **6 MERGES** (silver g(l)ued consecutive real points → missed a boundary):
  silver pt `8`=true[8,9,10] · `20`=[22,23] · `35`=[38,39] · `55`=[58,59] ·
  `58`=[62,63] · `68`=[73,74].
- **1 SPLIT**: silver broke true point `44` into two.

The gap is dominated by **missed point boundaries (merges)** — a new point started
(serve) but silver didn't break. That is mostly a **bronze/serve-detection ceiling**
(RULE 6: fix bronze, don't paper over in silver); investigate each merge as
missed-serve (bronze) vs contiguity-threshold (silver). The single split (44) is
the one candidate for a silver over-break. **This is the target list for the bench.**

## Bench design

Extend the existing `bench_silver` family (local Docker Postgres, `fixtures_silver`,
`diag/bench_silver*`). New: a **reconciliation scorer** that, per match:
- Aligns silver points ↔ GT points (by time/serve, tolerant to ±1 boundary).
- Scores: point count, **point-winner accuracy**, serve side/try where GT has it.
- Emits a per-point diff (silver vs GT) so a miss is triageable as
  **structural-exclude / detector-fix / train** vs a **silver bug** (RULE 6 triage).
- Locks baselines in a `*_baseline.json` (c8b77210 = 18/18; df594aea = the
  measured starting accuracy). CI/`bench` stays the serve-detector gate; this is a
  local reconciliation gate run before any silver-derivation change.

## Candidate SOUND silver levers (driven by the first-look, TEST don't assume)

The measured error pattern is **6 merges + 1 split**, so the levers are:
1. **Triage each of the 6 merges** (silver pts 8/20/35/55/58/68) — was there a real
   serve/point-start silver didn't break on? Trace the boundary in bronze:
   - **Missed serve (bronze)** → the serve detector never fired (Erin far serves,
     player out of frame). This is the **bronze/serve ceiling — do NOT fix in
     silver** (RULE 6). Expected to be most of them; confirms the ~5 dropped points.
   - **Real >5s break present but silver didn't split** → a genuine silver
     contiguity bug worth fixing (but `SILVER_RALLY_CONTIGUITY` already breaks at
     the first >5s gap, so this would be a surprise — verify with the timestamps).
2. **The 1 split (true point 44)** — the clearest silver-side candidate: did silver
   over-break one real rally into two (e.g. a mid-rally tracking gap when a player
   left frame)? If so, a **sound gap-bridge** (resume-within-window, no serve
   between, ball in court envelope) may fix it — but it must not re-merge the 6.
3. **Point-winner accuracy** — once Tomo adds true winners, score the last-shot →
   winner logic on the 86 clean points (the 18/18-style metric).

Do NOT pursue: recovering missed far serves in silver (bronze detector job), or
encoding line-call overrides (genuinely ambiguous). Realistic expectation: much of
the merge gap is a bronze ceiling; the honest silver win is small (the split + any
true contiguity miss) — which is the correct, integrity-preserving outcome.

## Verification loop (per lever)

devenv rebuild → reconciliation bench on BOTH matches → require c8b77210 unchanged
(18/18) + df594aea improved (or neutral) → `bench` green → before/after per-point
diff reviewed → ship with an env rollback. Never autonomous-flip a derived change.

## Video

Source: `s3://nextpoint-prod-uploads/wix-uploads/1784867517_Erin_vs_Jolanda.mov`
(eu-north-1). Not needed for the bench (GT + silver suffice); only pull it for
human spot-checks. `01:13:40` long.
