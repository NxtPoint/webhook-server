# frontend

> All HTML SPAs for the platform. Custom-built ECharts + canvas dashboards, no SPA framework. Each file is self-contained — vanilla JS, inline `<style>`, ECharts via CDN, no bundler.

## What this owns

- 14 HTML files: 10 authenticated app pages + 4 public marketing pages
- A shared design system applied by convention (Inter font, green/amber/red palette, CSS variables), duplicated across files — no shared CSS file or component library
- Shared ECharts helpers (`eBar`, `eStackedBar`, `ePie`, `eGauge`) that appear identically in every chart-heavy page

## What this is NOT

- **Not Wix.** The portal is embedded in a Wix iframe at `https://www.ten-fifty5.com/portal`, but everything in this directory is server-rendered HTML from Render. Wix's only remaining responsibilities are member auth (login → URL params), payment checkout (PayPal via Wix Pricing Plans), and the subscription event webhook.
- **Not bundled.** Pages are served as-is via `Flask.send_file()`. There is no webpack, no rollup, no TypeScript compile step. View-source on any page is the actual code.
- **Not a SPA in the React sense.** Each HTML file is its own app. Cross-page nav happens through `portal.html`'s sidebar, which iframes the next page in.

## Who serves these files

Two services, identical routing behaviour:

- **`locker-room`** (Render Flask service): the primary host. Each of the 14 files has a route in `locker_room_app.py` that returns `send_file(frontend/<name>.html)`. No DB connection, only Flask + gunicorn.
- **Sport AI - API call** (main API; `webhook-server` in `render.yaml`): each authenticated route is mirrored at the same URL in `upload_app.py` so the SPA can fetch same-origin from inside the portal iframe (avoids CORS for `/api/client/*` calls).

The pattern is one helper `_html(name)` in both apps that resolves an absolute path under `frontend/`. No file is conditionally served — both apps return the same bytes.

## Route map

### Authenticated app pages

| Route | File | Audience | One-line purpose |
|---|---|---|---|
| `/` | `locker_room.html` | Player / parent | Home dashboard: matches per month, usage gauge, match history, profile + linked-players + invite-coach tabs |
| `/portal` | `portal.html` | All authenticated | **Entry point.** Collapsible sidebar nav shell. Iframes the inner page. Embedded in Wix at `/portal`. |
| `/media-room` | `media_room.html` | Player / parent | 4-step upload wizard: game type → upload → details → progress |
| `/match-analysis` | `match_analysis.html` | Player / parent / coach | **Primary match dashboard.** 4 modules: Match Analytics, Placement Heatmaps, Player Performance, AI Coach |
| `/practice` | `practice.html` | Player / parent / coach | Practice analytics (heatmaps, timeline). Reference design for new dashboards. |
| `/pricing` | `pricing.html` | All authenticated | Entitlement-aware plans page. Renders new-plan / top-up / coach view. Sends `postMessage({type:'wix-checkout', planId})` up to Wix for PayPal checkout. |
| `/help` | `support.html` | All authenticated | Support bot UI. Mirrors AI Coach styling: greeting, quick-prompt chips, green-callout answer, amber escalate CTA. Backed by `/api/support/*`. |
| `/coach-accept` | `coach_accept.html` | Token-auth public | Coach invitation acceptance. Token from email URL is the auth. |
| `/register` | `players_enclosure.html` | Onboarding | Players' Enclosure — multi-player household setup wizard |
| `/backoffice` | `backoffice.html` | Admin email only | Pipeline status, customer table, KPI cards. Auth gate via `ADMIN_EMAILS` server-side. |

### Public marketing pages

These are **same-origin backups for SEO.** Wix is the primary host for the public marketing site. **SEO from these pages is invisible to Google because Wix iframes them** — see memory `project_seo_iframe_constraint.md`. Real SEO work belongs in Wix Studio.

| Route | File | Purpose |
|---|---|---|
| `/home` | `home.html` | Landing page |
| `/how-it-works` | `how_it_works.html` | Feature explainer + FAQ |
| `/pricing-public` | `pricing_public.html` | Marketing pricing (distinct from authenticated `/pricing`) |
| `/for-coaches` | `for_coaches.html` | Coach signup / onboarding marketing |

### Health / diagnostic

| Route | Purpose |
|---|---|
| `/__alive` | Locker-room liveness probe — `{"ok": true, "service": "locker-room"}`. No auth. |

## Auth pattern (the URL-param dance)

Every authenticated SPA expects these params, forwarded through the portal iframe:

```
?email=<member_email>&firstName=<f>&surname=<s>&wixMemberId=<id>&key=<CLIENT_API_KEY>&api=<api_base>
```

The portal reads them from Wix's iframe-postMessage handshake, then constructs every inner-iframe `src` with those params. SPAs read params via the helper `authParams()` defined inline in each page.

`X-Client-Key` header on every API call comes from the `key` param. Same key is shared across all member sessions — server-side `email` parameter on each request scopes data to the right account.

## Shared design system (by convention, not by import)

Each file inlines the same shape:

```css
:root {
  --bg: #f5f5f5;
  --green: #1a5c2e;       /* primary */
  --green-light: #22783c;
  --green-bg: rgba(26,92,46,0.08);
  --amber: #d97706;       /* warning / coach-pro CTA */
  --red: #dc2626;          /* errors / destructive */
  --text: #1a1a1a;
  --text-sec: #6b7280;
  --text-dim: #9ca3af;
  --border: #e5e5e5;
  --radius: 4px;
}
```

