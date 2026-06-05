# 18-field inherit-vs-rederive audit — bronze→silver architecture

**Status:** REFERENCE / architecture audit. 2026-05-27. Answers Tomo's question:
*"single source of truth is bronze; silver inherits 100%; no work should happen in silver."*
Verifies whether that's actually true today, field by field.

## The intended architecture (Tomo's, and it's correct as the target)
```
detectors (TrackNet / WASB / YOLOv8-pose / ViTPose)  ──▶  ml_analysis.* raw detections
analysis models (serve_detector, stroke_detector)    ──▶  ml_analysis.serve_events / stroke_events  ("final answers")
                                                            └─ this whole layer = BRONZE
build_silver_match_t5.py Pass 1   ──▶  silver.point_detail   (should be a PURE PROJECTION of the above)
build_silver_v2.py passes 3-5     ──▶  silver analytics ON TOP (score, serve location 1-8, zones, aggression)
```

**One correction to the mental model:** the raw detection **data** does NOT go to CloudWatch — CloudWatch only holds the Batch job **logs** (stdout). The actual data goes to **`ml_analysis.*` tables in Postgres**, which IS the bronze layer (both the raw detections *and* the serve/stroke model outputs live there).

## What each bronze table actually contains (verified, Match 1 `78c32f53`)
| table | rows (M1) | what it is | carries |
|---|---|---|---|
| `ball_detections` | 8005 | TrackNet/WASB ball + bounce | x,y, court_x/y, speed_kmh, **is_bounce**, is_in |
| `player_detections` | 18507 | YOLO/ViTPose players | bbox, center, court_x/y, **keypoints**, stroke_class (mostly null — classifier untrained) |
| `serve_events` | **53** | **serve_detector (pose-first, the 23/24 model)** | ts, player_id, **source** (pose_only/pose_and_ball/pose_and_bounce), **confidence**, hitter_court_x/y, bounce_court_x/y, rally_state |
| `stroke_events` | 176 | stroke_detector (velocity) | ts, predicted_hit_frame, player_id, confidence, peak_velocity — **NO swing TYPE** |

## The audit — per base field
| # | base field | bronze source available? | silver today | verdict |
|---|---|---|---|---|
| 1 | **serve** | ✅ `serve_events` (good, 23/24) | ❌ **RE-DERIVES** via bounce-geometric gate, ignores serve_events | **VIOLATION — fix now: inherit serve_events** |
| 2 | ball bounce court_x/y | ✅ `ball_detections.is_bounce`+court | ✅ inherits — **but silver adds a proximity FILTER** | ⚠️ inherits; relocate the filter to the bounce detector (bronze) |
| 3 | ball_speed | ✅ `ball_detections.speed_kmh` | ✅ inherits | ✅ clean |
| 4 | ball_hit_location x/y | ✅ player_detections / serve_events.hitter_court_x/y | ~ reads bronze position, but selects hitter via logic | ✅ ok (reads bronze) |
| 5 | ball_hit_s (timing) | ✅ serve_events.ts / stroke_events.ts | ⚠️ uses the **bounce** ts, not the stroke/serve event ts | ⚠️ should inherit event timing |
| 6 | player_id (who) | ⚠️ only side-based in every table; **no stable identity** | RE-DERIVES by court side | model gap (identity) — stopgap |
| 7 | **swing_type** (fh/bh/overhead) | ❌ **no model emits type** (stroke_events has none; stroke_classifier untrained) | RE-DERIVES from pose | **model gap — silver stopgap until classifier trained** |
| 8 | **volley** | ❌ no model emits it | DERIVES via net-distance heuristic | **model gap — silver stopgap / analytic** |
| 9 | ball_player_distance | derived from two bronze positions | computed | ✅ legit derivation |
| 10 | is_in_rally | — | constant True | trivial |

## The honest conclusion (this is the nuance)
Your principle is the **right target**, and it's **partly true today** — but not fully, for two *different* reasons:

