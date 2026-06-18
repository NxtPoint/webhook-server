# support_bot

> Customer-service chatbot for the portal. Claude Haiku with prompt caching + forced tool-use, FAQ-only, escalates anything not covered to `info@ten-fifty5.com`.

**Canonical implementation reference:** [`../docs/business/features.md`](../docs/business/features.md) (Support Bot section). This README is the file-level orientation; the business doc covers backend architecture, cost model, guardrails, and iteration loop in depth.

## What this owns

- The `support_bot.*` schema (`conversations`, `faq_cache`) — idempotent boot setup in `db.py::init_support_schema()`
- `support_bp` Flask blueprint registered on `upload_app`: `/api/support/ask`, `/feedback`, `/escalate`, `/health`
- The Haiku prompt builder + answer-tool schema (forced tool-use → guaranteed structured output)
- Per-email rate limiter, sha256-keyed FAQ cache, escalation SES email

## What this is NOT

- **Not a general LLM chat.** Hard FAQ-only — the system prompt + user message contain the entire FAQ; the bot answers ONLY from that text.
- **Not the AI Coach.** That's `tennis_coach/` — match-data-driven coaching, Sonnet, paid-only. Support bot is FAQ over support content, free, all users.
- **Not the FAQ author.** `faq.md` is the load-bearing artefact and is hand-written by Tomo + co-worker. The bot is a thin shell over it.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Package init — short purpose docstring |
| `init.py` | Boot-time schema setup (called from `upload_app.py` on import) |
| `db.py` | `init_support_schema()`, `cache_get/put`, `log_turn`, `record_feedback`, `fetch_transcript`, `mark_escalated`, `health_metrics`, `question_hash` |
| `faq.md` | **THE knowledge base.** Bot answers ONLY from this file. Currently 5 seed entries; ~30 real entries to be authored. |
| `faq_loader.py` | Reads `faq.md` on import, computes `FAQ_HASH` (sha256), exposes `FAQ_TEXT` + `FAQ_LOADED_AT` |
| `prompt_builder.py` | Builds system prompt (FAQ-only rule + FAQ text), user message, and `ANSWER_TOOL` schema (forced tool-use) |
| `haiku_client.py` | Thin Anthropic SDK wrapper — Haiku 4.5, prompt caching, returns parsed tool input + token usage |
| `rate_limiter.py` | Per-email rate limit (`HARD_LIMIT` per day) — checks `support_bot.conversations` history |
| `email_sender.py` | `send_escalation()` — SES email of full transcript to `info@ten-fifty5.com`, Reply-To = customer |
| `support_api.py` | `support_bp` Flask blueprint — `/ask`, `/feedback`, `/escalate`, `/health` |

## Entry points

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/support/ask` | `X-Client-Key` | Main entry. Body: `{message, email, page_context?, conversation_id?}`. Returns `{answer, confidence, needs_human, cited_sections, actions, conversation_id, turn_idx}`. |
| `POST /api/support/feedback` | `X-Client-Key` | Thumbs up/down on a turn. Body: `{turn_id, rating: 'up'\|'down', comment?}` |
| `POST /api/support/escalate` | `X-Client-Key` | Email transcript to support. Body: `{conversation_id, email, user_note?}` |
| `GET /api/support/health` | `X-Client-Key` + admin email | FAQ hash, conversation counts, cost metrics |

Plus `frontend/support.html` served at `GET /help` (by both `locker_room_app.py` and `upload_app.py` as same-origin backup). Reached via the **Help & Support** sidebar item in the portal.

## Flow — the `/ask` happy path

```
Frontend POSTs {message, email, conversation_id?, page_context?}
        │
        ├─ kill switch:  SUPPORT_BOT_ENABLED=false → return canned escalate message
        ├─ rate limit:   check_rate_limit(email)   → 429 if over
        ├─ assign turn_idx (next index for this conversation_id)
        │
        ├─ _fetch_user_context(email) — pulls first_name, plan, role, credits_remaining
        │     (uses information_schema.tables to safe-check subscription_state)
        │
        ├─ qhash = sha256(message + page_context)
        ├─ cache_get(qhash, FAQ_HASH)
        │     │
        │     └─ HIT  → log_turn (zero tokens) → return cached answer
        │
        ├─ FAQ_TEXT empty? → hard escalate (no LLM call)
        │
        ├─ build system prompt (FAQ-only rule + full FAQ text)
        ├─ build user message (question + user context + page_context)
        ├─ call_haiku(system, user, ANSWER_TOOL)  — forced tool-use
        │     │
        │     ├─ FAILURE → log failed turn, return canned escalate message
        │     └─ SUCCESS → tool_input = {answer, confidence, needs_human, cited_sections, actions}
        │
        ├─ if confidence='high' AND not needs_human:  cache_put(qhash, payload, FAQ_HASH)
        ├─ log_turn(... tokens_input, tokens_output, tokens_cached, cost_cents)
        │
        └─ return {answer, confidence, needs_human, cited_sections, actions, conversation_id, turn_idx}
