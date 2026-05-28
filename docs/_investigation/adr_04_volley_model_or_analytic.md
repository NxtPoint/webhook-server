# ADR-04: Volley — dedicated model or analytic derivation?

**Status:** APPROVED 2026-05-28 (architectural + research-grounded spec). Build is BLOCKED by ADR-01 (bounce model) and ADR-02 (swing-type classifier) — depends on their outputs.
**Owner:** Tomo decides; any agent can implement post-approval.
**Sequence:** see [ADR-05](./adr_05_detector_build_sequencing.md). **Blocked by ADR-01 (bounce model) and ADR-02 (swing-type classifier)** — depends on their outputs.
**Last updated:** 2026-05-28.

## Context

Per [bronze_silver_18_audit.md](./bronze_silver_18_audit.md):

> "volley | none (no model emits it) | DERIVES via net-distance heuristic | **model gap — silver stopgap / analytic**"

Today the volley flag is set by a net-distance heuristic inside `stroke_detector` / `build_silver_match_t5`: if the hit happens within a configured distance of the net, it's a volley. T5 emits 13 volleys vs SportAI's 6 on Match 1 — over 2× over-count. The proxy is too loose because a baseliner hitting a deep ball can be classified as a volley by net-distance alone, despite the ball having bounced first.

The **true definition** of a volley is mechanical: *a stroke that hits the ball before it bounces on this side of the net*. That's an event-order check on top of (a) stroke events and (b) bounce events — no new perception model is needed once we have a real bounce model (ADR-01) and accurate stroke timing (already in `stroke_events`).

