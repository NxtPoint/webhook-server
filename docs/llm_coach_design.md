# LLM Tennis Coach — Feature Spec

**Status**: Design approved. Implementation pending completion of `ss_*` presentation layer.

**Author**: Background design agent, 2026-04-11

---

## 1. User Experience

**Entry point: a "Coach" tab on the match-analysis page, not a separate page.**

Rationale: the user is already looking at their match data when they want coaching insight. Pulling them to a new page breaks context. The coach tab sits alongside "Match Summary", "Serve Detail", "Return Detail", "Rally Detail" in the existing `renderAnalyticsModule()` tab strip. Portal sidebar gets no new item — this is a capability within Match Analysis, not a top-level destination.

**What the user sees when they open the Coach tab:**

```
┌──────────────────────────────────────────────────────────┐
│  MATCH ANALYTICS  |  PLACEMENT HEATMAPS                  │
├──────────────────────────────────────────────────────────┤
│  Summary  Serve  Return  Rally  [Coach ✦]                │
├──────────────────────────────────────────────────────────┤
│  Quick Analysis cards (pre-generated, appear instantly)  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐      │
│  │ Your Serve   │ │ Key Weakness │ │ Tactical Tip │      │
│  └──────────────┘ └──────────────┘ └──────────────┘      │
│                                                          │
│  Ask the Coach ─────────────────────────────────────     │
│  ┌────────────────────────────────────────────────┐      │
│  │ [text input]                    [Send ➤]       │      │
│  └────────────────────────────────────────────────┘      │
│  Quick prompts:                                          │
│  [Analyze my serve] [Biggest weakness] [Adjust tactics]  │
│                                                          │
│  ── Coach response appears below ──                      │
│  [Response with cited stats in green callout badges]     │
└──────────────────────────────────────────────────────────┘
```

**Pre-generated insight cards**: Generated automatically the first time the user opens the Coach tab for a given match. Stored in the cache. Cards are fast because they use cached responses.

**Trustworthiness — cited stats**: The system prompt instructs Claude to cite specific numbers in brackets, e.g. "Your first serve percentage (54%) is below where you want it...". The frontend renders these bracketed numbers as small green pill badges that, when hovered, show the SQL view that produced them. This is achieved by having the backend return both the prose response AND the `data_snapshot` that was passed to Claude — the frontend can cross-reference.

**Quick prompts:**
- "Analyze my serve performance"
- "What's my biggest weakness in this match?"
- "How should I adjust my tactics next time?"
- "Walk me through a key turning point"

---

## 2. Backend Architecture

**Blueprint**: New file `tennis_coach/coach_api.py`, registered as `coach_bp`. Same pattern as `client_api.py`: `X-Client-Key` auth, `email` query param.

**File structure:**
```
tennis_coach/
  __init__.py
  coach_api.py        ← Flask blueprint, routes
  db.py               ← schema creation, cache read/write, rate limit check
  data_fetcher.py     ← assembles ss_.* view data into structured dict
  prompt_builder.py   ← prompt templates, system prompt, data formatting
  claude_client.py    ← thin wrapper around Anthropic SDK
  rate_limiter.py     ← rate limit logic
```

**Endpoints:**

### `POST /api/client/coach/analyze`
- Auth: `X-Client-Key` header
- Body: `{ task_id, email, prompt_key: "serve_analysis"|"weakness"|"tactics"|"freeform", freeform_text? }`
- Flow: rate limit → cache check → fetch `ss_.*` views → call Claude → store in cache → return
- Response: `{ ok, response, data_snapshot, cached, tokens_used }`

### `GET /api/client/coach/cards/<task_id>?email=`
- Returns the three pre-generated insight cards
- If not yet generated, triggers async generation, returns `{ ok, status: "generating" }`
- Response when ready: `{ ok, cards: [{ title, body, category }] }`

### `GET /api/client/coach/status/<task_id>?email=`
- Lightweight poll endpoint for card generation status
- Returns `{ ready: true/false }`

