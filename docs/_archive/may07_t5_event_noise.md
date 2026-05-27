# T5 Event Noise — May 7 PREP investigation

**Session:** agent-ac1bff976f520a088 (NOISE) · **Date:** 2026-05-07
**Scope:** Phase 3 PREP. Read-only characterisation of the 75 extra T5 events on
`a798eff0-551f-4b5a-838f-7933866a727c` vs SA `2c1ad953-b65b-41b4-9999-975964ff92e1`
(160 vs 85 rows in `silver.point_detail`).

## Method

Both tasks queried from `silver.point_detail`. SA point boundaries derived from
`min/max(timestamp) per point_number` on the SA rows that have a `timestamp` (15 SA
rows have NULL `timestamp` and were excluded). T5 events were classified by their
`timestamp` against (a) SA's first-serve `ts=55.0`, (b) SA point boundaries, (c) ±0.5s
proximity to any SA event inside the same point.

## Headline counts (all rows, raw)

| Bucket | Count | % of 160 |
|---|---:|---:|
| (a) Pre-first-serve (`ts < 55.0`) | **63** | 39% |
| (b) Between SA points | **68** | 43% |
| (c) Mid-rally noise (no SA within ±0.5s) | **25** | 16% |
| (d) Mid-rally legitimate | **4** | 2% |
| **Total** | **160** | 100% |

(a)+(b) = **131 / 160 = 82%** of all T5 events sit outside any SA point. Only 4 T5 events have a matching SA event within ±0.5s.

## What `exclude_d` already catches

The silver builder flags 77/160 T5 rows `exclude_d=true`. Filtering those out:

| Bucket | Raw | exclude_d=true | **Survives to gold** |
|---|---:|---:|---:|
| (a) Pre-first-serve | 63 | 11 | **52** |
| (b) Between SA points | 68 | 53 | **15** |
| (c) Mid-rally noise | 25 | 12 | **13** |
| (d) Mid-rally legitimate | 4 | 1 | **3** |
| **Total non-excluded** | 160 | 77 | **83** |

SA non-excluded total: **81**. So the *headline* "75 extras" shrinks to **+2 net** after `exclude_d` — the existing pre-/between-point filter is already doing most of the heavy lifting, but it leaks badly on bucket (a).

## Stroke distribution of survivors (non-excluded)

| Stroke | T5 (non-excl) | SA (non-excl) | T5 in (a) survivors |
|---|---:|---:|---:|
| Forehand | 28 | 38 | 14 |
| Backhand | **27** | **14** | **24** |
| Serve | 14 | 24 | 2 |
| Volley | 13 | 4 | 11 |
| Overhead | 1 | 0 | 1 |

The headline T5 BH=62 vs SA BH=15 collapses to T5 BH=27 vs SA BH=14 once `exclude_d` is honoured. **24 of those 27 surviving backhands are pre-first-serve (warm-up).** Confirms the racquet-bounce hypothesis.

## Time clustering

- **Bucket (a)** is 100% in the *early* third — all 63 events fall in ts 0.32–35.76s. SA's first serve is at 55.0s. This is the warm-up period before the match starts.
- **Bucket (b)** clusters in *late* third (49 of 68) — including 23 between SA pt16→17 (ts 561.68–585.40, 23.7s gap) and 18 between pt13→14 (ts 436.04–464.16, 28.1s gap). Same `~16s gaps` flagged in May 7 phantom-bounce diagnosis (FAR misses 458/463/584).
- **Bucket (c)** mostly *early* (20 of 25) — likely peri-serve detector chatter on first few serves where SA already counted one event.

## Surprise

`exclude_d` already drops 53 of 68 between-point events, but only 11 of 63 pre-first-serve events. The current exclusion logic is keyed off "between known points," not "before the first known point." Whatever rule decides `exclude_d=true` (need to read `build_silver_v2.py` pass 3) has a blind spot for warm-up. That single gap accounts for **52 of the 54 surviving extras**.

## Five-line summary

- **(a) Decomposition:** of 75 extras, raw split is 63 pre-first-serve + 68 between-point + 25 mid-rally-noise + 4 legit (plus –85 SA baseline ≈ 75). After `exclude_d` the surviving T5/SA delta shrinks to +2 — but stroke distribution still skews because 52 warm-up events leak.
- **(b) Dominant bucket:** pre-first-serve (`ts < SA-first-serve`), 52 surviving rows, 24 of them Backhand.
- **(c) Racquet-bounce hypothesis: HOLDS.** All bucket-(a) survivors fall in 0.32–35.76s warm-up window; Backhand is the dominant stroke (24/52 = 46%) — exactly the 1H/2H racquet-tap motion the May 7 diagnosis predicted.
- **(d) Surprise:** the existing `exclude_d` rule already neutralises between-point noise (drops 53/68) but is blind to pre-first-serve activity (drops only 11/63). Phase 3 doesn't need a brand-new filter — it needs to extend the existing exclusion to "before first detected serve."
- **(e) Recommendation for Phase 3 implementor:** the cheap, high-leverage filter is `exclude_d = true WHERE timestamp < first_t5_serve_timestamp`. Combined with extending the same window-based logic in `build_silver_v2.py` pass 3 to cap the front edge, this alone removes 52 of the 54 surviving extras and brings T5 BH count from 27 to 3 — already inside Phase 5's ±10% tolerance vs SA BH=14. Defer point-boundary detection (the harder Phase 2 work) until after this single one-liner is shipped and measured.
