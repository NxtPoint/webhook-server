# Support Bot — Feature Spec

**Status**: Design draft, pending review.
**Author**: 2026-04-27
**Sibling spec**: `docs/llm_coach_design.md` — many patterns mirror that doc deliberately.

---

## 1. Goal & Non-Goals

**Goal**: Deflect 60–80% of inbound customer-service email by answering common questions inline on the website. Customer asks "how do I cancel?" / "how do I add a new match?" / "why isn't my match showing?" → bot answers from a curated FAQ in conversational tone, with links to the right page in the portal. If it can't answer, it offers a one-click "email us with this conversation" escalation.

**Non-goals**:
- Not a generic chatbot. The bot only answers from a fixed FAQ — it cannot speculate, invent policy, or make claims about the product outside what we've written down.
- Not a sales bot. No upsell, no "have you tried our paid plan" tactics.
- Not a replacement for the AI Coach. The Coach analyses *your match data*. The Support Bot answers *how the product works*. Different jobs, different prompts, different cost profile.
- Not a decision tree. The user types in natural language; the LLM matches intent.

---

## 2. User Experience

### Where it lives

A **floating chat bubble** in the bottom-right corner, available on every authenticated portal page. **Portal pages only** — no Wix marketing site, no public-facing surface in v1.

The widget is included on these pages (all inside the portal shell):

| Page | File | Sidebar nav label |
|---|---|---|
| Dashboard / My Profile | `frontend/locker_room.html` | Dashboard |
| Upload Match | `frontend/media_room.html` | Upload Match |
| Match Analytics + Placement Heatmaps | `frontend/match_analysis.html` | Analytics |
| Plans & Pricing | `frontend/pricing.html` | Plans & Pricing |
| Backoffice (admin) | `frontend/backoffice.html` | Backoffice |
| Practice (admin WIP) | `frontend/practice.html` | Practice |
| Portal shell (outer frame) | `frontend/portal.html` | (always loaded) |

**Not** on `coach_accept.html` (pre-account token flow), `players_enclosure.html` (register wizard, pre-account), or any of the public marketing pages (`home.html`, `how_it_works.html`, `pricing_public.html`, `for_coaches.html`).

