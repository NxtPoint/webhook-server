# Support Bot (`support_bot/`)

Customer-service chat answering FAQ-only questions on the portal. Uses Claude Haiku 4.5 with prompt caching + forced tool-use for guaranteed structured output. All authenticated (`X-Client-Key`); portal-only — no public/Wix surface.

**Status (2026-04-29)**: Backend + frontend page deployed. Both live.

**Design history**: `docs/support_bot_design.md` (the original spec — note that the frontend pivoted from a floating widget to a dedicated `/help` page styled like the AI Coach module; design doc reflects the pre-pivot widget plan).

## Flow

```
Portal page  →  POST /api/support/ask   (X-Client-Key auth)
              ├─ rate limit check (per email, 100/day hard cap)
              ├─ user-context fetch (billing.account/member/subscription_state/vw_customer_usage)
              ├─ faq_cache lookup by hash(question + page_context, faq.md sha256)
              │     hit  → log turn, return cached payload, $0 cost
              │     miss → continue
              ├─ Haiku 4.5 call:
              │     - system prompt = static instructions + FAQ (cache_control=ephemeral)
              │     - user message  = name/plan/role/credits/page + question
              │     - tool_choice   = forced answer_user tool
              ├─ persist to faq_cache (only if confidence=high AND not needs_human)
              ├─ log turn to support_bot.conversations (incl. tokens + cost_cents)
              └─ return { answer, confidence, needs_human, cited_sections, actions }
```

User-context (name, plan, role, credits) goes in the user message — NOT the cached system block — so it doesn't invalidate the cache on every call.

## Tables (all idempotent on boot via `init_support_schema()`)

**`support_bot.conversations`** — every Q+A logged (cache hits and misses both):
- `id` (uuid PK), `conversation_id` (uuid), `turn_idx` (integer)
- `email` (text NOT NULL), `page_context` (text)
- `question`, `answer`, `confidence` (`high`/`medium`/`low`), `needs_human` (bool), `cited_sections` (text[])
- `feedback` (jsonb: `{rating, comment}`), `escalated_at` (timestamptz)
- `tokens_input`, `tokens_output`, `tokens_cached`, `cost_cents` (numeric)
- Indexes: `(conversation_id, turn_idx)`, `(created_at DESC)`, `(email)`

**`support_bot.faq_cache`** — dedup of identical questions:
- `question_hash` (text PK = sha256 of `lower(question)|page_context`)
- `page_context`, `answer_payload` (jsonb), `hit_count`, `last_hit_at`
- `faq_hash` (sha256 of `faq.md` at write time — used to invalidate stale cache rows when FAQ changes)
- Cache TTL = until faq.md changes (and only confidence=high entries are cached)

## Endpoints (`support_bot/support_api.py`)

All require `X-Client-Key` header.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/support/ask` | Main entry. Body: `{message, email, page_context?, conversation_id?}`. Returns structured answer. |
| POST | `/api/support/feedback` | Body: `{turn_id, rating: 'up'\|'down', comment?}`. Updates `feedback` jsonb on the turn. |
| POST | `/api/support/escalate` | Body: `{conversation_id, email, user_note?}`. Sends transcript to `info@ten-fifty5.com` via SES, stamps `escalated_at`. |
| GET | `/api/support/health` | **Admin-only** (`email` param must be in `ADMIN_EMAILS`). Returns FAQ hash, total chars, conversation counts (24h/7d), thumbs ratio, cost. |

Hard kill switch: env var `SUPPORT_BOT_ENABLED=false` returns a stock "we're temporarily unavailable, please email" reply with zero LLM cost.

## Key files

| File | Purpose |
|---|---|
| `support_bot/faq.md` | **The knowledge base.** Plain markdown with `## section.id` headers. Bot ONLY answers from this file. Currently seeded with 5 example entries; real ~30 to be written. |
| `support_bot/faq_loader.py` | Reads `faq.md` once at import; exposes `FAQ_TEXT`, `FAQ_HASH`, `FAQ_LOADED_AT`. Reload by service restart (no hot-reload). |
| `support_bot/db.py` | Schema DDL + cache/conversation helpers. `init_support_schema()`, `cache_get/put`, `log_turn`, `record_feedback`, `mark_escalated`, `fetch_transcript`, `count_daily`, `health_metrics`. |
| `support_bot/rate_limiter.py` | Per-email daily cap (30 soft / 100 hard). Fail-open if count query errors. |
| `support_bot/prompt_builder.py` | `build_system_prompt()` (static instructions + FAQ block), `build_user_message()` (per-call user context), `ANSWER_TOOL` schema. |
| `support_bot/haiku_client.py` | Anthropic SDK wrapper. Model: `claude-haiku-4-5-20251001`. Temperature 0.3, max_tokens 400. Forces `tool_choice` for structured output. Prompt-cache aware. Returns `{ok, tool_input, tokens_*, cost_cents}`. |
| `support_bot/email_sender.py` | SES escalation transcript → `info@ten-fifty5.com` with `Reply-To` set to the customer. Mirrors `coach_invite/email_sender.py` styling. |
| `support_bot/init.py` | `init_support_bot()` — schema init + FAQ-loaded check, called from `upload_app.py` on boot. |
| `support_bot/support_api.py` | Flask blueprint `support_bp`. Auth check, four endpoints listed above, `_fetch_user_context()` for billing-table lookup with the `subscription_state`-existence guard pattern. |