**Rate limiting** (`tennis_coach/rate_limiter.py`):
- Per (email, task_id) per day: max 5 freeform calls (cards don't count)
- Per email per day across all matches: max 20 total
- Implemented as DB count query on `coach_cache`
- On limit hit: `429 { error: "daily_limit_reached", resets_at }`

**Caching** — new table `tennis_coach.coach_cache`:
```sql
CREATE TABLE IF NOT EXISTS tennis_coach.coach_cache (
    id            serial PRIMARY KEY,
    task_id       uuid NOT NULL,
    email         text NOT NULL,
    prompt_key    text NOT NULL,  -- 'serve_analysis', 'weakness', 'tactics', 'cards', 'freeform:<sha256>'
    response      text NOT NULL,
    data_snapshot jsonb,
    tokens_used   integer,
    created_at    timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS coach_cache_uq
    ON tennis_coach.coach_cache (task_id, email, prompt_key);
```

- Cache TTL: **indefinite** — match data doesn't change after ingest
- "Regenerate" link → `?force=true` skips cache
- Freeform key: `freeform:` + first 12 chars of SHA-256 of lowercased stripped text

---

## 3. Prompt Engineering

**System prompt (fixed):**

```
You are a professional tennis coach with 20 years of experience coaching players
from club level to ATP/WTA circuit. You analyse match data with the precision of
a tour-level analyst and the directness of a coach who respects their player's
time.

Your job is to give actionable, specific feedback grounded in match statistics.
You do not give generic encouragement. You do not speculate about things you
cannot see in the data. When you cite a number, it comes from the match
statistics provided — never from memory or assumption.

Rules:
- Every coaching point must reference at least one specific statistic from the
  data provided, cited in brackets: e.g. "Your first serve percentage [54%] is
  below where you want it"
- Maximum 3 coaching points per response, each under 60 words
- Use direct language: "Your backhand is leaking errors" not "There may be room
  for improvement on your backhand side"
- If the data does not support a conclusion, say so explicitly: "The data
  doesn't give me enough serve placement detail to comment on T vs Wide
  selection"
- Never fabricate statistics. If a field is null or missing, ignore that
  dimension
- Close with one concrete drill or focus point for the next session
```

**Data format** — compact JSON, never raw silver rows:

```json
{
  "match": {
    "date": "2026-04-08", "location": "Club Beograd",
    "score": "6-4, 3-6, 6-3",
    "player_a": "Tomo", "player_b": "Nikola"
  },
  "summary": {
    "total_points": 142,
    "points_won_pct": { "a": 54, "b": 46 },
    "aces": { "a": 4, "b": 1 },
    "double_faults": { "a": 3, "b": 7 },
    "winners": { "a": 18, "b": 22 },
    "unforced_errors": { "a": 24, "b": 19 },
    "avg_rally_length": 4.2,
    "max_rally": 19
  },
  "serve": {
    "first_serve_pct": { "a": 61, "b": 54 },
    "first_serve_pts_won_pct": { "a": 72, "b": 68 },
    "second_serve_pts_won_pct": { "a": 51, "b": 44 },
    "serve_speed_avg_kmh": { "a": 168, "b": 154 },
    "direction_pct": { "a": { "wide": 38, "body": 22, "T": 40 }, "b": {...} },
    "direction_win_pct": { "a": { "wide": 68, "body": 74, "T": 77 }, "b": {...} }
  },
  "rally": {
    "rally_pts_won_pct": { "a": 48, "b": 52 },
    "stroke_split": { "a": { "forehand": 58, "backhand": 42 }, "b": {...} },
    "aggression_pct": { "a": { "attack": 28, "neutral": 54, "defence": 18 }, "b": {...} },
    "depth_pct": { "a": { "deep": 41, "middle": 38, "short": 21 }, "b": {...} },
    "errors_by_stroke": { "a": { "forehand": 8, "backhand": 16 }, "b": {...} }
  },
  "pressure": {
    "break_points_faced": { "a": 11, "b": 8 },
    "break_points_saved_pct": { "a": 55, "b": 75 },
    "break_points_converted_pct": { "a": 50, "b": 63 }
  }
}
```

**Anti-hallucination techniques:**

1. **Data is pre-aggregated** — Claude sees numbers, not raw rows. No arithmetic needed by the model.
2. **Null suppression** — `data_fetcher.py` omits any field where source is NULL/0. Claude cannot cite a stat it was never shown.
3. **Explicit instruction** — system prompt says "if a field is null or missing, ignore that dimension."
4. **Frontend validation** — `data_snapshot` returned alongside response so UI can cross-reference cited numbers.
5. **Temperature: 0.3** — low/deterministic, some variation in phrasing but not creative.
6. **No tools / function calling** — plain text output with strict format. Simpler = fewer failure modes.
7. **Count threshold** — `data_fetcher.py` suppresses any dimension where shot count < 5 (not statistically meaningful).

**Length control**: system prompt caps at 3 points × 60 words. Natural ~200-300 words. Hard `max_tokens: 600`.

**Tone**: direct, analytical, peer-level. Not motivational.

---

## 4. New `ss_.*` Views for Coach

Four pre-aggregated views. All read from `silver.point_detail` joined with `ss_.vw_player` for player-role resolution.

### `ss_.coach_match_summary`
One row per task_id. Top-level KPIs for both players.

**Columns:**
```
task_id, player_a_name, player_b_name, match_date, location, score_display,
total_points, pa_points_won, pb_points_won,
pa_aces, pb_aces, pa_dfs, pb_dfs,
pa_winners, pb_winners, pa_errors, pb_errors,
pa_fs_pct, pb_fs_pct,
pa_fs_pts_won_pct, pb_fs_pts_won_pct,
pa_ss_pts_won_pct, pb_ss_pts_won_pct,
pa_return_pts_won_pct, pb_return_pts_won_pct,
pa_rally_pts_won_pct, pb_rally_pts_won_pct,
avg_rally_length, max_rally_length,
pa_serve_speed_avg, pb_serve_speed_avg,
pa_serve_speed_max, pb_serve_speed_max
```

### `ss_.coach_serve_patterns`
One row per (task_id, player_id, serve_side, serve_bucket).

**Columns:**
```
task_id, player_id, serve_side_d, serve_bucket_d,
serve_count, serve_in_count, serve_in_pct,
pts_won, pts_won_pct
```

### `ss_.coach_rally_patterns`
One row per (task_id, player_id, stroke_d, depth_d, aggression_d).

**Columns:**
```
task_id, player_id, stroke_d, depth_d, aggression_d,
shot_count, error_count, error_pct,
winner_count, winner_pct
```

### `ss_.coach_pressure_points`
One row per (task_id, player_id).

**Columns:**
```
task_id, player_id,
bp_faced, bp_saved, bp_saved_pct,
bp_opportunities, bp_converted, bp_converted_pct,
game_pts_won_pct, set_pts_won_pct
```

---

## 5. Prompt Templates

### Template 1 — Serve Analysis
- **SQL**: `coach_match_summary` (1 row) + `coach_serve_patterns` (all rows for player_a)
- **Response shape**: overall serve verdict → strongest direction → weakest direction → drill

### Template 2 — Biggest Weakness
- **SQL**: `coach_match_summary` + `coach_rally_patterns` + `coach_pressure_points` (player_a)
- **Response shape**: highest error-rate pattern → secondary observation → corrective drill

### Template 3 — Tactical Adjustment
- **SQL**: All four views for both players
- **Response shape**: opponent's primary weakness → tactical pattern to exploit → what to avoid

### Template 4 — Pre-generated Cards (background, runs on first Coach tab open)
- **SQL**: All four coach views for player_a
- **Output**: JSON array of 3 cards `{ title, body, category }`. Parsed by backend, stored in cache.

---

## 6. Cost Estimate

| Component | Tokens |
|---|---|
| System prompt | ~280 |
| Data payload | ~600-800 |
| User question | ~20-40 |
| Response | ~250-400 |
| **Total per call** | **~1,200-1,500** |

**Pricing (Claude Sonnet 4.6):**
- Input: $3/1M → ~$0.0042
- Output: $15/1M → ~$0.0052
- **~$0.01 per call**

**Per user**: 5 freeform + 3 cards = max 8 calls per match session → ~$0.08/session

**Monthly** (100 active users × 2 matches): ~$16/month

**Cost controls:**
- Cards cached forever per match
- Freeform cached by question SHA
- Daily limit 5 freeform/match/user
- Admin KPI report: `coach_calls_7d`, `coach_cost_7d_usd`

---

## 7. Risks and First Test

**Risks:**

1. **Silver data quality → hallucination risk.** If `shot_outcome_d` or `serve_bucket_d` have gaps, aggregated views mislead. Mitigation: `data_fetcher.py` suppresses fields with count < 5.

2. **Double-fault detection reliability.** Complex logic in silver v2. If wrong, trust-damaging failure ("Your 7 DFs cost you the set" when real is 3). **Validate DF counts before enabling coach.**

3. **Player identity (A vs B confusion).** `first_server` S/R drives mapping. If wrong, coach advises the wrong player. Use same resolution logic as frontend `resolvePlayerIds()`.

4. **Rate limit bypass.** Same trust model as rest of client API. Not critical at current scale.

5. **Latency on first open.** Cold card generation ~3-5s. Needs loading state, not blank panel.

**Hardest part**: `ss_.coach_serve_patterns` — correctly joining serve shots to point outcomes. Requires careful testing against known match results.

**First test**: Run serve analysis prompt against one known match. Manually verify every stat Claude cites matches the dashboard. Any number >2% off = view bug.

---

## 8. Implementation Order

1. **Data layer** — `tennis_coach/db.py`, `data_fetcher.py`, the four `ss_.*` views. Debug endpoint to inspect raw payload.
2. **Claude integration** — `claude_client.py`, `prompt_builder.py`, three prompt templates. curl-test, verify citations match payload.
3. **Caching + rate limiting** — `coach_cache` table, reads/writes, daily limit check.
4. **Frontend Coach tab** — add to tab strip, cards UI, quick prompts, freeform input, green-badge citation rendering, polling for card status.
5. **Cards auto-generation** — Template 4, `GET /cards/` with poll pattern.
6. **Portal badge (deferred)** — surface the feature in the sidebar once usage patterns prove it out.
