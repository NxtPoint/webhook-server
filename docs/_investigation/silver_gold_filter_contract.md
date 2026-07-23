# Silver → Gold filter contract

**Status:** canonical. **Written:** 2026-07-23, measured on `c8b77210` (Tomo v
Jimbo Ma) + the other seeded matches. **Why it exists:** every dashboard reads a
`gold.*` view, every gold view filters `silver.point_detail`, and if two views
filter differently their numbers will never reconcile. This is the single rule
for that filter.

---

## TL;DR

**`exclude_d IS NOT TRUE` is the one membership filter. Everything else is a
refinement *within* it.** `is_in_rally` is subsumed by it — never filter on
`is_in_rally` directly. `court_x IS NOT NULL` is **completeness, not membership**
— filtering on it silently drops real shots.

---

## The three candidate filters, measured (c8b77210, 100 rows)

| filter | rows kept | what it actually means |
|---|---|---|
| `exclude_d IS NOT TRUE` | 80 | a real in-play event (serve or rally shot) belonging to a point |
| `is_in_rally = TRUE` | 86 | SportAI's own in-rally flag — an *input* to exclude_d |
| `court_x IS NOT NULL` | 76 | a bounce coordinate was recorded — **data completeness** |

These are not three views of the same thing. They nest and diverge in ways that
matter:

### exclude_d is a strict superset of "not in rally"

Every `is_in_rally = FALSE` row is already excluded (**0 escape**), and exclude_d
removes **6 more** that SportAI called in-rally but are genuinely between-point
(pre-first-serve returns, post-point activity, the re-anchor tail). So:

```
exclude_d = (is_in_rally = FALSE)  ∪  (our own pre-serve / gap-break / warm-up exclusions)
```

Using `is_in_rally` instead of `exclude_d` would re-admit those 6 between-point
rows and corrupt every rally count. **exclude_d already contains everything
is_in_rally knows, plus more. Use exclude_d.**

### court_x (has-bounce) is completeness, and must never be a membership filter

9 of the 71 shot rows on this match are kept, real, in-play shots with **no
bounce coordinate** — a netted shot has no floor bounce, and the tracker drops
some. **A netted winner is a real point-ending shot with `court_x IS NULL`.**

**The floor-bounce coverage ceiling is physics, not a data gap (measured on
`c8b77210`, 2026-07-23).** Of the 9 bounceless real rally shots, 8 are Errors and
1 is In — and 0 of the 9 have a recoverable opponent-side floor bounce in-window
(every nearby candidate is on the hitter's own side — the ball descending, not a
landing — and is correctly rejected by the cross-net guard). Recovered
`debug_candidate` bounces only ever *add* coverage (pass-2 prefers delivered),
never replace a value. So `court_x IS NULL` on a kept shot is overwhelmingly a
real netted/out ball with no landing — not something to "fix".

**`type='swing'` bounces are contact, not landings — never accepted for
non-volleys (fixed 2026-07-23).** Every delivered `type='swing'` bounce aligns
<0.05s with a `player_swing` contact, i.e. it *is* a racket-contact point. For a
non-volley shot with no floor bounce, pass-2 used to fall back to a swing bounce —
which is the *opponent's* next contact, 20-30m from the true landing — and stamp
it as `court_x/court_y`. That poisoned every heatmap/depth/zone for ~5 shots per
match. `pass2_bounce` now accepts `type='swing'` only when `volley IS TRUE`;
otherwise the shot keeps `court_x/y` NULL (honest). Point winners are unaffected
(all such shots are mid-rally `In`). The related `rally_location_bounce`
hit-fallback was **resolved in Phase 3** (see the derived-column dictionary
below) — it is now honestly NULL when the bounce is missing.

> If a winners/errors or rally-count chart filters on `court_x IS NOT NULL`, it
> silently deletes exactly those shots — the most decisive ones. Only filter on
> `court_x IS NOT NULL` for a chart that physically needs a landing coordinate
> (a bounce heatmap dot, depth, a court zone). Never for counting shots, points,
> winners, or errors.

---

## The canonical filter ladder

Start at the top for every gold view; add a rung only for the narrower question.

```sql
-- rung 1 — MEMBERSHIP. every gold view begins here.
WHERE exclude_d IS NOT TRUE            -- 80 rows: all real serves + rally shots

-- rung 2 — SERVES (1st-serve %, aces, DFs, serve speed, serve placement)
  AND serve_d = TRUE                   -- 27 rows (includes faulted 1st serves)

-- rung 2' — RALLY SHOTS (stroke, aggression, depth, rally location, rally length)
  AND serve_d IS NOT TRUE
  AND shot_ix_in_point IS NOT NULL     -- 53 rows: the actual shots-in-a-rally

-- rung 3 — only for charts that NEED a landing coordinate (heatmaps, zones)
  AND court_x IS NOT NULL              -- completeness subset — expect gaps, label them
```

### Two invariants that make this safe (verified across all seeded matches)