The widget always has authenticated context: `email`, `plan`, `role`, `credits_remaining`, `first_name` — read from the same auth params the rest of each page already consumes (`?email=&firstName=&surname=&wixMemberId=&key=&api=` per `CLAUDE.md`'s "Locker Room SPAs" section). It can give plan-specific answers and deep-link to the right portal tab.

### What the user sees

```
                                                    ┌──────────────────────┐
                                                    │  ✦ Help & Support    │
                                                    ├──────────────────────┤
                                                    │ Hi Tomo 👋           │
                                                    │ Ask me anything      │
                                                    │ about Ten-Fifty5.    │
                                                    │                      │
                                                    │ ┌──────────────────┐ │
                                                    │ │ Cancel my plan   │ │
                                                    │ │ Add a new match  │ │
                                                    │ │ Invite my coach  │ │
                                                    │ │ Billing question │ │
                                                    │ └──────────────────┘ │
                                                    │                      │
                                                    │ [type a question…] ➤ │
                                                    └──────────────────────┘
                                                                       ╲
                                                                        ◉  ← bubble
```

**Closed state**: a 56px circular button with the same green accent (`--green-primary`) used elsewhere, the spark/help icon (✦), and a small unread-count indicator (always 0 in v1; reserved for future).

**Open state**: 380×560 panel anchored bottom-right. On mobile, expands to full-screen with a back arrow.

### Conversation flow

1. **Greeting** — "Hi Tomo 👋 Ask me anything about Ten-Fifty5." (uses first name from auth context if logged in)
2. **Quick-action chips** — 4 buttons matching common intents (Cancel my plan / Add a new match / Invite my coach / Billing question). Same `coach-quick-btn` styling as the AI Coach.
3. **Free-text input** — natural-language question.
4. **Bot response** — markdown-rendered answer in the same green-callout style as Coach citations (`<span class="coach-pill">…</span>` reused for FAQ section references). Inline action buttons where relevant ("Open Pricing tab →" deep-links to `/portal#pricing`).
5. **Was this helpful?** — thumbs up/down at the end of every answer. Down → "Email us with this conversation" CTA expands.
6. **Email escalation** — pre-fills `info@ten-fifty5.com`, subject `[Support] {first 60 chars of original question}`, body with full Q&A transcript + the user's account context (email, plan).

### Look & feel — explicit reuse

So it feels like one product, not bolted on:

| Element | Reuse from existing |
|---|---|
| Color palette | Same `--green-primary`, `--amber`, `--red` CSS variables |
| Font | Inter (already loaded everywhere) |
| Buttons | `.toggle-btn` / `.coach-quick-btn` shapes |
| Pills/badges | `.coach-pill` for FAQ citations |
| Loading | Same spinner + "Support thinking…" copy mirroring "Coach thinking…" |
| Markdown | Same `highlightStats()`-style transformer for `[brackets]` → green pills |
| Empty state | Same `.coach-empty` class |

The widget's HTML/CSS lives in a single file (`frontend/support_widget.html` as an HTML fragment, OR injected JS — see §6) and pulls the host page's CSS variables, so it automatically inherits any future design changes.

---

## 3. Backend Architecture

### Folder

`support_bot/` — new top-level subdirectory, mirroring `tennis_coach/` to satisfy the folder-organization rule.

```
support_bot/
  __init__.py
  faq.md                  ← THE knowledge base. Plain markdown, versioned in git.
  support_api.py          ← Flask blueprint, registered as support_bp
  db.py                   ← schema creation, conversation logging, dedup cache
  prompt_builder.py       ← system prompt, FAQ stuffing, structured-output schema
  haiku_client.py         ← thin Anthropic SDK wrapper, prompt-caching aware
  rate_limiter.py         ← per-email / per-IP daily limits
  faq_loader.py           ← loads faq.md at boot, computes content hash for cache invalidation
```

### Endpoints

#### `POST /api/support/ask`

- **Auth**: `X-Client-Key` header **required** (same auth pattern as `client_api.py`). All requests are authenticated — no anonymous surface.
- **Body**: `{ message, conversation_id?, email, page_context? }`
  - `message` — the user's question (≤ 1000 chars, validated)
  - `conversation_id` — UUID; if present, this is a turn within an existing conversation
  - `email` — required. Used to look up plan/account context from `billing.member`
  - `page_context` — optional. The slug of the page the widget was opened on (`'/match-analysis'`, `'/pricing'`, etc). Helps Claude answer in context ("you're on the pricing page — to cancel, click…")
- **Flow**:
  1. Rate-limit check (per email if auth, per IP otherwise)
  2. Look up account context if authenticated: plan, role (owner/coach/child), credits remaining
  3. Hash `(message + page_context)` → check `support_bot.faq_cache` for an exact prior match within last 24h. If hit, return cached response (saves the API call entirely)
  4. Build prompt: cached system block (FAQ + system instructions) + user block (question + context)
  5. Call Haiku 4.5 with prompt caching enabled and tool-use for structured output
  6. Persist conversation turn to `support_bot.conversations`
  7. Persist (question_hash, response) to `support_bot.faq_cache`
  8. Return structured response
- **Response**:
  ```json
  {
    "ok": true,
    "conversation_id": "uuid",
    "answer": "To cancel your subscription, go to the Plans & Pricing tab in your portal...",
    "confidence": "high",
    "needs_human": false,
    "cited_sections": ["billing.cancellation"],
    "actions": [
      {"label": "Open Pricing tab", "href": "/portal#pricing"}
    ],
    "tokens_used": {"input": 6240, "output": 180, "cached": 5980}
  }
  ```

#### `POST /api/support/feedback`
- **Body**: `{ conversation_id, turn_id, rating: "up"|"down", comment? }`
- Logs to `support_bot.conversations.feedback` (jsonb).

#### `POST /api/support/escalate`
- **Body**: `{ conversation_id, user_message? }`
- Reads the conversation transcript, formats it, sends an email via existing SES integration to `info@ten-fifty5.com` with the user's account context.
- Returns `{ ok: true, email_sent: true }`.

#### `GET /api/support/health` (admin)
- Returns FAQ content hash, last-loaded timestamp, total questions answered (24h / 7d), thumbs-up/thumbs-down ratio, escalation rate, total spend (24h / 7d).

### DB schema (`support_bot/db.py`)

```sql
CREATE SCHEMA IF NOT EXISTS support_bot;

CREATE TABLE IF NOT EXISTS support_bot.conversations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    turn_idx        integer NOT NULL,
    email           text NOT NULL,           -- always present (portal-only, all auth)
    page_context    text,
    question        text NOT NULL,
    answer          text NOT NULL,
    confidence      text,                    -- 'high'|'medium'|'low'
    needs_human     boolean DEFAULT false,
    cited_sections  text[],
    feedback        jsonb,                   -- {rating, comment, given_at}
    escalated_at    timestamptz,
    tokens_input    integer,
    tokens_output   integer,
    tokens_cached   integer,
    cost_cents      numeric(8,4),            -- computed at write
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX ON support_bot.conversations (conversation_id, turn_idx);
CREATE INDEX ON support_bot.conversations (created_at DESC);
CREATE INDEX ON support_bot.conversations (email);

CREATE TABLE IF NOT EXISTS support_bot.faq_cache (
    question_hash   text PRIMARY KEY,
    page_context    text,
    answer_payload  jsonb NOT NULL,          -- the full structured response
    hit_count       integer DEFAULT 1,
    last_hit_at     timestamptz DEFAULT now(),
    faq_hash        text NOT NULL,           -- sha256 of faq.md at write time
    created_at      timestamptz DEFAULT now()
);
-- On FAQ change, faq_cache rows with old faq_hash get ignored (and swept by cron).
```

Idempotent creation pattern: `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN IF NOT EXISTS`, called from `support_bot/__init__.py` on import — same shape as `tennis_coach/db.py`.

### Rate limiting (`support_bot/rate_limiter.py`)

| Limit | Threshold |
|---|---|
| Per email, soft | 30 questions / 24h |
| Per email, hard cap | 100 / 24h before 429 |

Implemented as `COUNT(*) FROM support_bot.conversations WHERE email=%s AND created_at > now() - interval '...'`. Generous enough that no real customer will hit it; abuse cap is the hard ceiling.

---

## 4. Knowledge Base — `support_bot/faq.md`

The single most important deliverable. The bot is only as good as this file.

**Format**: plain markdown with structured section headers. Each top-level section becomes a citable identifier:

```markdown
# Account

## account.signup
**Q: How do I sign up?**
You sign up via the Wix site at ten-fifty5.com. After signup you'll get an email
with a link to your portal.

## account.cancel
**Q: How do I cancel my subscription?**
Go to your portal → Plans & Pricing tab → "Manage subscription". Cancellation
takes effect at the end of your current billing period; you keep access until
then. We don't pro-rate refunds for partial months.

## account.delete
**Q: Can I delete my account entirely?**
Yes — email info@ten-fifty5.com asking for account deletion. We'll respond
within 5 business days and walk you through it (GDPR-compliant erasure).

# Billing

## billing.plans
**Q: What plans do you offer?**
... (links into docs/pricing_strategy.md content where relevant)

## billing.credits
**Q: What are credits and how do they work?**
...

## billing.refunds
**Q: Can I get a refund?**
...

# Matches

## matches.upload
**Q: How do I upload a new match?**
...

## matches.not_showing
**Q: I uploaded a match but it's not showing in my dashboard. Why?**
Match processing takes 5–15 minutes typically. If it's been over an hour, the
upload may have failed. Check the Media Room → Status. If it shows "failed",
email us with your task_id.

## matches.edit_details
**Q: How do I change the player names / date / location of a match?**
...

# Coach Invites

## coaches.invite
## coaches.revoke
## coaches.accept_problems

# Technical

## tech.video_format
## tech.upload_failed
## tech.iframe_not_loading

# Privacy & Data

## privacy.what_we_store
## privacy.who_can_see
## privacy.export_data
```

**Writing rules** (codified in the spec so the co-worker writing the FAQ has a clear template):
- Each section is one Q + one A
- Answer ≤ 100 words. Direct, no marketing fluff.
- Where an action is needed, name the exact UI path: "Portal → Plans & Pricing → Manage subscription"
- Where policy applies, state it plainly. No "we strive to" or "our team will do its best"
- Cross-references use the section id: `(see also: billing.credits)`
- Edge cases get their own section, not buried in a big paragraph

**FAQ size**: aim for 30–50 sections covering the actual support volume Tomo gets via email today. Expanded over time based on the conversation log.

**Versioning**: `faq.md` is git-tracked. Every change is a commit. Cron sweeps `faq_cache` rows whose `faq_hash` doesn't match current — within an hour of any FAQ change, all cached answers regenerate.

---

## 5. Prompt Engineering

### Model selection

**Claude Haiku 4.5** (`claude-haiku-4-5-20251001`).
- ~10× cheaper than Sonnet 4.6 (which the AI Coach uses)
- Plenty smart for FAQ Q&A (this is retrieval-and-rephrase, not numerical reasoning)
- Fast (sub-second TTFT) so the widget feels snappy

### Prompt caching — the core cost optimisation

The system prompt is **identical for every request** (FAQ + instructions). We mark it `cache_control: {"type": "ephemeral"}` so Anthropic caches it for 5 minutes.

- **Cache miss (first request in 5-min window)**: full input price for ~6k tokens
- **Cache hit (every subsequent request)**: 10% of input price for the cached portion

A busy support session sees mostly cache hits → effective cost ~$0.001/query.

### Structured output via tool use

Rather than asking Claude to "respond in JSON" (unreliable), we define a single tool the model is forced to call:

```python
SUPPORT_TOOL = {
    "name": "answer_user",
    "description": "Answer the user's question using only the provided FAQ.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "The answer in friendly conversational tone, 50-150 words. "
                               "Reference FAQ sections in [brackets] like [billing.cancellation]."
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "high = direct match in FAQ. medium = partial match, some inference. "
                               "low = question not covered, generic safe answer."
            },
            "needs_human": {
                "type": "boolean",
                "description": "True if the question is account-specific (refund request, billing dispute, "
                               "data deletion, technical bug report) and the user should email support."
            },
            "cited_sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "FAQ section ids used to answer (e.g. ['billing.cancellation'])."
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "href": {"type": "string"}
                    },
                    "required": ["label", "href"]
                },
                "description": "Up to 2 deep-link buttons relevant to the answer."
            }
        },
        "required": ["answer", "confidence", "needs_human", "cited_sections"]
    }
}
```

Call shape: `tool_choice = {"type": "tool", "name": "answer_user"}` — forces the model to use this tool, guarantees parseable structured output.

### System prompt (fixed, cached)

```
You are the Ten-Fifty5 Support Bot. Ten-Fifty5 is a tennis match analytics
service. Players upload match videos, our pipeline analyses them, and they
see dashboards with serve/rally/return stats and AI coaching feedback.

Your job is to answer customer questions using ONLY the FAQ below. You are
friendly, direct, and brief.

Hard rules:
1. ONLY answer from the FAQ. If a question isn't covered, set confidence=low,
   needs_human=true, and tell the user to email info@ten-fifty5.com.
2. NEVER invent policy, prices, deadlines, features, or commitments.
3. NEVER apologise excessively. One "sorry about that" is fine when warranted;
   stop there.
4. If the user is asking about THEIR specific account or data (e.g. "why is
   my match not processing", "I want a refund for last month"), set
   needs_human=true — even if you can answer the general question.
5. Always cite the FAQ section ids you used in [brackets] within the answer.
6. Use the user's first name if known. Match the conversational tone of the
   FAQ — not corporate, not overly casual.
7. If the question is in the AI Coach's territory (analysing the user's
   match data, suggesting tactical changes), redirect them: "That's a great
   question for the AI Coach inside Match Analysis — open any match and click
   the Coach tab."

User context (when authenticated):
- Name: {first_name}
- Plan: {plan}
- Role: {role}
- Credits remaining: {credits}
- Page they're on: {page_context}

=== FAQ ===
{faq.md verbatim, ~6000 tokens}
=== END FAQ ===
```

### Anti-hallucination guardrails

1. **Hard FAQ-only rule** in system prompt
2. **`confidence` field** — frontend treats `low` confidence specially (more prominent "email us" CTA)
3. **`needs_human` flag** — escalates anything account-specific even when the bot has an answer
4. **Citation enforcement** — answers without `cited_sections` get a warning logged; if a session has >30% uncited answers, alert in `/api/support/health`
5. **Structured output via forced tool call** — no free-form prose to drift on
6. **Temperature: 0.3** — same as the Coach. Consistent, slightly varied phrasing
7. **`max_tokens: 400`** — answers are short by construction

---

## 6. Frontend Widget

### Single delivery: integrated into each portal page

The widget is **a single static asset** — `static/support_widget.js` — included by a single `<script>` tag added to each portal HTML file. It self-mounts a bubble + panel into `document.body` on load. The widget script ships its own scoped CSS (using `:where(#nf5-support-widget)` so it inherits the host page's CSS custom properties — `--green-primary`, `--bg-card`, `--text-primary` etc. — without bleeding out).

### Per-page integration

Each of the seven portal HTML files gets one `<script>` tag added near the bottom of `<body>`. The widget reads existing auth from URL params already on the page (no new auth plumbing).

| File | Source reference | Insertion point |
|---|---|---|
| `frontend/portal.html` | the outer shell — widget mounts on the parent frame, floats over the iframe | just before closing `</body>` |
| `frontend/locker_room.html` | inner page (Dashboard / My Profile / Linked Players / Invite Coach) | before closing `</body>`, after existing `<script>` tags |
| `frontend/media_room.html` | upload wizard | before closing `</body>` |
| `frontend/match_analysis.html` | match dashboard (Match Analytics + Placement Heatmaps) | before closing `</body>` |
| `frontend/pricing.html` | plans & pricing | before closing `</body>` |
| `frontend/backoffice.html` | admin pipeline / customers / KPIs | before closing `</body>` |
| `frontend/practice.html` | practice analytics | before closing `</body>` |

**Snippet to add** (identical line on all seven files):

```html
<!-- Support bot widget -->
<script src="/static/support_widget.js" defer></script>
```

The widget script is served by both `locker_room_app.py` (the static-only service) and `upload_app.py` (the main API, same-origin backup) via the existing Flask static-file serving. No new routes needed; just drop the file in `static/`.

### Iframe vs outer-frame mounting

The portal architecture is `portal.html` (outer shell with sidebar) → iframe → inner page (e.g. `match_analysis.html`). Without care, the widget would mount twice — once on the outer shell, once on the inner page — and we'd see two bubbles.

**Solution**: each include of `support_widget.js` runs an idempotency check on load:

```js
// Pseudo-code at top of support_widget.js
if (window.top !== window) {
  // We're in an iframe. Check if parent already has the widget.
  try {
    if (window.top.__nf5_support_mounted__) return;
  } catch (e) { /* cross-origin, treat as not mounted */ }
}
if (window.__nf5_support_mounted__) return;
window.__nf5_support_mounted__ = true;
// ...mount widget
```

Net effect: the bubble appears exactly once, anchored to the **outermost** authenticated frame (the portal shell when accessed via portal, the inner page when accessed directly via a deep link). Clicking the bubble overlays the widget panel above whichever inner page is currently active.

### Reading auth context

Each portal page already reads `?email=&firstName=&surname=&wixMemberId=&key=&api=` URL params at boot (as documented in `CLAUDE.md` "Locker Room SPAs"). The widget reuses these directly via `new URLSearchParams(location.search)` — no shared global needed. If a page already exposes `window.__nf5_auth__` (e.g. portal.html may set this), the widget prefers it.

### Widget states & UX details

- **Closed bubble**: 56px, anchored bottom-right, animated subtle pulse on first portal load per session (cookie-tracked).
- **Open**: 380×560 panel; focuses input by default, shows greeting + 4 quick-action chips, history of current conversation.
- **Persistence**: `conversation_id` stored in `sessionStorage` — survives navigation between portal sub-pages within the SPA, cleared on tab close. (Cross-session history is a future enhancement, see §11.)
- **Loading**: same spinner + "Support thinking…" text mirroring the AI Coach's "Coach thinking…".
- **Markdown rendering**: same `[section.id]` → green `.coach-pill` regex transformer that the AI Coach uses (see `match_analysis.html:1868`). On hover, the pill shows a tooltip with the FAQ section title.
- **Inline action buttons**: rendered below the answer for each item in `actions[]`. Same styling as `.coach-quick-btn` (see `match_analysis.html:1802`), with an arrow ↗ glyph.
- **Thumbs**: small 👍/👎 row at the bottom of every bot message. Click → fires `/api/support/feedback`.
- **Escalation**: thumbs-down expands a "This didn't help — email us" CTA → `/api/support/escalate` → confirmation toast. Email always goes to `info@ten-fifty5.com`.
- **Mobile**: panel goes full-screen with a sticky header showing "Help & Support" + a back arrow. Same pattern as the existing media-room mobile UX.

### Accessibility

- Keyboard: `Esc` closes panel, `Cmd/Ctrl + /` opens, Tab cycles input/buttons
- Focus traps inside open panel
- ARIA labels on bubble and panel (`role="dialog"`, `aria-label="Help and support chat"`)
- Reduced-motion respects `prefers-reduced-motion`

### Widget states & UX details

- **Closed bubble**: 56px, animated subtle pulse on first page load (one-time per session, cookie-tracked)
- **Open**: focuses the input by default, shows greeting + 4 quick-action chips, history of current conversation
- **Persistence**: conversation_id stored in `sessionStorage` — survives page navigation within the SPA, cleared on tab close. (Across-session history could come later.)
- **Loading**: same spinner + "Support thinking…" text as the AI Coach's "Coach thinking…"
- **Markdown rendering**: same `highlightStats()`-style regex turning `[section.id]` into green `.coach-pill` spans (which on hover show a tooltip with the FAQ section title). Bullet lists and bold get rendered as well.
- **Inline action buttons**: rendered below the answer for each item in `actions[]`. Same styling as `.coach-quick-btn` but with an arrow ↗ glyph.
- **Thumbs**: small 👍/👎 row at the bottom of every bot message. Click → fires `/api/support/feedback`.
- **Escalation**: thumbs-down expands "This didn't help — email us" CTA → `/api/support/escalate` → confirmation toast.
- **Mobile**: panel goes full-screen with a sticky header showing "Help & Support" + a back arrow. Same as the existing media-room mobile pattern.

### Accessibility

- Keyboard: Esc closes panel, Cmd/Ctrl+/ opens, Tab cycles input/buttons
- Focus traps inside open panel
- ARIA labels on bubble and panel
- Reduced-motion respects `prefers-reduced-motion`

---

## 7. Cost Model

**Pricing (Claude Haiku 4.5)** — input ~$1/MTok, output ~$5/MTok, **cached input ~$0.10/MTok**.

| Component | Tokens | Notes |
|---|---|---|
| System prompt (cached) | ~5,800 | FAQ + instructions, paid 1× per 5-min window per cache key |
| User context block | ~80 | name/plan/credits/page |
| Question | ~30 | typical user question |
| Tool definition | ~280 | schema |
| Response | ~180 | structured output |

**Per-call costs**:
- **First call (cold cache)**: 6,200 in + 180 out = ~$0.0072
- **Subsequent calls (cache hit)**: 5,800 cached + 110 fresh in + 180 out = ~$0.00098

**Plus deduplication** — `support_bot.faq_cache` returns prior identical Q+context responses with zero API cost. Realistically 30–40% of questions will dedupe.

**Realistic monthly cost** (assume 30 questions/day average, growing):
| Volume | Cost/day | Cost/month |
|---|---|---|
| 30 q/day, 30% dedup | ~$0.025 | ~$0.75 |
| 100 q/day, 30% dedup | ~$0.08 | ~$2.50 |
| 500 q/day, 40% dedup | ~$0.30 | ~$9 |

**Compared to AI Coach**: Coach uses Sonnet 4.6 at ~$0.01/call vs Support Bot at ~$0.001/call. Order of magnitude cheaper.

**Cost guardrails**:
- Daily ceiling per user (rate limit) caps blast radius
- Admin `/api/support/health` shows running spend
- Hard kill switch: env var `SUPPORT_BOT_ENABLED=false` returns a "we're temporarily unavailable, please email" response

---

## 8. Risks & Mitigations

1. **FAQ drift** — product changes, FAQ doesn't. Mitigation: every PR that changes user-visible behaviour gets a "FAQ update?" checkbox in the description; cron alert if FAQ hasn't been touched in 60 days.
2. **Hallucinated policy** — bot invents a refund window. Mitigation: hard FAQ-only rule + `cited_sections` enforcement + `confidence=low` triggers prominent escalation CTA.
3. **Account-specific questions answered generically** — bot says "your match should be ready in 15 min" when actually that user's task failed. Mitigation: rule #4 in system prompt forces `needs_human=true` for any account-specific question.
4. **Brand-damaging tone failures** — bot is too apologetic / too casual / mispronounces a name. Mitigation: tone rules in system prompt; thumbs-down review weekly to catch patterns.
5. **Abuse / cost spike** — someone scripts 10k questions from a single account. Mitigation: per-email rate limit + hard 24h cap + spend alert in `/api/support/health`. Hard kill switch via env var `SUPPORT_BOT_ENABLED=false`.
6. **Iframe double-mount** — widget appears twice (once on `portal.html` shell, once on inner page). Mitigation: `window.__nf5_support_mounted__` idempotency flag (see §6 "Iframe vs outer-frame mounting"). Tested by loading both portal-with-iframe and direct page access.

---

## 9. Implementation Order

**Phase 0 — FAQ writing (Tomo + co-worker)**
- Draft `support_bot/faq.md` with 30–50 Q+A sections covering current support volume
- This is the single most important step. Code without FAQ is useless. **This is genuinely a co-worker task** — they don't need to know Python, just the product.

**Phase 1 — Backend core (1 day)**
- `support_bot/` folder + `db.py` schema + `faq_loader.py` + `haiku_client.py` with prompt caching
- `support_api.py` blueprint with `POST /api/support/ask` only
- Register in `upload_app.py`
- Curl-test from terminal, verify FAQ citations match payload

**Phase 2 — Caching, feedback, escalation (½ day)**
- `faq_cache` dedup
- `POST /api/support/feedback`
- `POST /api/support/escalate` (reuse existing SES sender from `coach_invite/email_sender.py`)
- `GET /api/support/health` admin endpoint

**Phase 3 — Frontend widget (1 day)**
- `static/support_widget.js` — single self-contained file with bubble, panel, chat state, API calls, scoped CSS
- Add the one-line `<script src="/static/support_widget.js" defer></script>` snippet to all seven portal HTML files (see §6 table)
- Verify the iframe idempotency check — load `portal.html`, confirm exactly one bubble; load `match_analysis.html` directly, confirm one bubble
- Mobile responsive
- Thumbs feedback + escalation CTA

**Phase 4 — Iterate from real conversations (ongoing)**
- Weekly review of thumbs-down + escalations → expand FAQ
- Track `cited_sections` distribution to find FAQ gaps

**Total to MVP**: ~2.5 working days of code, plus the FAQ writing (which can run in parallel and is the actual load-bearing work).

---

## 10. Locked decisions (2026-04-27)

1. **Surface** — portal pages only. No Wix marketing-site work in v1.
2. **Escalation destination** — `info@ten-fifty5.com` for all escalations. No split inboxes.
3. **First-name source** — `billing.member.first_name`, falling back to the `firstName` URL param already on every portal page.
4. **Greeting personalisation** — always on, using first name when available.

## Still open (need Tomo's call)

5. **Conversation history persistence** — A (in-tab only, sessionStorage) vs B (persist across sessions per email). Recommendation: **A for v1**.
6. **Scope of v1 FAQ** — A (top 30 inbound questions) vs B (comprehensive 50–80 sections). Recommendation: **A**, expand from real conversation logs.

---

## 11. Future enhancements (not v1)

- **Smart routing to AI Coach** — when user asks a coaching question, deep-link them into Match Analysis Coach tab with that question pre-filled.
- **Proactive surfacing** — if a user's match has been stuck >1h, the bubble pulses with "Your match is taking longer than usual — tap here for help".
- **Multilingual** — when SES + Wix go multi-language, translate the FAQ + add per-language system prompt. Haiku handles this fine without retraining.
- **Conversation memory across sessions** — store transcript per email so "as we discussed last week…" works.
- **Voice input** — same widget, mic button, browser SpeechRecognition.
- **Embeddings retrieval** — if FAQ grows past 15k tokens, switch from "stuff entire FAQ in cached system prompt" to "embed FAQ sections + retrieve top 5". Not needed for v1.