This is structurally identical to `ball_player_distance` ([audit row #9](./bronze_silver_18_audit.md)): derived from two bronze positions, not modelled. Both are *legitimate derivations*, not stopgaps — provided they live in **bronze**, not silver.

## Sub-questions

1. **Model or analytic?** Train a dedicated volley detector, or derive from bounce + stroke events?
2. **If analytic, where does it live?** A bronze post-processing step, or silver pass 3-5?
3. **Output shape** — flag-on-stroke, separate `volley_events` table, or replace the existing `volley` column on `stroke_events`?

## Options

### Q1 — model vs analytic
| Option | Pros | Cons |
|---|---|---|
| **A. Pure analytic** (derive from `stroke_events` + `ball_bounces` order) | No perception model to train; correctness follows from the definition; cost amortised against ADR-01 and ADR-02 which we're building anyway; transparent + debuggable | Only as good as its inputs — bad bounce detection → bad volley flag (but that's true of any approach; a dedicated model would have the same dependency) |
| **B. Dedicated volley classifier** (CNN on pose + ball trajectory window around hit) | Independent of bounce-detection quality | Duplicates work — a volley is mechanically *not-a-groundstroke*; training a model for it is solving a problem we've already solved as soon as bounces are right; corpus extension needed; weeks of work for a derivative fact |
| **C. Hybrid — analytic with a model confidence boost** | Catches edge cases the analytic misses (drop-shot vs volley confusion) | Complexity for marginal gain; postpone until measurement says we need it |

### Q2 — placement (if analytic)
| Option | Pros | Cons |
|---|---|---|
| **A. Bronze post-processing step** (`ml_pipeline/analytics/volley_derive.py`, runs after bounce_detector + swing_type classifier emit) — adds `volley_flag` to `stroke_events` | Honours "silver does no work on base facts"; volley is a base fact even if derived; downstream silver inherits | One more module to run in the ingest flow |
| **B. Silver pass 3-5 derivation** | One fewer module; passes already exist | Violates "silver does no work" — volley IS a base fact (one of the 18); deriving it in silver is exactly the anti-pattern the rules call out |
| **C. Inside `stroke_detector` itself** | Consolidates stroke-related logic | Couples stroke_detector to bounce_detector outputs (order dependency); breaks single-responsibility |

### Q3 — output shape
| Option | Pros | Cons |
|---|---|---|
| **A. Flag-on-stroke** — `stroke_events.volley_flag BOOLEAN` + `stroke_events.volley_confidence FLOAT` | Natural shape; one row per stroke; silver joins easily | Requires coordination with parallel agent (they own stroke_detector — but the column add is independent of detector logic) |
| **B. Separate `volley_events` table** | Cleaner separation | Awkward — every stroke either is or isn't a volley; a flag is the right shape; a separate table over-engineers it |
| **C. Replace existing `volley` column** | One fewer column | Existing `volley` is the heuristic; we want both for measurement during transition (does the model agree with the heuristic on the 6 SA cases? where do they disagree?) — keep both initially, deprecate the old one once new one is proven |

## Recommendation

**Q1: A — Pure analytic.**
Volley is genuinely derivative — it falls out as soon as ADR-01 (bounce model) and accurate stroke timing exist. Training a dedicated classifier would solve a problem we've already solved upstream. The volley over-count today is a *symptom* of bounce-detection imprecision, not a perception problem.

**Q2: A — Bronze post-processing step.**
Lives in a new `ml_pipeline/analytics/` directory (the first member of an "analytics" tier that sits between perception modules and silver). The directory captures the architectural fact that *some* base facts are derived from other base facts, but the derivation still belongs in bronze (so silver can inherit). Same conceptual tier as `ball_player_distance`.

**Q3: A — Flag-on-stroke with confidence.**
Coordinate with the parallel agent to add `volley_flag BOOLEAN` and `volley_confidence FLOAT` columns to `stroke_events` (or to a side-table joined on `stroke_event_id` if they prefer not to grow `stroke_events`). Keep the existing heuristic `volley` column initially for measurement; deprecate once the new one is proven on Match 1 + Match 2 + Match 3.

## Algorithm sketch

Pseudo-code for the bronze analytic step:

```
for each stroke_event s in chronological order:
    last_opponent_stroke = nearest preceding stroke from opponent
    bounces_since = ball_bounces between last_opponent_stroke.ts and s.ts
    bounces_on_this_side = filter bounces_since by player-side
    s.volley_flag = (len(bounces_on_this_side) == 0)
    s.volley_confidence = function of:
       - confidence of bounce_detector for the bounces in the window
       - clarity of court-side assignment for the bounces
       - time gap (very short = ambiguous; clean window = high conf)
```

The algorithm is ~30 lines. Bench: agreement with SportAI's `volley_flag` on Match 1's 6 volleys (and disagreement-explanation on the 13-6=7 false volleys T5 currently emits).

## Open follow-ups (decide at build time)

1. **Order dependency** — needs to run AFTER bounce_detector AND after swing-type classifier (to know which side hit). Wire into ingest flow accordingly.
2. **First-stroke-of-point handling** — a return-of-serve happens after a serve bounce, but the serve isn't "an opponent's stroke" in the same sense. Algorithm needs to seed correctly from `serve_events`.
3. **Half-volley** — ambiguous case (ball just bounced, hit on the rise). Decide whether to mark as volley or groundstroke; SA's choice is probably the safest default. Measure on real corpus.
4. **Net-cord handling** — depends on whether bounce model from ADR-01 emits net-cord events as a separate class.

---

## Build spec v1 (research-grounded, 2026-05-28)

### Authoritative volley definition

USTA glossary (canonical public source): a **volley** is "a stroke made by hitting the ball before it has touched the ground"; a **half-volley** is "the stroke made by hitting a ball immediately after it has touched the ground." ITF Rule 24 frames the underlying mechanic — "before the ball bounces on my side" is the rule-grounded discriminator.

### Edge-case classification (per ADR-04 decision, 2026-05-28: `half_volley_flag` kept as separate output)

| Sub-type | Mechanic | `volley_flag` | `half_volley_flag` | `volley_subtype` (v1) |
|---|---|---|---|---|
| Standard / punch volley | Hit before bounce, short swing | true | false | `'punch'` (default) |
| Drive volley | Hit before bounce, full groundstroke swing | true | false | NULL (derive from swing kinematics in v2) |
| Swinging volley | Hit before bounce, full swing, often above shoulder | true | false | NULL (v2) |
| **Half-volley** | Bounce → hit within ≤ 100 ms / ≤ 0.3 m of bounce | false | **true** | `'half'` |
| Return of serve | Serve bounces on receiver side, then receiver hits | false | false | NULL |
| Net-cord during rally | Ball clips net; per ITF play continues; receiver's stroke classified by what happens next | per mechanic | per mechanic | NULL |
| Overhead / smash | Hit before bounce, above head, off a lob | true | false | NULL (v2 — derive from contact-point height) |

### Algorithm

```python
# ml_pipeline/analytics/volley_derive.py — runs after bounce_detector + swing_type_classifier
LOOKBACK_MAX_S        = 5.0    # rally cadence ceiling
HALF_VOLLEY_MAX_DT_S  = 0.10   # 100 ms post-bounce contact threshold
HALF_VOLLEY_MAX_D_M   = 0.30   # 30 cm radial threshold
SIDE_AMBIGUITY_M      = 0.20   # bounce within 20 cm of net y → side uncertain → ignore

def derive_volleys(strokes, bounces, serves):
    for s in strokes:
        # 1. Drop out-of-rally noise
        if not within_any_rally_window(s.ts, serves):
            s.volley_flag = None; s.volley_confidence = 0.0
            continue

        # 2. Find opponent's prior stroke (lookback bounded)
        prev_opp = last_stroke_where(strokes, before=s.ts,
                                     player_side != s.side,
                                     min_ts = s.ts - LOOKBACK_MAX_S)

        # 3. Seed t0: opponent stroke ts OR serve ts (for return-of-serve)
        if prev_opp is None:
            srv = serve_immediately_before(s.ts, serves)
            if srv is None: s.volley_flag = None; continue
            t0 = srv.ts
        else:
            t0 = prev_opp.ts

        # 4. Bounces on striker's side in (t0, s.ts)
        window = [b for b in bounces if t0 < b.ts < s.ts and b.court_side == s.side]

        # 5. Half-volley check
        half = any(
            (s.ts - b.ts) <= HALF_VOLLEY_MAX_DT_S
            and dist(b.xy, s.contact_xy) <= HALF_VOLLEY_MAX_D_M
            for b in window
        )
        s.half_volley_flag = half
        s.volley_flag      = (len(window) == 0)  # half_volley is NOT a volley per USTA

        # 6. Confidence — see formula below
        s.volley_confidence = compute_conf(window, t0, s.ts, bounces, prev_opp)
```

### Side-attribution rule

A bounce's `court_side`:
- `near` if `bounce.y < net_y − SIDE_AMBIGUITY_M`
- `far`  if `bounce.y > net_y + SIDE_AMBIGUITY_M`
- `net_cord` otherwise (ignore for volley counting)

Striker's side from swing_type_classifier (NEAR/FAR only — no A/B identity needed; **ADR-03 is NOT a blocker for this analytic**).

### Confidence formula

```
volley_confidence = w_bounce × w_side × w_window

w_bounce = min(bounce.conf for bounce in window) if window else bounce_detector.recall_prior   # ~0.85 default
w_side   = 1.0 if all bounces in window classified > 0.5 m from net y
            else 1 − (count_ambiguous / count_total)
w_window = 1.0 if prev_opp found cleanly
         else 0.7 if seeded from serve
         else 0.4 if lookback hit LOOKBACK_MAX_S without finding either
```

Propagates uncertainty correctly: bounce_detector miss in the relevant window → `w_bounce` collapses → confidence drops, signalling "this volley flag may be a missed-bounce artifact."

### Output (per ADR-04 decision, 2026-05-28)

Three new columns on `ml_analysis.stroke_events` (coordinate with parallel agent who owns stroke_detector):
- `volley_flag BOOLEAN`
- `half_volley_flag BOOLEAN` — kept as separate column for SA reconciliation parity + future skill-metric wedge
- `volley_confidence FLOAT`

Existing heuristic `volley` column on `stroke_events` is kept initially for measurement comparison; deprecated once analytic v1 is proven on Match 1 + Match 2 + Match 3.

### Top 3 references
1. USTA Tennis Terms and Definitions — authoritative volley + half-volley wording.
2. ITF 2026 Rules of Tennis Rule 24 — mechanical root (two-bounce loses point).
3. Silent Impact (UIST '24) — production-system precedent for 6-class taxonomy; confirms commercial systems collapse half-volley (justifies our keeping `half_volley_flag` as optional/informational, not load-bearing).

---

## Cross-references

- [bronze_silver_18_audit.md](./bronze_silver_18_audit.md) — model-gap framing.
- [ADR-01](./adr_01_bounce_model_architecture.md) — blocks this.
- [ADR-02](./adr_02_swing_type_classifier_plan.md) — blocks this (need per-stroke side attribution).
- [ADR-05](./adr_05_detector_build_sequencing.md) — sequencing places this last.
