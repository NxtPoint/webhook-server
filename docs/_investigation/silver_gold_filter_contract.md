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