1. **A serve is never excluded** (0 of 235). The exclusion rules are all
   `NOT serve_d`. So serve-only views are safe *even without* an explicit
   exclude_d — but write it anyway, so the safety is visible, not accidental.
2. **Every excluded row has `shot_ix_in_point IS NULL`** (0 violations). So a
   view filtering `shot_ix_in_point = N` cannot leak an excluded row. Again:
   true today, but rely on it explicitly at your peril — prefer exclude_d.

The 9 kept serves with no `shot_ix` are **faulted first serves**: real serves
(they count for 1st-serve %), but not the shot that started the rally.

---

## Current gold state — reconciles today, but on THREE implicit mechanisms

Audit of every `gold.*` view that reads `silver.point_detail`:

| view | filter used | safe? | why |
|---|---|---|---|
| `match_kpi` (points/games) | `exclude_d IS NOT TRUE` | ✓ | explicit |
| `match_kpi` (serve breakdown) | `serve_d = true` | ✓ | serves never excluded |
| `match_serve_breakdown` | `exclude_d IS NOT TRUE` | ✓ | explicit |
| `match_return_breakdown` | `shot_ix_in_point = 2` | ✓ | excluded ⟹ null shot_ix |
| `match_rally_breakdown` | `exclude_d IS NOT TRUE` | ✓ | explicit |
| `match_rally_length` | `exclude_d IS NOT TRUE` | ✓ | explicit |
| `match_shot_placement` | `exclude_d IS NOT TRUE` | ✓ | explicit |
| `player_match_kpis` | `exclude_d IS NOT TRUE` | ✓ | explicit |
| `vw_player` / `vw_point` | `player_id IS NOT NULL` | ⚠ | no exclude_d; safe only if consumed for roster/serve, NOT rally counts |

**The numbers reconcile today.** But they reconcile through three *different*
mechanisms — explicit `exclude_d`, "serves are never excluded", and "excluded ⟹
null shot_ix". That is fragile: a future edit that counts rally shots off
`vw_point`, or an exclusion rule that one day fires on a serve, breaks a
reconciliation that currently holds by luck of invariants.

**Recommendation (not yet done — a deliberate, reviewed change):** make rung 1
explicit in every view that reads silver, even where an invariant currently
covers it. One filter, stated everywhere, so reconciliation survives the next
change to the exclusion rules. This is a ~9-view edit; validate each view's row
counts before/after in devenv (they should not move).

---

## Architecture model — measured against the code (your framing, checked)

Your mental model was right in shape; two corrections in the detail:

- **Bronze:** 100% of the SportAI JSON is *ingested*, and the point-critical gaps
  are now closed (candidate bounces, `debug_data`, `video_info`, per-swing
  signals). But "all reconciled" overstates it — `ball_position` (5591 rows),
  `player_position` (6642), most of `debug_data.swings`, and `court_keypoints`
  are captured-but-unused. Ingested ≠ consumed.
- **Verbatim from bronze:** **16 columns**, not 18 — 12 from `player_swing`
  (player/valid/serve/swing_type/volley/is_in_rally/ball_player_distance/
  ball_speed/ball_impact_type/ball_hit_s/ball_hit_location_x/y) + 4 from
  `ball_bounce` (type/timestamp/court_x/court_y). Pass 1 and pass 2. No changes.
- **Derived:** ~37 columns, not 30. **And the `_d` suffix is NOT a reliable
  "derived" marker** — 14 columns carry it, but 23 more are equally derived and
  don't (`serve_location` 1-8, `rally_location_hit` A-D, `point_number`,
  `point_winner_player_id`, `rally_length`, the `_norm` coords, `shot_q`…).
  Don't reason "`_d` = derived, everything else = fact" — it's false.
- **Gold:** yes — a filtered, aggregated view of silver, and the filter is this
  document.

---

## A note on Excel vs the live DB

The `vw_point.xlsx` export is a convenient way to eyeball a single match against
video, and it is what the owner marks up. But for population questions —
"how many rows does each filter keep, and do they reconcile" — the live DB is the
source of truth; a spreadsheet is one match, one moment, and one person's column
selection. Every count in this document was measured against the DB, not the
export.

---

## Derived-column dictionary (Phase 3 verification, 2026-07-23)

Every derived column below was **independently re-derived from its source columns
and compared to the stored value on `c8b77210`** — all match (0 mismatches),
i.e. the builder implements each rule correctly. Silver is now **52 columns**
(16 verbatim + 3 keys/model + 33 derived) after the Phase-2 drop of `shot_q`,
`shot_key_q`, `invert_hit`, `invert_bounce`.

