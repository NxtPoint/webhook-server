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

## Ground truth — extraction + VERIFY FIRST (do this before any bench code)

The GT lives in `C:\Users\tomos\OneDrive\Documentos\Tenfifty5\erin_v_yolanda recon.xlsx`
(Sheet1, 611 rows = the silver shot export; `id` col = silver.point_detail id).
Players: **Erin = low id (~427), Yolanda = ~770.**

The point mapping is in **cell fill colours marking the START and END of each
point**, NOT whole rows (colours are non-uniform across columns — a naïve row-colour
read is wrong; a first pass gave 31 runs, not ~100). Colours: blue `FF00B0F0` +
yellow `FFFFFF00` = normal points (alternating; he switched to blue when yellow
would repeat), green (theme) = tiebreak, orange (theme) = **potentially problematic
(there may be more oranges)**.

**Step 1 (mandatory): extract the GT into a structured form and get Tomo to
sign it off** before building anything on it. Two options, pick with Tomo:
  (a) Ask Tomo for a small clean tab: `point_no | start_silver_id | end_silver_id |
      winner (Erin/Yolanda) | type (normal/tiebreak/problem)` — most robust, small
      effort, removes all colour-parsing ambiguity.
  (b) Parse the start/end colour markers programmatically, reconstruct the ~100
      points, render them back (point#, id range, winner, type) and have Tomo
      confirm — respects the work already done, but must be eyeball-verified.
Store the signed-off GT in-repo (e.g. `ml_pipeline/ground_truth/recon_df594aea.json`)
alongside a c8b77210 GT (the 18/18 winners we already validated).

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

## Candidate SOUND silver levers (hypotheses to TEST, not to assume)

Each must improve df594aea AND keep c8b77210 at 18/18, or it's rejected:
1. **Rally gap-bridging** — when tracking drops <Ns mid-rally (player out of frame)
   and resumes with a plausible continuation (no serve between, ball within court
   envelope), treat it as one rally, not two. Directly targets the "point looks
   like it ended" flaw. Delicate: must not merge genuinely separate points.
2. **Point-winner robustness** when the last tracked shot is pre-frame-exit (the
   winner is currently the last *tracked* shot's outcome — verify that's right when
   data cuts off).
3. Re-check `exclude_d` membership on the boundary shots the bridging would affect.

Do NOT pursue: recovering missed far serves in silver (bronze detector job), or
encoding line-call overrides (genuinely ambiguous).

## Verification loop (per lever)

devenv rebuild → reconciliation bench on BOTH matches → require c8b77210 unchanged
(18/18) + df594aea improved (or neutral) → `bench` green → before/after per-point
diff reviewed → ship with an env rollback. Never autonomous-flip a derived change.

## Video

Source: `s3://nextpoint-prod-uploads/wix-uploads/1784867517_Erin_vs_Jolanda.mov`
(eu-north-1). Not needed for the bench (GT + silver suffice); only pull it for
human spot-checks. `01:13:40` long.
