# SA point 6 gap on 880dff02 — bronze ball-detection coverage gap

**Date:** 2026-05-07  Agent: POINT6  Branch: `investigate/sa-point6-gap`  Read-only diagnosis.

**Pair:** T5 `880dff02-58bd-412c-9a29-5c5151004447`  vs  SA `2c1ad953-b65b-41b4-9999-975964ff92e1`

## Verdict

**Bucket (b) — bronze detection gap.** The T5 Batch ball tracker (TrackNetV2) emitted **zero ball detections** anywhere inside SA point 6's window. `build_silver_match_t5._t5_pass1_load` iterates `ml_analysis.ball_detections WHERE is_bounce=TRUE` and inserts one silver row per bounce — no bounces ⇒ no silver rows. Silver is doing exactly what its inputs say.

## Evidence

### SA point 6 window
9 strokes spanning **224.960 – 239.120 s** (14.16 s rally, gaps 1.0–4.0 s). Inter-stroke spacings: `[1.00, 1.48, 1.04, 3.96, 1.48, 1.56, 1.68, 1.96]`. Adjacent points 5 (3 strokes, 178.44–195.96) and 7 (3 strokes, 272.76–274.88) frame a real rally — not an ace, not a snippet.

Window expanded by ±1.0 s = `[223.960, 240.120]` ⇒ **frames [5599, 6003] @ fps=25.0** (verified from `video_analysis_jobs`: 15300 frames / 612.0 s).

### T5 silver in window (model='t5', incl. exclude_d)
| Rows in window | Total T5 silver rows | Total `exclude_d=TRUE` |
|---|---|---|
| **0** | 160 | 111 |

T5 silver `ball_hit_s` range is 0.32–589.40 s, so the row span covers point 6's window — silver simply has nothing there.

### T5 bronze coverage in window
| Source | Count in [5599, 6003] | Total in match | Notes |
|---|---|---|---|
| `ml_analysis.player_detections` | **490** | 17,443 | Player 0: 400 (court=400). Player 1: 90 (court=10). 25/sec consistently — full coverage. |
| `ml_analysis.ball_detections` (any) | **0** | 1,983 | — |
| `ml_analysis.ball_detections` (is_bounce) | **0** | 162 | — |
| `ml_analysis.serve_events` | **2** | 107 | ts=225.00 + 235.60, both `source='pose_only'`, `rally_state='between_points'` (no bounce to anchor). |

### Match-wide ball-detection gap pattern (top 5)
| gap (s) | frame range | ts range | encloses SA point? |
|---|---|---|---|
| 91.6 | 7539 → 9829 | 301.56 → 393.16 | yes — multiple |
| 73.2 | 899 → 2728 | 35.96 → 109.12 | yes |
| **61.8** | **5347 → 6892** | **213.88 → 275.68** | **yes — point 6 (224.96–239.12)** |
| 52.5 | 10009 → 11322 | 400.36 → 452.88 | yes |
| 42.2 | 11669 → 12723 | 466.76 → 508.92 | yes |

1,983 ball detections / 612 s ≈ 3.2 det/s vs 25 fps — only ~13 % of frames have any ball detection. Six gaps >40 s each, every one wide enough to swallow a whole point.

### Why silver has nothing to do here
`build_silver_match_t5.py:437` is the only entry point: it iterates `ball_detections WHERE is_bounce=TRUE`, looks up the nearest player detection per bounce, inserts one silver row per bounce. No bounces in [5599, 6003] ⇒ pass 1 emits zero rows for that window; passes 3-5 cannot resurrect rows that were never inserted. `serve_events` are not consumed by silver (the unshipped `silver/connect-serve-events` branch is exactly that wiring).

## Actionable next step
**Multi-week detector investment** — not a 1-day silver fix. The phantom-bounce class targeted by Phase 1 is a *false-positive* problem; this is the inverse — *false negatives* in TrackNetV2's far-half ball tracking. Six >40 s ball-coverage gaps point at a model-recall ceiling, not a silver bug. Workstream candidates: TrackNetV3 weight retrain, frame-delta Hough fallback gain-up, ROI bounce extractor (currently STUB per Phase 6 note). This is North-Star Phase 6 territory.

## Five-line summary
- **(a) Bucket:** (b) bronze detection gap.
- **(b) Counts in [5599, 6003]:** player_detections **490 / 17,443**, ball_detections **0 / 1,983**, bounces **0 / 162**, serve_events **2** pose-only.
- **(c) Pattern:** zero ball detections in the 61.8 s gap frames 5347→6892 (ts 213.88→275.68). Match-wide six >40 s ball-coverage gaps; only 13 % of frames have any ball detection.
- **(d) Next step:** detector investment (TrackNetV3, fallback recall, ROI bounce extractor) — not a silver fix.
- **(e) Surprise:** player tracking is *fine* over the window (25/s, 100 % court coords for player 0); the gap is purely ball-side. Two pose-only serve_events sit in-window but silver doesn't consume them.