```

## Escalation — when the bot pushes to a human

The bot escalates (sets `needs_human=true` in its output, frontend shows the amber escalate CTA) when:

1. **FAQ empty** — `faq.md` returned no content. Hard-escalate without LLM call (`support_api.py:235-248`).
2. **LLM call fails** — Anthropic API error. Fail-safe: log the failed turn, return canned message (`support_api.py:262-291`).
3. **Account-specific question** — Haiku detects "what's MY remaining credits / why was I charged" and returns `needs_human=true` from the tool (`support_api.py:296-298`). FAQ-coverage is irrelevant; account-specific always escalates.
4. **Low confidence** — Haiku returns `confidence='low'` regardless of answer. Cache write is skipped; user sees the escalate CTA.

The `POST /escalate` endpoint then SES-emails the full transcript to `info@ten-fifty5.com` with Reply-To set to the customer's email.

## Gotchas

- **`faq.md` is the entire feature.** The bot's quality scales 1:1 with FAQ quality. Currently seeded with ~20 entries (5 originals + 15 drafted from documented platform behaviour). Edit or replace as real inbound questions arrive.
- **FAQ_HASH is a cache invalidator.** When `faq.md` content changes, its sha256 hash changes, and stale cache entries are no longer matched (`cache_get(qhash, FAQ_HASH)` keys on both). No manual cache flush needed.
- **Forced tool-use guarantees structure.** Haiku is forced to call the `ANSWER_TOOL`, which has a strict JSON schema. No prose-parsing required.
- **Prompt caching cuts costs ~10×.** The system prompt (FAQ included) is cache-marked. Cached queries cost ~$0.001; cache writes cost ~$0.008. Realistic monthly spend at portal volumes is < $5.
- **Account-specific escalation is in the prompt, not the code.** The system prompt instructs Haiku to set `needs_human=true` for any question that needs the user's actual data. We don't gate by question content — Haiku decides.
- **`information_schema.tables` check before subscription lookup.** `_fetch_user_context` checks the `billing.subscription_state` table exists before SELECTing. Otherwise a missing table on a fresh DB poisons the whole transaction. See memory `feedback_postgres_missing_table.md`.
- **Kill switch.** `SUPPORT_BOT_ENABLED=false` short-circuits `/ask` with a canned escalate message. Use this if cost spikes.
- **Cache writes only on `confidence='high'`.** Speculative answers don't pollute the cache (`support_api.py:309-310`).

## Required environment variables

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key |
| `CLIENT_API_KEY` | Same key as `client_api.py` |
| `SES_FROM_EMAIL` | Escalation email From address |
| `AWS_REGION` | SES region |
| `SUPPORT_BOT_ENABLED` | `false` to disable the bot (default true) |

## See also

- [`../docs/business/features.md`](../docs/business/features.md) (Support Bot section) — **canonical implementation reference**
- [`../docs/business/_archive/support-bot-design.md`](../docs/business/_archive/support-bot-design.md) — original design doc (historical; §2 UX section is stale, backend rationale still useful)
- [`../docs/business/README.md`](../docs/business/README.md) §5 block-reason cascade — how the bot routes account-specific questions
