# tennis_coach

> AI Coach for match analysis. Claude Sonnet over compact match-data payloads built from gold views. Returns coaching cards (pre-generated) and freeform answers (rate-limited). Paid plans only — business rules are canonical in [`../docs/business/README.md`](../docs/business/README.md) §5 (don't duplicate them here; this README is the code map).

## What this owns

- The `tennis_coach.coach_cache` table (one row per `(task_id, email, prompt_key)`)
- The `gold.coach_*` views (currently `coach_rally_patterns` + a stubbed `coach_pressure_points`)
- The `coach_bp` Flask blueprint at `/api/client/coach/*`
- The Claude Sonnet client + 3 named prompt templates + 1 cards prompt + 1 freeform builder
- The per-day rate limiter (5 freeform calls per (email, task_id), 20 per email overall)
- The AI Coach paywall check (`_check_ai_coach_entitled`)
- The data router (`_fetch_data_for_task`) that picks match-fetcher vs technique-fetcher by `sport_type`

## What this is NOT

- **Not the Support Bot.** That's `support_bot/` — Haiku, FAQ-only, free, all users.
- **Not a streaming chat.** Cards are generated synchronously and cached indefinitely; freeform answers are also cached on first call. No SSE, no websockets.
- **Not a billing actor.** Calling AI Coach does not consume a match credit. Only the upload itself consumes. AI Coach is a feature that's *included* in paid plans, not metered separately.
- **Not the technique data fetcher.** `coach_data_fetcher.fetch_technique_data` lives in `technique/`, called from this module via `_fetch_data_for_task` when `sport_type == 'technique_analysis'`.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Package marker |
| `init.py` | Boot-time setup: `init_coach_cache()` + `init_coach_views()` (called from `upload_app.py`) |
| `db.py` | `init_coach_cache()`, `cache_get/put`, `count_daily_calls`, `freeform_key()` (sha256 hash) |
| `coach_views.py` | `init_coach_views()` — DROP+CREATE the `gold.coach_*` views |
| `data_fetcher.py` | `fetch_match_data(task_id)` — assembles compact dict from `gold.match_kpi` + `gold.match_serve_breakdown` + `gold.match_rally_breakdown` + `gold.match_return_breakdown` + `gold.coach_rally_patterns`. Drops dimensions with `shot_count < MIN_SAMPLE` (5) to prevent hallucination on thin samples. |
| `prompt_builder.py` | `SYSTEM_PROMPT` + `CARDS_SYSTEM_PROMPT`. Builders: `build_serve_analysis_prompt`, `build_weakness_prompt`, `build_tactics_prompt`, `build_freeform_prompt`, `build_cards_prompt`. |
| `claude_client.py` | `call_claude(messages, system, max_tokens=600)` — Sonnet 4.6, T=0.3, returns structured `{ok, text, input_tokens, output_tokens}` |
| `rate_limiter.py` | `check_rate_limit(email, task_id)` — checks per-match (5/day) and per-email (20/day) limits via `count_daily_calls`. Cards are excluded. |
| `coach_api.py` | `coach_bp` Flask blueprint with the 4 endpoints |

## Entry points

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/client/coach/analyze` | `X-Client-Key` + ownership + paywall | Main coaching call. Body: `{task_id, email, prompt_key, freeform_text?, force?}` |
| `GET /api/client/coach/cards/<task_id>?email=...` | `X-Client-Key` + ownership + paywall | Pre-generated insight cards. Generates synchronously on first call, then cached. |
| `GET /api/client/coach/status/<task_id>?email=...` | `X-Client-Key` + ownership | Lightweight poll: `{ready: bool}` based on cache presence |
| `GET /api/client/coach/debug/<task_id>?email=...` | `X-Client-Key` + admin email | Returns the raw data payload that would be sent to Claude (no LLM call) |

`prompt_key` values for `/analyze`: `'serve_analysis' | 'weakness' | 'tactics' | 'freeform'`. Cards aren't reachable here — use `/cards/<task_id>`.

## Flow — `/analyze`

```
POST /api/client/coach/analyze
        │
        ├─ _guard()                  — X-Client-Key
        ├─ validate body
        ├─ _verify_ownership(task_id, email)  — must match bronze.submission_context
        ├─ _check_ai_coach_entitled(email)
        │     ├─ admin email → allow
        │     ├─ role='coach' → allow
        │     ├─ paid_active → allow
        │     └─ else → 402 UPGRADE_REQUIRED
        │
        ├─ check_rate_limit(email, task_id) → 429 if over
        │
        ├─ cache_key = (prompt_key) or freeform_key(text)
        ├─ cache_get(task_id, email, cache_key)
        │     │
        │     └─ HIT → return cached
        │
        ├─ _fetch_data_for_task(task_id)
        │     ├─ sport_type='technique_analysis' → technique.coach_data_fetcher.fetch_technique_data
        │     └─ else → tennis_coach.data_fetcher.fetch_match_data
        │
        ├─ build_<prompt>_prompt(match_data) → (messages, system)
        ├─ call_claude(messages, system) → {ok, text, input_tokens, output_tokens}
        │
        ├─ cache_put(task_id, email, cache_key, response, data_snapshot, tokens)
        │
        └─ return {ok, response, data_snapshot, tokens_used, cached: false}
```

## Data model

`tennis_coach.coach_cache` — created idempotently by `init_coach_cache()`:

| Column | Notes |
|---|---|
| `id` | PK |
| `task_id` | UUID — FK-style to `bronze.submission_context.task_id` (no DB constraint) |
| `email` | Owner email; cache is per-email so freeform questions don't leak across coaches |
| `prompt_key` | `'cards' \| 'serve_analysis' \| 'weakness' \| 'tactics' \| 'freeform:<sha256-12>'` |
| `response` | Claude's raw text response |
| `data_snapshot` | JSONB of the data payload sent to Claude (audit trail) |
| `tokens_used` | input + output tokens |
| `created_at` | `now()` default |

Unique index: `(task_id, email, prompt_key)`. Re-asking the same freeform question (after sha256 normalisation) returns the cached answer.

## Gold views

| View | Purpose |
|---|---|
| `gold.coach_rally_patterns` | Per `(task_id, player_id, stroke_d, depth_d, aggression_d)`: shot count, error count, winner count, error_pct, winner_pct. Feeds weakness + tactics prompts. |
| `gold.coach_pressure_points` | **STUB** — returns zero rows. Break-point detection needs game-score progression, which silver doesn't store. Flagged in `coach_views.py:13-16`. |

The Sonnet prompts also read from existing `gold.match_*` views directly via `data_fetcher.py`. There are no per-prompt views; `data_fetcher` shapes the JSON in Python.

## Anti-hallucination guardrails

- **`MIN_SAMPLE = 5`** in `data_fetcher.py`. Any rally-pattern dimension with fewer than 5 shots is dropped from the payload. Claude can't cite stats it doesn't see.
- **`SYSTEM_PROMPT` rules**: every coaching point must cite a stat in brackets `[54%]`. Maximum 3 points per response. Each ≤ 60 words. Refuse to fabricate; explicitly say "data doesn't give me enough detail" if a dimension is missing.
- **"Player A only" rule**: the system prompt forbids analysing the opponent. If the user asks "how do I beat them", Claude redirects to "what *you* can control".
- **Cards JSON schema enforcement**: `_parse_cards()` parses the response, validates each card has `title` + `body`, and falls back to a single-card "Coach Insight" envelope if parsing fails.

## Gotchas

- **Cache is indefinite, by design.** Match data doesn't change after ingest. Re-running the same prompt for the same `(task_id, email)` always returns the cached answer unless `force=true`.
- **Freeform key uses sha256 of normalised question.** `freeform_key("How do I improve my SERVE? ".strip().lower())` always hashes to the same value. Two slightly different freeform phrasings cache separately — that's intentional (the answers will differ).
- **Cards are not rate-limited.** Pre-generated, cached, free. Only the *first* call per match generates and is therefore the only one that pays a Claude call.
- **Admin bypass on AI Coach paywall.** `info@ten-fifty5.com`, `tomo.stojakovic@gmail.com` always allowed (`coach_api.py:67-68, 123-124`). Mirrors the rule in `client_api.py`.
- **`_fetch_data_for_task` lazy-imports technique fetcher.** Avoids circular import on boot. The import only happens when sport_type matches.
- **Rate limit fails open.** If `count_daily_calls` errors, the call is allowed through (`rate_limiter.py:49-50`). Better to over-serve than block legit usage on a transient DB hiccup.
- **`gold.coach_pressure_points` exists but is a stub.** Don't use it. If you need pressure-point analysis, score progression must first land in silver.

## Drift watch

[`../docs/business/_archive/llm-coach-design.md`](../docs/business/_archive/llm-coach-design.md) is the **original design spec** and references `ss_.*` views that no longer exist. Production uses `gold.coach_*` (this module's views) plus existing `gold.match_*`. **Treat `coach_views.py` and `data_fetcher.py` as source of truth**, not the design doc.

## Required environment variables

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Sonnet 4.6 access |
| `CLIENT_API_KEY` | Same key as `client_api.py` |

## See also

- [`../docs/business/README.md`](../docs/business/README.md) §5 — AI Coach paywall + rate-limit rules
- [`../docs/business/pricing-and-packages.md`](../docs/business/pricing-and-packages.md) §7 — AI Coach access matrix and the "this is the differentiator" positioning
- [`../docs/business/_archive/llm-coach-design.md`](../docs/business/_archive/llm-coach-design.md) — original design spec (drifted; see drift watch above)
- `support_bot/` — separate module, FAQ-only Haiku bot for support questions
- `technique/coach_data_fetcher.py` — technique-specific data assembly
- [`../CLAUDE.md`](../CLAUDE.md) §Dashboards & Gold Views — broader gold-view catalogue
