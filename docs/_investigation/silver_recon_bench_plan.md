# Silver reconciliation bench — two-match accuracy harness (task plan)

**Status:** NOT STARTED (planned 2026-07-26). This is the next major SportAI job.
**Owner context:** Tomo hand-mapped the full Erin v Jolanda match on video.

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