1. **One real architectural VIOLATION** — `serve`. The model output (`serve_events`, the 23/24 pose-first detector) **exists and is good**, but silver throws it away and re-derives serves from bounces (the inferior "15"). This is the "lost the plot" — and it's **fixable now** by inheriting `serve_events`. The bounce-geometric serve gate is the rogue code to delete.

2. **Model GAPS** — `swing_type`, `volley`, `identity`. Here silver re-derives **not** because of rogue code, but because **no model emits these facts yet**: the stroke_classifier (which would emit fh/bh) is **untrained/dormant**, there's no volley model, and nothing emits stable A/B identity. So silver's pose-inference / heuristics are **necessary stopgaps** that fill the gap until those models exist.

**So "silver inherits 100%, no work in silver" is the END STATE we reach as the models get trained** — it can't be fully achieved today by deleting silver code, because for swing/volley/identity there's nothing in bronze to inherit *from* yet. This is exactly the build-first/train-last ladder: train the missing models → they emit to bronze → silver inherits → delete the stopgap.

## Action plan (sequenced, bronze-first)
**Now (model output exists → inherit, delete re-derivation):**
1. **Wire `serve_events` → silver serves** (confidence-filtered to land near the true count; carries ts + hitter + bounce already). **Delete the bounce-geometric serve gate.** ← biggest architectural win.
2. **Relocate the bounce proximity-filter** from silver into the bounce detector (Batch) so silver purely inherits `is_bounce`. (Until then it's a flagged silver filter.)
3. **ball_hit_s**: prefer serve_events/stroke_events timing over bounce ts.

**Later (no model output yet → train the model, then inherit):**
4. **swing_type** → train the stroke_classifier (emits fh/bh to bronze) → silver inherits → delete pose-inference stopgap.
5. **identity** → an identity model/signal (or accept "Near/Far") → silver inherits.
6. **volley** → derive from a model signal (ball-not-bounced-before-hit) or keep as a labelled silver analytic.

**Governance:** every silver "derivation" that isn't pure projection or a legitimate analytic (score/zone/serve-location) must be tagged in code as either (a) inherit-from-bronze, or (b) STOPGAP-until-model-X. No silent re-derivation.

---

## The target architecture — ONE MODEL PER FACT (Tomo's framing, 2026-05-27)

The organising principle: **a fact is "done" when a dedicated model turns raw detections into a normalised bronze answer, and silver merely projects it.** Serve "works" precisely because it has a model (`serve_detector`). The fields that don't work are the ones with no model — silver is doing the model's job inside a SQL/Python query on raw data. That's the anti-pattern to eliminate.

```
RAW layer    detectors           →  per-frame detections (noisy, no semantics)
             TrackNet/WASB           ml_analysis.ball_detections (x,y,court,is_bounce)
             YOLOv8/ViTPose          ml_analysis.player_detections (bbox,pose)
                                      court_detector (calibration)
MODEL layer  one model per fact   →  normalised "final answer" events (THIS is what was missing)
             serve_detector          ml_analysis.serve_events        ✅ EXISTS
             stroke_detector         ml_analysis.stroke_events (timing only — NO type)  ⚠️ partial
             ↳ swing-type classifier (fh/bh/overhead)                 ❌ MISSING (untrained)
             ↳ bounce detector (true ground-contact model)            ❌ MISSING (only a velocity-reversal rule lives inside ball_tracker)
             ↳ identity model (stable A/B)                            ❌ MISSING
             ↳ volley signal (ball-not-bounced-before-hit)            ❌ MISSING
BRONZE       = the MODEL-layer event tables (the normalised answers)
SILVER       pure projection of bronze events → point_detail, + analytics (score, serve location 1-8, zones, aggression). NO re-derivation.
```

**Build backlog reframed as "build the missing models":**
| fact | model today | action |
|---|---|---|
| serve | `serve_detector` ✅ | wire `serve_events`→silver NOW; improve model precision (over-fires 51 vs 25 on M1) |
| ball bounce | velocity-reversal rule *inside* ball_tracker | promote to a real bounce model in the MODEL layer; move the silver proximity-filter into it |
| swing type | none (classifier untrained) | train stroke_classifier → emits fh/bh to bronze → silver inherits |
| identity | none | identity model or accept Near/Far |
| volley | none | derive in a model from bounce-vs-hit timing |

**Answer to "are we overthinking it?": no — this IS the right structure, and it unifies everything.** "Build the 18 to 70-80%" = "build a model per fact." "Train to 90-95% free via dual-submit" = train each model. "Silver inherits 100%" = the end state once every fact has a model. We do NOT need to re-discover this with more agents; we need to build the models one at a time (and can parallelise *independent* model builds later).

---

## UPDATE 2026-05-27 — serve-wiring attempt: the gate can't just be deleted (pass-3 coupling)

Tried wiring `serve_events` → silver (conf≥0.70) + deleting the bounce-geometric serve gate. **Validation on Match 1 caught a regression — reverted, not committed.** Findings:
- Sourcing serves from `serve_events` and **appending** the bounce-less ones: serve recall 40 %→**60 %**, but **points 17→11** (SA 18) — the appended serves lack bounce coords, so pass-3 can't derive their `serve_side_d` → point-anchoring breaks.
- Sourcing only the bounce-coincident serve_events (no append): points **17→11** *and* recall **40 %→32 %** — worse on both.
- **Root cause:** pass-3 point/serve-side numbering is **coupled to the bounce-gate serves** (which carry the bounce geometry pass-3 reads). And `serve_events` itself **over-fires** on M1 (51 raw / 26 @conf≥0.70 vs SA 25) — a model-precision issue.

**So the serve fix is a 2-part effort, not a one-line wire:**
1. Rework **pass-3** (in `build_silver_v2.py`, shared with SA — careful) to derive point boundaries + `serve_side_d` from `serve_events` (hitter position) rather than from the bounce on serve rows.
2. THEN delete the bounce-geometric gate. Plus serve-model precision (model-side).

Until then the bounce-geometric serve gate stays as a **TAGGED stopgap** (it currently yields better silver metrics — 17 points / 40 % recall — *because* pass-3 is coupled to it). Tag it in code as `STOPGAP-until-pass3-inherits-serve_events`. This is the measure-first discipline working: it stopped a points regression from shipping.

---

## ★ UPDATE 2026-06-05 — silver-heuristic audit (Tomo's "clean silver, inherit bronze 100%, no exceptions") + the STROKE = BALL-HIT reframe

**The unlocking insight (Tomo, 2026-06-05): a stroke IS a ball-hit — one and the same event.** So bronze `stroke_events` should be the canonical *hit* event carrying `{frame, player_id, swing_type, ball_hit_location_x/y, ball_hit_s}`, and **silver must be STROKE-DRIVEN: exactly one row per bronze hit event, projected verbatim.** Bounces are a *separate* bronze fact (where the ball landed) attached to the hit for outcome/zone — they must NOT generate rows.

**Today silver is BOUNCE-DRIVEN** (`_t5_pass1_load_bounce_driven`): it iterates bounces and *heuristically reconstructs* the hit. **That inversion is the source of nearly all the debt AND the overcount.** Tomo's corollary: once silver is hit-driven, *a row exists only if there's a valid stroke with a valid hit* — so pre-serve racquet taps, missed hits, double bounces, phantom bounces all vanish automatically. T5's inflated count collapses from ~162/343 toward **the real ~84 hits** as a *consequence of correctness*, not a filter. **Bronze is the answer; silver is never the answer.**

### Full Pass-1 heuristic debt catalog (the cleanup checklist)
Everything in `build_silver_match_t5.py::_t5_pass1_load_bounce_driven` that computes a base fact = DEBT to delete once bronze is right. The shared passes 3–5 (`build_silver_v2.py`) are legitimate analytics (KEEP).

| Silver logic (Pass 1) | Verdict | Bronze owner it belongs to |
|---|---|---|
| Bounce-driven **row generation** (1 row/bounce) | 🔴 DEBT | `stroke_events` (1 row per hit) |
| **Hitter attribution** — `_build_player_buckets`, `_find_nearest_detection`, soft-window, **mirror-fallback**, stale-tagging | 🔴 DEBT | `stroke_events.player_id` |
| **Geometric serve** — `_serve_geometric_check`, `_check_hitter_stationary_pre_hit`, cooldown, `FIRST_SERVE_MIN_TS`, `_is_overhead_pose` | 🔴 DEBT (partly addressed by `T5_SERVE_FROM_EVENTS` overlay) | `serve_events` |
| **swing_type** — `_infer_swing_type_from_keypoints` / `_infer_swing_type_from_position` | 🔴 DEBT (swing classifier now exists but is disabled — failed gate) | `stroke_events.swing_type` |
| **volley** — net-distance proxy | 🔴 DEBT | `stroke_events` (ball-not-bounced-before-hit signal) |
| **ball_hit_location_x/y** — hitter court_x/y at the bounce | 🔴 DEBT | `stroke_events.ball_hit_location` (the stroke *is* the hit) |
| **ball_hit_s** — uses the **bounce** ts (wrong; ~0.3–0.5s after the hit) | 🔴 DEBT | `stroke_events` hit time |
| Bounce **proximity guard**, **gap_break** re-anchor, **exclude_d** | 🔴 DEBT (exclusion heuristics) | eliminated by hit-driven: no valid stroke+hit ⇒ no row |
| Point/game structure, server alternation | 🟢 KEEP (analytics — not in bronze) | silver |
| serve location 1-8 / serve_side_d / zones (A-D) / aggression / depth | 🟢 KEEP (analytics) | silver |
| stroke_d (swing_type → Forehand/Backhand/… mapping), rally_length | 🟢 KEEP (rename/count) | silver |
| shot_outcome_d (Winner/Error/In), ace/DF/service-winner/point/game winner | 🟢 KEEP — geometric in/out of the *bounce* fact + sequence logic (conditional on correct bronze bounce coords) | silver |

### Per-fact status refresh (what changed since 2026-05-27)
- **serve** — `T5_SERVE_FROM_EVENTS` overlay now inherits `serve_events`; the geometric gate remains a tagged stopgap (pass-3 coupling, line 104-108, still open). Model over-fires (precision) — the serve "check + train + lock" step.
- **ball bounce** — the velocity-reversal rule in `ball_tracker.detect_bounces` is the rogue base-fact computation; the **bounce CNN v2** is now validated (`bounce_detector_v2_7match.pt`, gravity_residual, **precision 20%→37%, count 343→172 ≈ SA 162** at thr 0.5). Promote it to THE bronze bounce model (MODEL layer) → silver inherits. *(This is "finish bounce.")*
- **swing_type** — classifier now EXISTS (trained, deployed rev 64, **disabled rev 65** — failed the gate: no "other" class → forces volleys/serves→forehand). Needs v2.1 (4th class) before it's the bronze answer.
- **stroke = ball-hit** — `stroke_events` still carries timing only (**NO swing_type, NO ball_hit_location**). Making it carry both is the keystone that lets silver go hit-driven.
- **identity** — still Near/Far only; unchanged.

### Locked roadmap (Tomo's order, 2026-06-05) — bronze-first, then silver becomes a thin projection
1. **Bounce** → promote CNN v2 to the bronze bounce model. *(finish bounce)*
2. **Serve** → fix pass-3 to anchor on `serve_events` (hitter pos, not the bounce on serve rows) + serve-model precision; then delete the geometric gate. *(check → model → train → lock)*
3. **Stroke = ball-hit** → bronze `stroke_events` carries `swing_type` + `ball_hit_location` (+ correct hitter attribution — the perspective-bias, rule #11). The hard one.
4. **Flip silver to STROKE-DRIVEN** (`T5_STROKE_DRIVEN_SILVER`): 1 row per bronze hit, project verbatim, feed the SAME passes 3–5. **Delete the entire Pass-1 debt list above.** Overcounts die (→ ~84 real hits). Gate: per-hit reconcile vs SA must hold/improve.

**Governance reaffirmed:** no new base-fact logic in silver. Every existing silver derivation is either KEEP (analytic above) or tagged `STOPGAP-until-<bronze model>`. We extend bronze models, never silver heuristics.