| column | source(s) | rule | status |
|---|---|---|---|
| `ball_hit_x_norm` | `ball_hit_location_x/y` | far hitter (hy<11.885) → `10.97 − hx`; else `hx` | re-derived ✓ |
| `ball_hit_y_norm` | `ball_hit_location_y` | far hitter → `23.77 − hy`; else `hy` | re-derived ✓ |
| `ball_bounce_x_norm` | `court_x`, `ball_hit_location_y` | near hitter (hy>11.885) → `10.97 − cx`; else `cx` | re-derived ✓ |
| `ball_bounce_y_norm` | `court_y`, `ball_hit_location_y` | near hitter → `23.77 − cy`; else `cy` | re-derived ✓ |
| `serve_bucket_d` | `serve_location` | 1,8→wide · 2,3,6,7→body · 4,5→T | re-derived ✓ |
| `depth_d` | `ball_bounce_y_norm` | serve→NULL · >20 Deep · >18 Middle · ≤18 Short | re-derived ✓ |
| `aggression_d` | `ball_hit_y_norm` | serve→NULL · ≤24 Attack · <26 Neutral · ≥26 Defence | re-derived ✓ |
| `stroke_d` | `serve_d`/`volley`/`swing_type` | Serve→Volley→Overhead→Forehand→Backhand→Slice→Other | re-derived ✓ |
| `rally_length` | `shot_ix_in_point` | ix=1→0 · else ix−1 | re-derived ✓ |

**Semantic check on the norms (not just formula match):** both players' hits map
to the same canonical half (avg `hit_y_norm` ≈ 24.8 for far and near hitters
alike) and both players' bounces to the opponent half (avg `bounce_y_norm` ≈
19.2). So serve, rally and return events overlay on one shared orientation — the
inversion is correct, not just internally consistent.

### `rally_location_bounce` — hit-fallback removed (2026-07-23)

It previously fell back to the **hit** zone when `court_x` was NULL, so ~25% of
rally shots (13/53 on `c8b77210`) carried a HIT zone under a column named for the
BOUNCE zone — contaminating every placement heatmap. Removed: a shot with no
bounce is now honestly NULL (53→40 set, 0 via fallback). Same principle as the
Phase-1 swing fix; point winners unaffected (18/18).

### Not re-derived here (owned elsewhere / known-open)

- `serve_location` (1–8), `serve_side_d`, `server_end_d`, `serve_d` — the serve
  geometry. The audit has **open P1 items** on the service-line constants
  (`6.40/17.37` should be `5.485/18.285`) and the service-box test; verify those
  as part of that fix, not here.
- `shot_outcome_d`, `ace_d`, `service_winner_d`, `point_winner_player_id` — the
  outcome chain. Validated indirectly and decisively by the **18/18 point-winner
  reconciliation against video**, which is stronger than a formula re-derivation.
- `point_number`, `game_number`, `shot_ix_in_point` — point/game structure,
  validated by the 18-point / 2-game reconciliation.

---

## The event spine (Phase 4, 2026-07-23)

**The spine already exists — it is `exclude_d IS NOT TRUE`.** There is no separate
"event index" column and there should not be. A serve attempt and a rally shot
are different event *types* with different attributes, so they are modelled as
two clean sub-populations under the one membership filter, not forced into a
single sequence. Decided with the owner (Option A: two-filter, no new column).

```
SPINE  =  exclude_d IS NOT TRUE                    (80 true events on c8b77210)
  serves :  serve_d = TRUE                          (27: 9 faulted + 18 in-serve)
            - every 1st / 2nd attempt is its own row
            - distinguished by serve_try_ix_in_point ('1st' / '2nd')
            - double faults flagged by double_fault_d (NOT by relabelling serve_try)
            - full serve_location / serve_bucket_d / serve_side_d on every attempt
  rally  :  shot_ix_in_point IS NOT NULL AND NOT serve_d   (53)
            - the rally sequence, indexed from the in-serve
            - rally_length = shot_ix_in_point − 1
```

**`shot_ix_in_point` is the RALLY spine, not the event spine.** It is NULL on
faulted serves by design — it counts from the serve that *started the rally*.
Do not extend it to index serves: `rally_length` is `shot_ix − 1`, so indexing
faulted serves would inflate every rally-length statistic. If you need a
chronological point replay, order the spine rows by `ball_hit_s` within a point —
no index column required.

### `double_fault_d` — the serve-attempt fix that came out of the spine work

The builder used to overwrite `serve_try_ix_in_point` to `'Double'` on **both**
serve rows of a double-fault point. That removed the 1st serve from the
first-serve-% denominator (audit P1). Fixed 2026-07-23:

- `serve_try_ix_in_point` now stays `'1st'` / `'2nd'` on DF points.
- a new point-level boolean `double_fault_d` (EXISTS-stamped like `ace_d`)
  carries the double fault.
- gold counts DFs via `double_fault_d`, and the first-serve denominator via
  `serve_try = '1st'` — which now correctly includes the DF point's 1st attempt
  (it faulted → NULL outcome → not counted as "in", which is right).

Measured on `c8b77210`: `first_serves_total` 17 → **18**, first-serve %
**52.9% → 50.0%** (the owner's known truth), double-fault count unchanged at 1,
18/18 point winners preserved, gold reconciles, bench green.