- **Font**: Inter (Google Fonts CDN). System-font fallback chain.
- **Buttons**: `.toggle-group` + `.toggle-btn` for any segmented chooser.
- **Charts**: ECharts via CDN. Per-page helpers `eBar`, `eStackedBar`, `ePie`, `eGauge` — same API in every file. If you change one signature, change them all.
- **Layout**: `.app-shell` with sidebar variants. Mobile breakpoint at 768px.

**Drift risk**: this is duplicated, not shared. If the green changes, every file changes. There's no `frontend/design-system.css` today and adding one is non-trivial because each SPA is independently deployed and cached. Track this in [`../docs/business.md`](../docs/business.md) §12 if it becomes a real cost.

## iOS / iframe gotchas

All authenticated pages run inside Wix → portal → page (three iframe levels deep). To survive iOS Safari's iframe quirks:

- Use `height: 100%` on `html, body` — never `100vh` (broken on iOS).
- Include `<meta name="viewport" content="..., viewport-fit=cover">`.
- Add `padding-bottom: 300px` to mobile layouts so the iOS keyboard doesn't cover content.
- `overflow: hidden` on `html, body`; the inner `.app-shell` does the scrolling.

These rules apply to every SPA. Portal sets them; nested pages must too because they're rendered in their own iframe.

## Data sources by page

Every API call goes to the same-origin main API ("Sport AI - API call") because of the route mirroring above.

| Page | Primary endpoints |
|---|---|
| `locker_room.html` | `/api/client/profile`, `/api/client/members`, `/api/client/coaches`, `/api/client/usage`, `/api/client/matches` |
| `match_analysis.html` | `/api/client/match/<task_id>/{kpi,serve-breakdown,return-breakdown,rally-breakdown,shot-placement}`, `/api/client/coach/cards/<task_id>`, `/api/client/coach/analyze` |
| `practice.html` | `/api/client/practice-detail/<task_id>`, `/api/client/practice-heatmap/<task_id>/<type>` |
| `media_room.html` | `/api/submit_s3_task`, `/api/client/upload-init`, `/api/client/upload-part`, `/api/client/upload-complete`, `/api/client/entitlements` |
| `portal.html` | `/api/client/entitlements`, plus iframe-driven nav |
| `pricing.html` | `/api/client/entitlements`, posts up to Wix for checkout |
| `support.html` | `/api/support/{ask,feedback,escalate}` |
| `coach_accept.html` | `/api/coaches/accept-token` (token-only auth) |
| `backoffice.html` | `/api/client/backoffice/{pipeline,customers,kpis}` |
| `players_enclosure.html` | `/api/client/register`, `/api/client/children`, `/api/client/profile-photo-upload-url` |

## Gotchas

- **Two apps serve the same files.** A change to `frontend/match_analysis.html` is picked up by both `locker-room` and the main API ("Sport AI - API call") on next deploy. There's nothing to keep in sync — the file IS the source.
- **Portal is the only entry point.** Direct links to `/match-analysis` work but won't have the sidebar. Always link via `portal.html#nav=match-analysis` (or whatever the portal hash route is) when sharing in-product.
- **Dev gates are inline.** Some flows (T5 game types in Media Room, Technique upload) check `email === 'tomo.stojakovic@gmail.com'` directly in JS to gate visibility. There's no server-side gate for these — they're hidden, not enforced.
- **Marketing pages are mostly dead weight on Render.** Wix is the primary host; these exist as same-origin backups but the SEO win is null because Wix iframes everything. If you delete them, only direct-URL access (very rare) breaks.
- **No SPA router.** Each `.html` is a full page load. The portal does in-iframe nav (`navigateTo()` swaps the iframe `src`); there's no client-side routing.
- **CSS variables are duplicated, not imported.** A truly system-wide colour change is N edits across N files.

## Conventions for new pages

If you add a new SPA:

1. Drop the file in `frontend/<page>.html`. Vanilla HTML/JS, no bundler.
2. Add a route to `locker_room_app.py` returning `_html("<page>.html")`.
3. Mirror the route in `upload_app.py` (same-origin backup) — copy the `_html(name)` helper pattern.
4. Reuse the design-system block from any existing page (copy `:root` vars + Inter font import + `.toggle-btn` styles).
5. Reuse the `authParams()` helper from any existing page for URL-param auth.
6. If it needs a sidebar entry, add to `portal.html` `NAV_ITEMS`. (Sub-items get a tree-line connector.)
7. Mobile: include the iOS iframe meta tag + `padding-bottom: 300px` on the mobile layout.

## See also

- [`../CLAUDE.md`](../CLAUDE.md) §Locker Room SPAs — short overview that points here
- [`../docs/dashboards.md`](../docs/dashboards.md) — gold view + endpoint catalogue (data the SPAs render)
- [`../docs/business.md`](../docs/business.md) §4 — entitlement gates the SPAs respect (free-trial AI Coach lock, coach view-only, etc.)
- `locker_room_app.py` — primary host for these files
- `upload_app.py` — same-origin route mirror
- Memory `project_seo_iframe_constraint.md` — why marketing-page SEO from Render is invisible to Google
