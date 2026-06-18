# Live Feature Reference

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)). One section each for the three live customer-facing features that aren't covered elsewhere: the Support Bot, the analytics Dashboards / gold views, and Technique analysis.

Sources merged (verbatim): `docs/support_bot.md`, `docs/dashboards.md`, `docs/technique.md`. (Design-history docs live in `_archive/`: `support-bot-design.md`, `llm-coach-design.md`.)
---

# Support Bot

# Support Bot (`support_bot/`)

Customer-service chat answering FAQ-only questions on the portal. Uses Claude Haiku 4.5 with prompt caching + forced tool-use for guaranteed structured output. All authenticated — **dual-mode (de-Wix, 2026-06-17): a Clerk JWT via `Authorization: Bearer` OR the legacy `X-Client-Key`** (`_check_client_key()` → `auth_v2.resolve_principal`); portal-only — no public surface.

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

All require authentication — a Clerk JWT (`Authorization: Bearer <token>`) OR the legacy `X-Client-Key` header (dual-mode).

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

---

# Dashboards & Gold Views

# Dashboards & Gold Views

The primary analytics experience. Custom-built ECharts + canvas dashboards that read from thin SQL views.

## The Dashboard (`match_analysis.html`)

Single-page app at `/match-analysis`. Loaded inside the portal iframe with `?email=&key=&api=` auth params.

Four modules selectable via the top green strip:

1. **Match Analytics** (8 tabs) — Summary (KPI strip + H2H bars + speed gauges), Serve Performance, Serve Detail, Return Summary, Return Detail, Rally Summary, Rally Detail, Point Analysis. Reads `gold.match_kpi` + breakdowns.
2. **Placement Heatmaps** (5 tabs) — Serve Placement, Player Return Position, Return Ball Position, Groundstrokes, Rally Player Position. All tabs have: Player A/B toggle (green/blue convention), Set filter, tab-specific filters (serve try, stroke, depth, aggression). Blue court `#1a4a8a` on green `#2d6a4f`, near-side plotting with normalised coords. Reads `gold.match_shot_placement`.
3. **Player Performance** (3 tabs, Player A only) — KPI Scorecard (18 KPIs across Serve/Return/Rally/Games/Speed, rolling 5-match avg vs benchmark, sparkline), Trend Charts, Last Match vs Average. Reads `gold.player_performance` (email-scoped).
4. **AI Coach** — standalone module. See [LLM Coach](#llm-tennis-coach) below.

**Cross-module**: collapsible match list sidebar (280px → 46px, auto-collapse <1200px), filter persistence within module, `gold.vw_player` / `gold.vw_client_match_summary` filter to `sport_type = 'tennis_singles'` (excludes T5/technique dev matches).

## Gold Presentation Views

Created idempotently on boot by `gold_init.py::gold_init_presentation()` (`DROP VIEW IF EXISTS ... CASCADE` + `CREATE VIEW` per view, each try/except-wrapped).

**Base layer** (dim + fact):
- `gold.vw_player` — dim. Resolves `first_server` → `player_a_id` / `player_b_id`. Filtered to `sport_type = 'tennis_singles'` (excludes T5 dev matches). Generates monotonic `session_id`.
- `gold.vw_point` — fact. `silver.point_detail` flattened + joined to `vw_player`.

**Per-match presentation layer** (one view per chart/table):
| View | Feeds | Shape |
|---|---|---|
| `gold.match_kpi` | Summary tab, speed gauges, head-to-head, point analysis | 1 row per match, both players in `pa_*` / `pb_*` columns. ~120 columns including games won, 1st/2nd serve win%, unreturned serves, serve speed split, rally outcomes |
| `gold.match_serve_breakdown` | Serve Performance/Detail tabs, Serve Placement table | 1 row per (task, player, side, direction, serve_try) |
| `gold.match_return_breakdown` | Return Summary/Detail tabs | 1 row per player, with returns made/won/depth/stroke/vs-1st/vs-2nd |
| `gold.match_rally_breakdown` | Rally Summary/Detail tabs, aggression profile | 1 row per player, aggression/depth/stroke/speed counts |
| `gold.match_rally_length` | Rally Detail length distribution + length-bucket win comparison | 1 row per (task, length_bucket) with pa/pb wins |
| `gold.match_shot_placement` | All Placement Heatmap tabs + Point Analysis zone tables + return/rally cross-tab charts | 1 row per shot — coords, outcome, stroke, phase |

**Cross-match performance layer** (Player A only):
| View | Feeds | Shape |
|---|---|---|
| `gold.player_match_kpis` | Intermediate — consumed by `player_performance` | 1 row per (email, task_id) with 18 KPIs for Player A |
| `gold.player_performance` | Player Performance module scorecard | 1 row per (email, kpi_name) with benchmark, rolling avg, delta, trend, status, sparkline |

**Coach-specific views** (created by `tennis_coach/coach_views.py::init_coach_views()`):
- `gold.coach_rally_patterns` — per (task, player, stroke, depth, aggression) error/winner rates
- `gold.coach_pressure_points` — **STUB** (returns zero rows with correct column shape; break-point detection needs window-function score reconstruction which isn't implemented yet)

**Legacy**:
- `gold.vw_client_match_summary` — created by `db_init.py`, feeds `/api/client/matches` match list. Will be replaced by `gold.match_kpi` eventually but currently live.

## Client API — Dashboard Endpoints

All under `/api/client/match/*`, dual-mode auth (Clerk JWT or legacy CLIENT_API_KEY), `email` query param for tenant isolation. Thin passthroughs: `SELECT * FROM gold.<view> WHERE task_id = CAST(:tid AS uuid)` → JSON.

| Endpoint | View |
|---|---|
| `GET /api/client/match/kpi/<task_id>` | `gold.match_kpi` |
| `GET /api/client/match/serve-breakdown/<task_id>` | `gold.match_serve_breakdown` |
| `GET /api/client/match/return-breakdown/<task_id>` | `gold.match_return_breakdown` |
| `GET /api/client/match/rally-breakdown/<task_id>` | `gold.match_rally_breakdown` |
| `GET /api/client/match/rally-length/<task_id>` | `gold.match_rally_length` |
| `GET /api/client/match/shot-placement/<task_id>` | `gold.match_shot_placement` |
| `GET /api/client/player/performance` | `gold.player_performance` (email-scoped, not task_id) |
| `GET /api/client/technique/report/<task_id>` | `gold.technique_report` |
| `GET /api/client/technique/comparison/<task_id>` | `gold.technique_comparison` |
| `GET /api/client/technique/kinetic-chain/<task_id>` | `gold.technique_kinetic_chain_summary` |
| `GET /api/client/technique/progression` | `gold.technique_progression` (email-scoped) |

On load, `match_analysis.html::selectMatch()` fires all six match endpoints in parallel via `Promise.all()` and caches as `selectedData.kpi / .serve / .return / .rally / .rallyLength / .placement`. The performance endpoint is fetched lazily when the Player Performance module is first opened.

Other dashboard endpoints:
- `/api/client/matches` — match list for sidebar (uses `gold.vw_client_match_summary`, filtered to `sport_type = 'tennis_singles'`)
- `/api/client/matches/<task_id>` — legacy raw silver.point_detail fetch
- `/api/client/match-analysis/<task_id>` — legacy full silver fetch

## LLM Tennis Coach

Package: `tennis_coach/`. Design doc: `docs/llm_coach_design.md`. Its own dashboard module.

**Endpoints** (dual-mode auth (Clerk JWT or legacy CLIENT_API_KEY)):
- `POST /api/client/coach/analyze` — named prompt or freeform. Returns `{response, data_snapshot, cached, tokens_used}`.
- `GET /api/client/coach/cards/<task_id>?email=` — pre-generated 3-card insight summary. Cached forever per (task, email).
- `GET /api/client/coach/status/<task_id>?email=` — poll for card generation status.
- `GET /api/client/coach/debug/<task_id>?email=` — **admin only**. Raw payload Claude sees, without calling Claude.

**Data flow**: `coach_api.py::_fetch_data_for_task()` auto-routes by `sport_type`:
- Match tasks → `tennis_coach/data_fetcher.py` → reads `gold.match_kpi`, `gold.match_*_breakdown`, `gold.coach_rally_patterns`
- Technique tasks → `technique/coach_data_fetcher.py` → reads `gold.technique_report`, `gold.technique_kinetic_chain_summary`, `gold.technique_comparison`

Then `prompt_builder.py` builds one of 5 templates (serve_analysis / weakness / tactics / cards / freeform) → `claude_client.py` calls Anthropic SDK (`claude-sonnet-4-6`, temp 0.3, max 600 tokens) → response cached in `tennis_coach.coach_cache` keyed on (task_id, email, prompt_key).

**Guardrails**: Player-A-only coaching (never analyses opponents). Small-sample suppression (MIN_SAMPLE=5) drops dimensions with too few shots. Rate limits: 5 freeform calls per (email, task_id) per day, 20 per email per day; cards excluded.

**Cost**: ~$0.01 per call, ~1.2-1.5k tokens. Realistic usage: $5-20/month. Requires `ANTHROPIC_API_KEY`.

**Credit integration**: NOT yet implemented — rate-limited only. Will require `billing_service.consume_entitlement()` integration.

## Practice Analytics Dashboard (`practice.html`)

Full-featured dashboard for serve/rally practice sessions. Apache ECharts + canvas. Route: `GET /practice`.

Tabs: Overview, Performance, Court Placement, Serve/Rally Analysis, Heatmaps (S3-rendered), Video.

Client API (practice-specific, not gold-layer):
- `GET /api/client/practice-sessions?email=` — list sessions
- `GET /api/client/practice-detail/<task_id>?email=` — `silver.practice_detail` rows + summary
- `GET /api/client/practice-heatmap/<task_id>/<type>?email=` — presigned S3 URL for heatmap images

Practice is the **reference design** for all custom dashboards. New dashboards should mirror its CSS, chart styling (`eBar`, `eStackedBar`, `ePie`, `eGauge`), mobile breakpoints, sidebar layout.

---

# Technique Analysis

# Technique Analysis (`technique/`)

Biomechanics stroke analysis via the external SportAI Technique API. Dev-only — gated to `tomo.stojakovic@gmail.com` in `media_room.html`. Sport type: `technique_analysis`.

## Flow

Unlike SportAI (async + URL polling) and T5 (AWS Batch + sentinel URL), the Technique API is **synchronous streaming**. A single background thread in `upload_app.py::_technique_run_pipeline()` does everything end-to-end:

```
Media Room → /api/submit_s3_task {gameType: 'technique'}
  → _technique_submit() creates task_id, spawns daemon thread:
    1. Download video from S3 (in memory, no intermediate storage)
    2. POST multipart/form-data to TECHNIQUE_API_BASE/process
    3. Read streaming JSON lines until status=done
    4. Bronze ingest → bronze.technique_* tables
    5. Silver build → silver.technique_* tables
    6. Copy video → trimmed/{task_id}/technique.mp4
    7. Mark complete (session_id + ingest_finished_at on submission_context)
    8. SES notify via existing _notify_ses_completion
```

Status tracked via standard `bronze.submission_context` columns (same as SportAI/T5). No in-memory tracker, no sentinel URL, no auto-ingest routing — `_technique_status()` just reads the DB.

## Tables

**Bronze** (`bronze.technique_*`, created by `technique/db_schema.py::technique_bronze_init()`):
- `technique_analysis_metadata` (1 row per task: uid, status, sport, swing_type, dominant_hand, height, warnings, errors)
- `technique_features` (1 row per feature: name, level, score, value, observation, suggestion, ranges, highlight_joints/limbs)
- `technique_feature_categories` (category → score, feature_names)
- `technique_kinetic_chain` (per body segment: peak_speed, peak_timestamp, plot_values)
- `technique_wrist_speed` (raw wrist_speed JSON, 1 row per task)
- `technique_pose_2d` / `technique_pose_3d` (full pose JSON blob, 1 row per task)

**Silver** (`silver.technique_*`, built by `technique/silver_technique.py::build_silver_technique()`):
- `technique_summary` — per-analysis: overall_score, level, top_strength, top_improvement
- `technique_features_enriched` — features joined with category scores + score_vs_category delta
- `technique_kinetic_chain_analysis` — peak ordering/sequencing, speed/time deltas between segments, is_sequential flag
- `technique_pose_timeline` — per-frame 2D+3D consolidated with confidence extraction
- `technique_trends` — cross-session (email-scoped): feature score history per (email, swing_type, feature_name, task_id)

**Gold** (`gold.technique_*`, created by `technique/gold_technique.py::init_technique_gold_views()` — DROP+CREATE pattern like `gold_init.py`):
- `technique_report` — per-analysis complete report (overall_score, category_scores, top_strengths/improvements, all_features as JSON arrays)
- `technique_comparison` — per-feature benchmarks (beginner/intermediate/advanced/professional ranges)
- `technique_kinetic_chain_summary` — simplified: chain_sequence, fastest/slowest segment, duration, is_sequential
- `technique_progression` — cross-session improvement (rolling_avg_5, delta_vs_prev, trend: improving/declining/stable)

## Key files

| File | Purpose |
|---|---|
| `technique/api_client.py` | `call_technique_api(video_bytes, metadata)` — streaming POST, reads JSON lines until status=done/failed |
| `technique/db_schema.py` | Bronze table DDL, idempotent |
| `technique/bronze_ingest_technique.py` | `ingest_technique_bronze(conn, payload, task_id, replace=True)` — extracts JSON into bronze tables |
| `technique/silver_technique.py` | Silver builder — same pattern as `build_silver_v2.py` |
| `technique/gold_technique.py` | Gold view DDL + `init_technique_gold_views()` |
| `technique/coach_data_fetcher.py` | Assembles technique data for LLM Coach (reads gold views) |

## Frontend

Media Room Step 3 `renderTechniqueForm()` collects: sport (currently tennis-only), swing type (12 dropdown options: forehand/backhand drive/topspin/slice, 3 serve types, 2 volleys, overhead), dominant hand toggle, height in cm (converted to mm on submit), date, location.

## Notes

- Unlike SportAI, **no intermediate S3 storage of the JSON result** — the payload stays in memory and goes straight into bronze ingest.
- Swing type list in the form is currently hardcoded; spec says to fetch dynamically from API when available.
- Pickleball sport is recognised by the API but out of scope for this build.
- Video trim is a simple `s3.copy_object` to `trimmed/{task_id}/technique.mp4` — no EDL, no FFmpeg (technique videos are 3-10s).
