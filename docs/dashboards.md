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

All under `/api/client/match/*`, CLIENT_API_KEY auth, `email` query param for tenant isolation. Thin passthroughs: `SELECT * FROM gold.<view> WHERE task_id = CAST(:tid AS uuid)` → JSON.

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

**Endpoints** (CLIENT_API_KEY auth):
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