## Boot wiring

In `upload_app.py`, registered after `tennis_coach` (same try/except pattern so a failure can't kill the service):

```python
try:
    from support_bot.init import init_support_bot
    from support_bot.support_api import support_bp
    init_support_bot()
    app.register_blueprint(support_bp)
except Exception:
    app.logger.exception("support_bot init failed on boot")
```

## Frontend

**Dedicated page**: `frontend/support.html`, served at `/help` by both `locker_room_app.py` (canonical) and `upload_app.py` (same-origin backup). Loaded inside the portal iframe via the standard `navigateTo()` flow — same auth handoff (`?email=&firstName=&key=&api=` URL params populated by `authParams()` in portal.html) that every other portal page uses. No floating widget, no postMessage hack, no separate static asset.

The page mirrors the AI Coach module visually:
- Greeting card with first-name personalisation + 4 quick-prompt buttons
- Input row with green Send button (mirrors `.coach-input` / `.coach-quick-btn`)
- Conversation log: user-message bubbles + bot answer cards with green left-border callouts (`.support-answer` mirrors `.coach-answer`)
- `[section.id]` citations rendered as green pills (`.support-pill` mirrors `.coach-pill`)
- Inline action buttons for `actions[]` from API
- Thumbs up/down per turn (local state only in v1 — see Iteration loop below for server-feedback upgrade path)
- Amber "This didn't help — email us" CTA appears when `needs_human=true` or `confidence=low`; calls `/api/support/escalate` and shows a toast

**Sidebar entry**: portal.html has a "Help & Support" nav item under "Plans & Pricing" with `path: '/help'` — navigates like every other item, no special-case JS.

## Cost model

Claude Haiku 4.5 pricing per million tokens: input uncached $1, input cached-read $0.10, input cache-write $1.25, output $5.

| Scenario | Tokens | Cost |
|---|---|---|
| First call in 5-min window (cache write) | ~5,800 cache-write + 110 input + 180 output | ~$0.0083 |
| Subsequent (cache hit) | ~5,800 cached + 110 input + 180 output | ~$0.0019 |
| `faq_cache` dedup hit | 0 | $0 |

Realistic monthly spend at 30 q/day with 30% dedup: ~$0.75. At 500 q/day with 40% dedup: ~$9. An order of magnitude cheaper per call than the AI Coach (which uses Sonnet).

## Anti-hallucination guardrails

1. Hard rule in system prompt: ONLY answer from FAQ; out-of-scope → `confidence=low`, `needs_human=true`, redirect to email.
2. Account-specific rule: any "my match", "my refund", "my account" question forces `needs_human=true` even if technically answerable.
3. Tool-use forcing: model cannot return free-form prose. Must call `answer_user` with a strict JSON schema.
4. `cited_sections` field — answers that cite no FAQ section are flaggable in the conversation log for review.
5. Cache only stores `confidence=high` entries — speculative answers re-generate every time.
6. Temperature 0.3 — minor phrasing variation, no creative drift.
7. AI-Coach redirect rule: questions about a user's match data ("how can I improve my serve") get redirected to the AI Coach inside Match Analysis.

## Smoke-test (Render shell, post-deploy)

See `docs/support_bot_design.md` §10/§11 for full curl commands. Quick check:

```bash
# Health
curl -s -H "X-Client-Key: $CLIENT_API_KEY" \
  "https://api.nextpointtennis.com/api/support/health?email=info@ten-fifty5.com" | jq

# Ask
curl -s -X POST -H "X-Client-Key: $CLIENT_API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"How do I cancel my subscription?","email":"info@ten-fifty5.com"}' \
  "https://api.nextpointtennis.com/api/support/ask" | jq
```

## Required env vars

All already configured for the main API:
- `ANTHROPIC_API_KEY` — Claude API key
- `CLIENT_API_KEY` — `X-Client-Key` value
- `SES_FROM_EMAIL`, `AWS_REGION`, AWS keys — for escalation email
- `SUPPORT_BOT_ENABLED` — optional kill switch (default `true`)

## Iteration loop

Real conversations are the single best signal for what to add to the FAQ. Weekly review path:

1. `SELECT question, COUNT(*) FROM support_bot.conversations WHERE feedback->>'rating'='down' GROUP BY question ORDER BY 2 DESC` — top thumbs-down patterns.
2. `SELECT question FROM support_bot.conversations WHERE escalated_at IS NOT NULL ORDER BY created_at DESC` — what got escalated.
3. Add FAQ entries to `faq.md` covering those gaps. Push. The `faq_hash` change auto-invalidates stale cache rows.

No machine learning involved — the bot is only as good as the FAQ.
