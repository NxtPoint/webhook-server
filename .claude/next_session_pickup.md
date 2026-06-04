# Next-session pickup — 2026-06-04 — far-side bugs fixed + swing classifier built; next = re-measure the 18-field scorecard

## ⚡ Executive summary (read first — 30 seconds)
**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN.
**Pipeline (Tomo's mental model, CONFIRMED correct):** SportAI upload → auto T5 shadow → auto-ingest → auto-corpus-label is **FULLY AUTOMATED** in prod (both `AUTO_DUAL_SUBMIT_T5` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS` ON). **Training is manual when required (by design).** T5 runtime optimised + accuracy-clean. Infra transfer to friend's A4000 box = future phase (spec in chat 2026-06-04).
**What landed this session (huge):**
- **Runtime:** B1 decode-skip + D1 + B2 shipped (118→~109 min, accuracy-clean). Queue switched to **g4-primary → g5-backup → Spot** (g4 ~1.45× slower but ~24% cheaper/match + matches the A4000 target; validated A/B: g4 158min/65ms-fr vs g5 109min/42ms-fr). Leaking + retired GPU boxes killed.
- **🎯 FAR-SIDE: it was BUGS, not physics** (the session's big win). Two fixable bugs found + fixed: (1) **frame-space mismatch** (`fab487a`) — SA hit_frame in source-fps matched against 25fps detections, dropped 62% of swing training hits bimodal-by-fps; (2) **far NULL court_y** (`353a6cc`) — strict ±5m bound nulls ~50% of far detections (court_y overshoots 2.4-7m). Combined: swing training data **786 → 1588 hits, FAR 207 → 595 (~2.9×)**. Did NOT relax map_to_court (would store bad far coords in silver — precise far coords stay a calibration task).
- **Swing classifier (stroke TYPE) — went from UNTRAINED (0%) to a real v1/v2 model.** v2 (far-rich): val macro-F1 **0.77** (fh 0.77 / bh 0.68 / overhead 0.87). Per-role NEAR/FAR eval added (`b07be60`) — far number pending (eval running). Weights `models/swing_classifier_v2.pt`, NOT deployed.
- **Bounce v2 retrained** on 7-match corpus: val F1 0.40 → **0.54** (still recall-limited). Weights `models/bounce_detector_v2_7match.pt`, NOT deployed.
- **Corpus paired 3 more matches** (now 7-8: bounce 2477 / swing 1763 / serve 395 labels).

If that's enough, go. Depth below.

## 🧭 WHERE WE ARE vs True North — the model/field scorecard (what Tomo asked 2026-06-04)
Build-first/train-last. Status of the buildable models (one-per-fact):
| Model | Status | At ~70% build bar? |
|---|---|---|
| **1. Serve** (serve_detector) | dev ceiling (bench 20/24, 23/24; count-aligned 26/26); far recall = residual | ✅ build-done → train selectively |
| **2. Stroke TYPE** (stroke_classifier) | **NEW v1/v2 model this session** (was 0%/heuristic). v2 macro-F1 0.77 — **per-role: NEAR 0.86, FAR 0.61** | ✅ **at bar** (far 0.61 ~= 60-70% on the hardest fact; near 0.86) |
| **3. Ball bounce** (bounce_detector) | v2 F1 0.54, recall-limited | ❌ **weakest — below bar** |
| **4. Ball track + hit** (WASB/TrackNet + hit timing) | ball detection ~build-done; ball_hit_location populated, accuracy unmeasured | ~partial |
| **5. Court calibration** (CNN+Hough) | ~88-94%, silent-degeneracy fixed; far-coord extrapolation overshoot remains | ✅ build-done (far-coord caveat) |
| (Player A/B identity) | Near/Far only; stable identity NOT solved (Q2-B blocked) | ❌ below bar |

**Answer to "are we only training for accuracy now?": NOT YET.** Serve + court are build-done; stroke-TYPE just got its first model today; but **ball bounce + A/B identity are still below the build bar**, and ball_hit_location accuracy is unmeasured. So it's a mix: train-selectively on the done ones, finish the build on bounce + identity.

## 🎯 NEXT ON THE RADAR (highest-value, True-North-aligned)
1. **RE-MEASURE the 18-field reconciliation vs SportAI** — `harness reconcile <sa> <t5>`. Today's far-side bug fixes (frame-space + far court_y) almost certainly improved the bronze-vs-SA alignment (the whole "bronze ≈ SportAI" game). The 18-field table below is STALE (2026-05-27, pre-fixes). Re-running it tells us how close to "dev done" we actually are now. **This is the scorecard that decides what's left.**
2. **Finish the per-role swing eval** (running) → know far swing F1 → decide if stroke-TYPE is build-done or needs more far data.
3. **Ball bounce** (the weakest) — lift recall: gravity-residual candidate-gen + the far-side fixes (re-build bounce dataset with the frame-space fix — likely has the SAME bug as the swing builder did) + more diverse matches.
4. **A/B identity** — product call: build changeover detection OR formally accept "Near/Far" as identity.
5. THEN sign off "dev done" → shift fully to selective training (already automated).

## Open items
| # | Item | Notes |
|---|---|---|
| 1 | Per-role swing eval | running locally (CPU); far macro-F1 pending |
| 2 | Bounce dataset frame-space bug? | the swing builder had it (fixed); CHECK build_serve_bounce_dataset / bounce manifest for the same fps mismatch |
| 3 | Deploy decisions | swing v2 + bounce v2 trained but NOT deployed — need per-role/far validation + rule-#8 rebuild + sign-off |
| 4 | Lambda function deploy | still IAM-blocked (needs Tomo cred) |
| 5 | Infra transfer to A4000 box | future phase; spec + polling-worker design in 2026-06-04 chat |

## Canonical state
- Batch job-def: **eu rev 62 / us rev 43** (b5gate image, MOG2=4, imgsz1280, g4-primary queue).
- GPU dev box: `i-0295d636` (t5-dev-gpu-1b, Tesla T4) — STOPPED. Drive via the runbook (`.claude/infrastructure/gpu_dev_box_runbook.md`); rsync absent on Win box → use tar+scp / S3.
- New weights (local, NOT deployed): `swing_classifier_v2.pt`, `bounce_detector_v2_7match.pt`.
---
**END OF PICKUP**
