# Marketing & SEO

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)). Merges the public marketing-site architecture + off-page plan, the backlink kit, the Klaviyo lifecycle flows, and the coach cold-outreach plan.

> **Correction (2026-06-17):** marketing "Log in / Start Free" CTAs now point at **`/login`** (Clerk), NOT the Wix Studio portal URL referenced in some sections below. Wix is retained only as the `PAYPAL_ENABLED=0` payment rollback. Event names are canonical per `marketing_crm/contracts/events.md`: **`match_uploaded`** and **`report_viewed`** (older `match_upload` / `report_view` spellings have been reconciled in the emitter).

Sources merged (verbatim): `docs/seo_marketing_migration.md`, `docs/seo_backlink_kit.md`, `marketing_crm/klaviyo/*` (3), `marketing_crm/outreach/coach_cold_outreach_plan.md`.
---

# Public marketing site — architecture, state + off-page plan

# Public Marketing Site — Architecture, State + Off-Page Plan

**Status: LIVE since 2026-06-15.** `www.ten-fifty5.com` + apex `ten-fifty5.com` serve the native, fully-crawlable marketing site from Render (the existing **`locker-room`** service, host-switched — no second paid service). The logged-in Wix app (login + portal + checkout) moved to its **free Wix Studio URL** `https://info5945780.wixstudio.com/online-tennis-analyt`. Dashboards are unchanged (Render-hosted, embedded in the Wix portal shell).

> **Why this exists.** 6 months on Wix → ~6 visitors/month. Two causes: (1) on-page — Wix rendered the marketing pages through JavaScript, so the only solid crawlable text was a block pasted at the bottom of each page; (2) off-page — Domain Rating 0.0, zero backlinks. The migration fixed (1): the marketing site is now native HTML, content-first, no iframe. The backlink plan at the end addresses (2), the bigger lever for a young domain.

> **Heads-up — the `my.` subdomain was abandoned.** Earlier drafts of this runbook pointed the Wix app at `my.ten-fifty5.com`. Wix Studio refuses plain subdomains, so the app stayed on its free `info5945780.wixstudio.com/online-tennis-analyt` URL and `www`/apex went to Render. All marketing "Log in / Start free" CTAs point at `…/portal` on that wixstudio URL. In Wix Domains, ignore the cosmetic "domain points away from Wix" warning — **never click "Try Again"** (it reverts `www` to Wix).

---

## How it's served (canonical: `locker_room_app.py`)

`_is_marketing_host()` checks `request.host` against `MARKETING_HOSTS` (`www.ten-fifty5.com` / `ten-fifty5.com`, extendable via the `MARKETING_HOSTS` env var). On a marketing host, `/` → `home.html` and `/pricing` → `pricing_public.html`; on every other host (the onrender URL the Wix portal embeds) those two paths are the unchanged **app** pages. Every other marketing path is a pure addition (harmless on the app host).

`marketing_app.py` is a standalone-service variant of the same site and is **NOT** wired into `render.yaml` — `locker_room_app.py` is the deployed path. Don't edit `marketing_app.py` expecting it to ship.

### Marketing routes

| Route | File | Notes |
|---|---|---|
| `/` | `home.html` | marketing host only; else app dashboard |
| `/overview` | `how_it_works.html` | "How It Works" |
| `/pricing` | `pricing_public.html` | marketing host only; else app pricing |
| `/coaching` | `for_coaches.html` | |
| `/academies` | `for_academies.html` | **added 2026-06-15** |
| `/contact-us` | `contact.html` | |
| `/blog`, `/post/<slug>` | generated `blog/*.html` | static blog (see below) |
| `/blog/images/<f>` | `blog/images/*` | per-article hero/thumbnail images |
| `/favicon.svg` · `/favicon.ico` · `/favicon.png` · `/apple-touch-icon.png` | `frontend/*` | brand favicon (tennis-ball mark) |
| `/og/<f>` | `frontend/og/*` | per-page 1200×630 social-share cards |
| `/robots.txt` · `/sitemap.xml` | generated | sitemap auto-includes every marketing route + blog post |
| (any unknown path) | `404.html` | **branded 404** for browsers; JSON for `/api`·`/ops` + JSON clients |

Legacy same-origin backups still exist (`/home`, `/how-it-works`, `/pricing-public`, `/for-coaches`, `/for-academies`); their canonicals point at the clean URLs so they aren't treated as duplicates.

### Config reference (no env vars required — code defaults cover it)
| Env var | Default (in code) | When to set |
|---|---|---|
| `MARKETING_HOSTS` | `www.ten-fifty5.com,ten-fifty5.com` | only if the marketing host differs |
| `SITE_BASE_URL` | `https://www.ten-fifty5.com` | used in robots/sitemap output |
| `APP_BASE_URL` | `https://info5945780.wixstudio.com/online-tennis-analyt` | reference only — the portal CTAs are hardcoded in the HTML |

The "Start free / Log in" CTAs are hardcoded `…wixstudio.com/online-tennis-analyt/portal` links in `frontend/{home,how_it_works,pricing_public,for_coaches,for_academies,contact}.html` and `build_blog.py`. If the app URL ever changes, find-replace there.

---

## The static blog (`build_blog.py`)

Dependency-free generator (no framework). **Publish a post:**
1. Drop `frontend/blog/_posts/<slug>.md` with frontmatter `title` / `description` / `date` (and optional `image: /blog/images/<file>` for a hero + index thumbnail).
2. Run `.venv/Scripts/python build_blog.py`.
3. Commit the generated `frontend/blog/*.html` (+ any image) and push.

Each post gets Article + BreadcrumbList JSON-LD, Open Graph (its own hero image as the OG card, else the homepage card), a canonical at `/post/<slug>`, the shared nav + footer, a skip link, and is auto-added to the sitemap. The Markdown supports `##`–`####` headings, lists, `**bold**`, `*italics*`, `[links]()`, and pipe tables.

---

## Shared design system + components

Every marketing surface (6 HTML pages + the blog templates) carries:
- a **shared sticky top-nav** — identical markup/CSS, centered links (Home · How It Works · Pricing · For Coaches · Academies · Blog · Contact), "Start Free" CTA, a 980px hamburger breakpoint, and a tiny script that highlights the current page (`aria-current`-style `.active`).
- a **shared dark footer** — Product + Get-in-touch columns, same link set.
- the locker-room palette (`--green #1a5c2e` …) + Inter, unified **1200px** content width, WCAG-AA contrast, lossless/near-lossless WebP imagery, `:focus-visible`, `prefers-reduced-motion`, and a skip-to-content link.

The system is **duplicated per file by convention** (no shared CSS). A site-wide colour/width/nav change is an N-file edit — the cross-page scripts in `.claude/tmp/` during the 2026-06-15 polish are the pattern (write once, apply to all, verify). See also `frontend/README.md`.

---

## On-page audit — status (all ✅)

| # | Item | Status |
|---|---|---|
| 1 | Native content (no iframe) | ✅ Whole site is native HTML on Render |
| 2 | SEO copy in main flow | ✅ Content *is* the page, top-down with headings |
| 3 | JSON-LD schema | ✅ Organization + FAQPage (home), Product + Offers + FAQ (pricing), ContactPage (contact), WebPage/Service + BreadcrumbList (overview/coaching/academies), Article + BreadcrumbList (every post) |
| 4 | Social cards | ✅ Per-page dedicated 1200×630 OG images (`/og/*`); posts use their hero |
| 5 | Favicon / touch icon | ✅ Brand SVG + .ico + apple-touch (ends the silent `/favicon.ico` 404) |
| 6 | Branded 404 | ✅ `404.html` for humans; JSON for API/ops |
| 7 | Crawl infra (robots, sitemap, canonical) | ✅ Generated; self-canonicals; `/academies` + all posts in sitemap |
| 8 | Accessibility | ✅ Skip links, ARIA tabs + FAQ accordions, 44px tap targets, focus states |
| 9 | Performance | ✅ WebP imagery, LCP hints, dropped unused Fraunces font, dead CSS/JS culled |
| 10 | Linked blog hub | ✅ `/blog` + 7 posts, linked in every footer |
| — | Off-page (DR 0.0) | ⏳ Owner task — see below + `docs/seo_backlink_kit.md` |

---

## Off-page: the real traffic lever (owner task, not code)

On-page is fixed, but a domain with **zero backlinks** won't rank for competitive terms no matter how clean the HTML. This is the bigger job for a young domain. Full kit (listing copy, directories, outreach templates) in `docs/seo_backlink_kit.md`.

### Backlink hit-list
1. **Tool directories** — AlternativeTo, Product Hunt, SaaS / "AI tools" / sports-tech directories.
2. **SwingVision-alternative space** — pursue inclusion on roundups ("best tennis analysis apps", "SwingVision alternatives"); the migrated comparison post is the asset to pitch.
3. **Tennis communities** — r/tennis, tennis forums, coaching groups: be useful, link where it adds value.
4. **Coach / academy partnerships** — every Coach Pro user is a potential link from their club/academy site.
5. **Local business listings** — Google Business Profile, local sports directories.
6. **Guest posts / mentions** — data-driven guest article → link back.

### Keyword strategy — winnable long-tail first
Not head terms ("tennis analysis") yet. Target what the blog already covers: "how to read tennis serve placement zones", "tennis rally length analysis", "what is tennis match analysis", "swingvision alternative android", "analyse tennis serve with data". As authority builds, move up. Realistic: meaningful organic growth over 2–3 months, compounding with backlinks + content.

---

## History — how it went live (done, kept for reference)

Cutover happened 2026-06-15 in stages, reversible at each: (1) deploy host-aware routing to the existing `locker-room` service; (2) verify the Wix app on its wixstudio URL; (3) point `www` + apex DNS at the Render service (rollback = point DNS back to Wix); (4) Google/Bing Search Console — submit `sitemap.xml`, request indexing. The `/post/<slug>` + `/blog` URLs were preserved so prior blog rankings carried over.

---

# Backlink & off-page kit

# Ten-Fifty5 — Backlink & Off-Page Kit

**Goal:** raise Domain Rating from 0.0 by earning relevant, legitimate backlinks. On-page SEO is done (`docs/seo_marketing_migration.md`); this is the engine that actually drives organic traffic for a young domain.

> **Honest expectations.** Quality + relevance beat volume. A handful of links from real tennis/AI sites is worth more than 100 spammy directories (which can *hurt*). Results show in **2–3 months**, compounding. Never buy links, use link farms/PBNs, or mass-blast irrelevant directories — Google penalises it.

---

## Reusable listing copy (paste into any directory/profile)

**Name:** Ten-Fifty5
**URL:** https://www.ten-fifty5.com
**Tagline:** Pro-level tennis match analysis from any camera. First match free.

**Short description (~30 words):**
> AI tennis match analysis from any camera. Upload one match and get serve heatmaps, rally patterns, an AI coach, and 18 KPIs of pro-level data in hours. First match free, no app required.

**Long description (~120 words):**
> Ten-Fifty5 is AI-powered tennis match analysis for serious recreational players, junior competitors, and their coaches. Record one match on any camera — phone, GoPro, DSLR, or a club's fixed camera — upload it, and within a couple of hours get pro-level analysis automatically: serve placement heatmaps and zone accuracy, rally-length patterns, attack-vs-defence balance, depth control, winners and errors by stroke, and biomechanical technique breakdowns. Every match produces 450+ data points across 18 KPIs, presented as a clean dashboard with an AI coach that reads your specific data and tells you what to work on next. No special hardware, no Apple lock-in, works on any device. Your first match is free — no credit card. Coaches get multi-player dashboards; the first linked player is free forever.

**Categories / tags:** Tennis · Sports Analytics · AI Tools · Sports Tech · Video Analysis · Coaching · SaaS

**Key features (bullets):**
- Any camera — phone, GoPro, DSLR, club cameras (no Apple lock-in)
- 450+ data points / 18 KPIs per match
- Serve placement heatmaps + zone accuracy
- Rally-length distribution & winning/losing patterns
- Biomechanical technique analysis (17-keypoint pose)
- AI coach grounded in your own match data
- Longitudinal progress tracking
- Coach/academy multi-player dashboards

**Pricing:** Free first match · pay-as-you-go from $25/match · monthly $25–$70 · Coach Pro $50/mo.

---

## Tier 1 — Directory submissions (free, high relevance — do these first)

| Site | Where | Why / Note |
|---|---|---|
| **AlternativeTo** | alternativeto.net (Add application) | List Ten-Fifty5 and tag it an **alternative to SwingVision**. Highly relevant audience + real backlink. |
| **There's An AI For That** | theresanaiforthat.com (submit) | Big AI-tools directory. Free listing (paid fast-track exists — skip it). |
| **Futurepedia** | futurepedia.io (submit a tool) | AI tools directory; category Sports/Other. |
| **SaaSHub** | saashub.com (submit) | Software directory; also add as a SwingVision alternative. |
| **Crunchbase** | crunchbase.com | Create a company profile (free). Trusted domain, real link. |
| **Product Hunt** | producthunt.com | **Plan a launch** (don't rush). Tue–Thu, prep a gallery + first comment. One-time spike + lasting link. |
| **Google Business Profile** | business.google.com | If you operate as a business/locale — adds a citation. |

**Process:** open each, create an account, paste the listing copy above, add the logo/screenshot, submit. ~10 min each. Spread over a few days.

---

## Tier 2 — Outreach (the highest-quality links)

Find targets by searching: `best tennis analysis app`, `SwingVision alternative`, `tennis stats app`, `AI tennis coach` — note the blogs/roundups that rank. Email the author. Your migrated comparison post (`/post/swingvision-alternative-ten-fifty5-comparison`) is your pitch asset.

### Template A — roundup / blog author
> **Subject:** A tennis-analysis tool for your [SwingVision alternatives] piece
>
> Hi [Name],
>
> I came across your article on [tennis analysis apps / SwingVision alternatives] — genuinely useful rundown, especially the part on [specific detail].
>
> I run Ten-Fifty5 (ten-fifty5.com), an AI tennis match-analysis platform that's a real alternative to the camera-locked apps: **any** camera works (phone, GoPro, Android, club cameras — no Apple lock-in), you get 450+ data points per match including serve heatmaps, rally analysis and biomechanical technique, plus an AI coach grounded in your own data. First match's free.
>
> If you ever refresh the piece, we'd slot in naturally alongside [SwingVision etc.]. Happy to give you free access to try it on one of your own matches — no strings. Either way, thanks for the helpful write-up.
>
> Cheers,
> Tomo — ten-fifty5.com

### Template B — coach / academy
> **Subject:** Free match analysis for your players
>
> Hi [Name],
>
> I built Ten-Fifty5 (ten-fifty5.com) — AI match analysis from any camera, made for coaches as much as players. You get a multi-player dashboard, development tracking, and an AI coach grounded in each player's data. **Your first linked player is free, forever.**
>
> Would love for you to try it with one of your players. And if it earns its place in your toolkit, a mention/link from your academy site would mean a lot to a small team like ours.
>
> Thanks,
> Tomo — ten-fifty5.com

### Template C — guest post pitch (tennis blogs)
> **Subject:** Guest piece: "What your tennis serve data actually tells you"
>
> Hi [Name],
>
> Big fan of [blog]. I write about tennis analytics at ten-fifty5.com and could contribute an original, data-driven piece your readers would like — e.g. "Reading your serve placement zones (T, wide, body)" or "Rally-length analysis: where club players really lose points." Free, exclusive to you, with one contextual link back. Interested?
>
> Tomo — ten-fifty5.com

---

## Tier 3 — Community & referral (drives visitors; mostly nofollow but still valuable)

- **Reddit r/tennis** — be useful first. Answer "how do I analyse my matches?" / "SwingVision alternatives?" threads genuinely; mention the tool only where it truly fits. Don't drop links cold (instant downvotes/removal).
- **TalkTennis (Tennis Warehouse forum)** — same value-first approach in gear/training subforums.
- **Tennis Facebook groups / Discords** — share a genuinely useful free analysis as a teaser.
- **Your blog** — keep publishing weekly (the generator's ready). Each post is a new indexable page + an asset to pitch.

---

## Tracking (simple)

Keep a sheet: Target | Type | URL | Date contacted/submitted | Status (live / pending / declined) | Link URL.
Aim: **3–5 Tier-1 directories this week**, **2–3 outreach emails/week** ongoing.

## What NOT to do
- ❌ Buy backlinks / "1000 links for $5" gigs.
- ❌ Mass-submit to irrelevant/spam directories.
- ❌ PBNs, link exchanges at scale, comment-spam.
- ❌ Over-optimised anchor text (keep it natural: "Ten-Fifty5", "ten-fifty5.com", "this tennis analysis tool").

---

# Klaviyo — Trial→Paid flow

# Klaviyo — Trial→Paid Flow (copy + build spec)

_Owner: Cowork. Source of truth for naming: `../contracts/events.md` + `../contracts/lifecycle_stages.md`._
_Status: copy ready. NOT live — see "Go-live dependencies" at the bottom (Claude Code + setup tasks)._
_Last updated: 2026-06-16._

This is the **trial→paid machine**: two connected flows that take a person from signup to their
free first match to a paying plan, automatically. All triggers use canonical event names from
`events.md`. All audience rules use stages from `lifecycle_stages.md`.

**Hard rules honoured:** marketing email goes only to contacts with `marketing_opt_in = true`
(opt-in). The contact is always the **account owner / parent** — never a minor. No biometric/pose
data, no child PII, no match video is referenced or used as a trait. Match KPIs (aces, serve %)
are derived analytics and are safe to personalise with *if* fed as profile traits (optional, below).

Sender voice: confident, data-first, second person, short sentences. "Stop guessing." "Bring the
data, we'll find the edge." No hype, no emojis, specific numbers over adjectives.

---

## FLOW 1 — Welcome & Activation

**Goal:** get the new signup to upload their free first match and open the report (reach
`activated`). This is the biggest single drop-off point in any free-trial SaaS.

- **Trigger:** metric `account_created`
- **Flow filter:** `marketing_opt_in` is true · person `role` is player OR parent (coaches → separate coach flow) · has NOT `match_uploaded` since starting the flow
- **Exit on:** `match_uploaded` (they've activated — hand off to Flow 2 after they view the report)

### Email 1.1 — immediate
- **Subject:** Your first match analysis is on us
- **Preview:** One upload. 450+ data points. No card needed.
- **Body:**
  > Welcome to Ten-Fifty5.
  >
  > Here's how it works — three steps, about 20 minutes of your time:
  >
  > **1. Record one match.** Phone at the back of the court, club cam, any MP4.
  > **2. Upload it.** We track every shot, bounce and pose — 450+ data points.
  > **3. Read your game.** Serve heatmaps, rally breakdowns, technique scores. Ready in 1–2 hours.
  >
  > Your first match is free. No credit card.
  >
  > **[Analyse my first match →]**
  >
  > Bring the data. We'll find the edge.

### Delay: 1 day → (if still no `match_uploaded`)

### Email 1.2 — friction-buster
- **Subject:** Still sitting on your first match?
- **Preview:** Any camera works. One MP4 is all we need.
- **Body:**
  > Quick one — your free analysis is still waiting.
  >
  > The only thing we need is a single video of one match: one fixed camera at the back of the
  > court, roughly baseline height, both players in frame. Most club and indoor cameras already
  > produce exactly this. iPhone, Android, DSLR — all fine.
  >
  > Upload it and in an hour or two you'll see where your free points are hiding.
  >
  > **[Upload my match →]**

### Delay: 2 days → (if still no `match_uploaded`)

### Email 1.3 — what you'll see / proof
- **Subject:** What one match actually tells you
- **Preview:** The stat that ended a two-year losing streak.
- **Body:**
  > Most players guess at what's going wrong. One analysed match replaces the guessing with a map:
  >
  > — **Serve placement** by zone, with win-rate on each (wide / body / T).
  > — **Rally length** vs win-rate — exactly where your points fall apart.
  > — **Technique** scored frame-by-frame against beginner → pro.
  >
  > One of our players put it simply: *"One look at my rally-length heatmap told me I was hitting
  > middle under pressure. Fixed in a fortnight."*
  >
  > Your first match is still free.
  >
  > **[See my game in data →]**

---

## FLOW 2 — Trial→Paid Conversion

**Goal:** convert an activated free-trial player into a subscription or PAYG purchase.
Starts once they've *seen the value* (viewed their free report). Default first nudge is **the day
after** the report is viewed — let it land first.

- **Trigger:** metric `report_viewed` (first occurrence)
- **Flow filter:** `marketing_opt_in` is true · stage is `trial` (free match used, no active sub, no PAYG credits) · has `subscription_started` zero times · has `credit_purchased` zero times
- **Exit on:** `subscription_started` OR `credit_purchased`

### Delay: 1 day after report viewed

### Email 2.1 — the gap
- **Subject:** You've seen one match. Here's what you're not seeing yet.
- **Preview:** One match is a snapshot. Your game is a trend.
- **Body:**
  > Now you've seen what a single match reveals. But one match is a snapshot — your real edge is in
  > the **pattern across matches**.
  >
  > On a paid plan, every match you upload stacks into one picture:
  >
  > — Your 18 KPIs **trending** against your own baseline, match after match.
  > — Technique scored over time — is your kinetic chain actually improving?
  > — And your **AI Coach**, unlocked — ask anything about how *you* play, answered from your own data.
  >
  > Plans start at $25/mo. Every plan includes match analysis, technique and unlimited AI Coach —
  > no feature tiers, no surprise paywalls.
  >
  > **[See plans →]**

### Delay: 2 days → (exclude anyone who converted)

### Email 2.2 — the AI Coach tease
- **Subject:** Ask your data why you lose the second set
- **Preview:** A tour coach, trained on your matches.
- **Body:**
  > Here's the kind of thing the AI Coach does once it's unlocked. A real exchange:
  >
  > *"Why am I losing the second set so often lately — is it fitness?"*
  >
  > *"Not fitness. Across your last 5 matches your 1st-serve % drops from 64% in set one to 52% in
  > set two, and your backhand errors jump 40%. It's decision quality under fatigue."*
  >
  > That answer is grounded entirely in your own stats — not generic coaching content. It's
  > included, unlimited, on every paid plan.
  >
  > **[Unlock my AI Coach →]**

### Delay: 3 days → (exclude converted)

### Email 2.3 — the long game
- **Subject:** Every match teaches you something. Don't let it fade.
- **Preview:** Your progression chart compounds. Your memory doesn't.
- **Body:**
  > Your coach moves on. Your stats fade. But a progression chart — every KPI, every match, every
  > month — compounds.
  >
  > That's the part no notebook and no memory can give you: proof you're actually getting better,
  > and early warning when a part of your game slips.
  >
  > - **Starter — $25/mo** · 3 matches/month
  > - **Standard — $40/mo** · 5 matches/month
  > - **Advanced — $70/mo** · 10 matches/month
  >
  > All include unlimited Technique and AI Coach. Cancel anytime — and you keep full read-access to
  > everything you've analysed.
  >
  > **[Choose my plan →]**

### Delay: 4 days → (exclude converted)

### Email 2.4 — last call + low-commitment option
- **Subject:** One more match? It's $25 — and your credits never expire.
- **Preview:** Not ready to subscribe? Pay as you go.
- **Body:**
  > If a monthly plan isn't right yet, you don't have to commit to one.
  >
  > Analyse a single match for **$25** — pay as you go, credits never expire, no subscription. Same
  > full analysis, same technique breakdown.
  >
  > And if you do subscribe later, everything you've already analysed is still there waiting.
  >
  > **[Analyse one match — $25 →]**  ·  **[Or see monthly plans →]**
  >
  > Bring the data. We'll find the edge.

---

## Optional personalisation (enhancement, not required)
If Claude Code feeds a **headline KPI** from `core.match.kpi_summary` as a profile/event trait
(e.g. `last_match_aces`, `last_match_first_serve_pct` — all derived analytics, NOT biometric), the
"your match" emails get sharper: _"12 aces — a personal best. Imagine that tracked every match."_
Use a generic fallback when absent so the email still renders. **This is the only place match data
would enter Klaviyo, and only non-biometric KPIs.** Flagging for the contract.

## How this gets assembled in Klaviyo
The Klaviyo connector can manage templates and campaigns but **cannot create flows via API** —
flow assembly (trigger + delays + conditional splits) is done in Klaviyo's visual Flow Builder.
Two options: (a) I create the email **templates** in your account now and walk you click-by-click
through wiring the two flows, or (b) via Chrome I assemble the flows on your behalf while you watch.
Recommend (a).

---

## ⚠️ Go-live dependencies (this flow cannot send until these are done)

**For Claude Code (data + emission):**
1. **Profiles + events must feed into Klaviyo** (downstream of `core.*`, per briefing rule 3). This flow triggers on: `account_created`, `match_uploaded`, `report_viewed`, `subscription_started`, `credit_purchased`.
2. **Event emission status.** `core.usage_event` emits the canonical `match_uploaded` and `report_viewed` (the older singular `match_upload` / `report_view` spellings were reconciled to the `events.md` contract). Still **not** yet emitted: `account_created`, `subscription_started`, `credit_purchased` — these must be emitted before the corresponding Flow triggers work.
3. **Trait to sync per profile:** `first_name`, `email`, `marketing_opt_in`, lifecycle `stage`, and (optional) a non-biometric headline KPI. Keyed by `account.public_id` + email.

**For setup (Tomo / Cowork, no code):**
4. **Marketing-consent capture must exist.** `privacy_inputs.md` confirms no consent capture in prod yet. Marketing email is opt-in only — we cannot email anyone without `marketing_opt_in = true`. This is a launch blocker for sending.
5. **Sending domain authentication** (SPF/DKIM/DMARC) in Klaviyo, and **set a default sender email** (currently blank in the account — likely `hello@` or `coach@ten-fifty5.com`).
6. **Add a physical postal address** in Klaviyo account settings (legally required in the email footer; currently blank).
7. **Don't duplicate the transactional "your match is ready" email** — that stays in SES (`match_processed`). Klaviyo only listens to that event for timing; it does not resend it.

---

# Klaviyo — Coach onboarding flow

# Klaviyo — Coach Engagement Flow (copy + build spec)

_Owner: Cowork. Source of truth for naming: `../contracts/events.md` + `../contracts/lifecycle_stages.md`._
_Status: copy ready. NOT live — see "Go-live dependencies". Pairs with `trial_to_paid_flow.md` (player conversion)._
_Last updated: 2026-06-16. **Revised** to the player-initiated access model._

## ‼️ Product truth this flow must respect
**A coach can NEVER add, enrol, or invite a player.** Access is always **player-initiated and
player-controlled**: the *player* invites the coach and the *player* grants and revokes access.
Therefore this flow contains **no enrolment, no "add a player", no "go get players" asks**. It is
**reminders + product-feature education + the Coach Pro upsell only**, triggered by access the
player has already granted.

**Hard privacy rules:** marketing email only to coaches with `marketing_opt_in = true`; coach is an
adult; **no minor PII or biometric data** about players ever enters Klaviyo — players are referenced
only abstractly ("a player", "your roster"), never a child's name/DOB/pose data.

Voice: "Coach with data." Confident, practical, coach-to-coach. Every CTA points at viewing/using
what they already have access to — never at acquiring players.

---

## Coach lifecycle (proposed — needs adding to `lifecycle_stages.md`)
| coach stage | entry condition |
|---|---|
| `coach_signup` | account_created role=coach, **no player has granted access yet** |
| `coach_active` | ≥1 player has granted access (`coach_accepted`) **and** coach has viewed that player's data |
| `coach_pro` | active Coach Pro subscription (unlimited players) |
| `coach_at_risk` | active/pro but no coach activity in 30+ days |

> ⚠️ Flag for Claude Code: confirm/add these to `lifecycle_stages.md`.

---

## FLOW 3 — Coach Engagement

### Optional orientation (cold self-signup coach, no access yet)
A coach who signs up before any player has linked them has nothing to view. We send **one** purely
informational email — **no ask to recruit players** (that's not something the coach can do):

- **Trigger:** `account_created` where `role = coach`
- **Email C0 — how it works**
  - **Subject:** How Ten-Fifty5 works for coaches
  - **Preview:** When a player shares their match data, it lands here.
  - **Body:**
    > Welcome to Ten-Fifty5.
    >
    > Here's how coaching with data works on our platform: when one of your players analyses a match
    > and **chooses to share it with you**, their full dashboard appears on your roster — serve and
    > rally breakdowns, technique scores, and an AI coach trained on their matches. Players control
    > what they share, always.
    >
    > Your first shared player is **free, forever**. We'll let you know the moment a player connects
    > with you.
  - _(No CTA to add/recruit players — none exists for a coach.)_
- Exit C0 once `coach_accepted` fires (a player grants access) → into the main flow below.

### Main flow — triggered when a player grants access
- **Trigger:** `coach_accepted` (a player has invited the coach / granted access)
- **Flow filter:** `marketing_opt_in` true · role is coach
- **Goal:** get the coach using the data they now have access to (→ `coach_active`)

#### Email C1 — immediate · a player connected with you
- **Subject:** A player just shared their game with you
- **Preview:** Their dashboard is live on your roster.
- **Body:**
  > Good news — a player has connected with you on Ten-Fifty5 and shared their match data.
  >
  > It's all on your roster now: serve placement, rally patterns, technique scored frame-by-frame,
  > and an AI coach trained on their actual matches. Open it and find where their next gain is hiding.
  >
  > **[Open my roster →]**
  >
  > _(Your first shared player is free for you, forever.)_

#### Delay 2 days → (if coach hasn't viewed the data)
#### Email C2 — feature: read the match like a coach
- **Subject:** The 3 views that change how you coach
- **Preview:** Serve zones, rally drop-off, technique scores.
- **Body:**
  > Once you're in your roster, three views do most of the work:
  > - **Serve placement** — where their serves land and win, by zone.
  > - **Rally analysis** — the exact rally length where their points fall apart.
  > - **Technique** — strokes scored frame-by-frame, kinetic chain sequenced.
  >
  > Spot the pattern, set the next session around it.
  >
  > **[Open my roster →]**

#### Delay 4 days
#### Email C3 — feature: the AI coach on their data
- **Subject:** Ask the data about any player
- **Preview:** A tour coach's read, grounded in their matches.
- **Body:**
  > Your roster includes an AI coach for every shared player — ask it anything and it answers from
  > *their* real stats: *"Why is this player losing the second set?" "Where are the free points on
  > their serve?"* It's grounded in their matches, not generic tips. Unlimited, on every shared player.
  >
  > **[Try it on a player →]**

> Coach becomes `coach_active` once they've viewed a shared player's data.

---

## Coach Pro upsell (sub-flow) — also player-driven
Per the product: a coach's **first** shared player is free forever; when a **second** player grants
access, that's the moment for Coach Pro (unlimited shared players, $50/mo).

- **Trigger:** 2nd `coach_accepted` for the same coach (i.e. active shared-player count reaches 2)
- **Flow filter:** `coach_active`, not already `coach_pro`
- **Email — go unlimited**
  - **Subject:** A second player connected — time to go unlimited
  - **Preview:** Coach Pro: every player who shares with you, one price.
  - **Body:**
    > More of your players are sharing their data with you — that's the whole point.
    >
    > **Coach Pro ($50/mo)** opens your roster to **unlimited** shared players. Every one gets full
    > match analysis, technique scoring and their own AI coach; their credits still cover their
    > uploads. No choosing which players you can see.
    >
    > **[Upgrade to Coach Pro →]**

---

## Retention (reminders only)
- **Roster digest (monthly):** which shared players improved, who's gone quiet, biggest movers —
  pulls the coach back in. Keep it free of minor names (use the player's account display name only;
  never a child's name).
- **`coach_at_risk`** (30 days no activity) → "your players' latest matches are waiting" reminder.

---

## ⚠️ Go-live dependencies

**For Claude Code (events / traits):**
1. **`coach_accepted` already exists** in `events.md` — it's the real entry trigger (player granted access). ✅
2. **Active shared-player count per coach** needed to time the Coach Pro upsell (2nd grant). Suggested trait `coach_shared_player_count`, derived from `core.relationship` (coach_player, status=active). Not in the contract yet — please add.
3. **Access-revoked event** (player removes a coach) is worth adding (suggested `coach_access_revoked`) so we can suppress/branch — players control access and may revoke. Not in the contract yet.
4. A coach-attributable **`report_viewed`** (coach viewed a shared player's data) to mark `coach_active`. Confirm `report_viewed` can carry the viewer = coach.
5. **Profile traits to sync (coach, PII-only):** `first_name`, `email`, `marketing_opt_in`, `role=coach`, coach stage, shared-player count. No player minor data, ever.
6. **Coach lifecycle stages** (above) added to `lifecycle_stages.md`.

**For setup (same as the player flow):** marketing-consent capture; sender domain auth + default sender; postal address. Flows assembled in Klaviyo's Flow Builder (connector can't create flows) — I'll create templates and walk you through wiring, or do it via Chrome.

---

## How the assets fit together (corrected)
- **Players** drive everything: a player signs up, analyses matches (`trial_to_paid_flow.md`), and may
  invite a coach and grant access.
- **This coach flow** activates and retains a coach **once a player has connected them**, and upsells
  Coach Pro when a 2nd player connects. It never asks a coach to recruit players — coaches can't.
- Coach **acquisition** (getting coaches to create an account) is a separate, cold channel — and is
  **not** done through Klaviyo (permission-based only). See the cold-outreach plan.

---

# Klaviyo — Coach invite campaign

# Klaviyo — Coach Invite Campaign (copy + build notes)

_Owner: Cowork. A one-off **campaign** (broadcast), not a triggered flow._
_Status: draft created in Klaviyo 2026-06-16 for a self-test to info@ten-fifty5.com._

## Purpose
Invite a coach/academy to sign up for Ten-Fifty5 (free first linked player). Used here as a
**deliverability + experience test** sent to Tomo (NextPoint Tennis) so he can receive it, click
through, and join ten-fifty5.

## ⚠️ Important channel note
Klaviyo is for **permission-based** email. Use it for coaches who **opted in / signed up / were
invited in-product**. For **cold** outreach to coaches & academies who've never heard of us, use a
dedicated cold-email tool (Instantly, Smartlead, etc.) — sending cold lists through Klaviyo risks
the domain reputation and may breach Klaviyo's terms. This test send is to ourselves, so it's fine.

> **Note (corrected model):** coaches never add/enrol players — a *player* invites the coach and
> grants access. So this acquisition email gets a coach to **create a free account**; their players
> then choose to share match data with them. No "add a player" ask.

## Email copy
- **Subject:** Coach your players with the data the pros use
- **Preview:** When your players share their matches, you see everything.
- **From:** coach@ten-fifty5.com (label "Ten-Fifty5") — _sender must be verified before send_
- **Body:**
  > Hi {{ first_name|default:'Coach' }},
  >
  > Your players are already recording their matches. Ten-Fifty5 turns those videos into ATP-grade
  > analysis — serve placement, rally patterns, biomechanical technique scores — plus an AI coach
  > trained on each player's own data.
  >
  > How it works for coaches:
  > - Create your **free coach account**.
  > - When a player analyses a match and **shares it with you**, their full dashboard lands on your roster.
  > - Your **first shared player is free, forever**; go **unlimited on Coach Pro ($50/mo)** when more connect.
  >
  > Bring the data. We'll find the edge — for every player on your roster.
  >
  > **[Create my free coach account →]**  (links to the portal signup)

## To actually send the test (Tomo, ~2 min)
1. Klaviyo → **Settings → Email → set a default sender email** (e.g. coach@ten-fifty5.com) and verify it.
2. Open the draft campaign **"Coach Invite — TEST"** and either send to yourself or hit Send (audience is the default "Email List", which currently only contains you).
3. (If asked to confirm a double opt-in, click the confirmation — the default list is double-opt-in.)

## Build state in Klaviyo
- Template: "Coach Invite — Ten-Fifty5" (CODE editor).
- Campaign: "Coach Invite — TEST", audience = Email List (SeWC2n), draft.
- Recipient profile: info@ten-fifty5.com (Tomo / NextPoint Tennis), persona=coach.

## Reality on testing the *flows* vs this *campaign*
This campaign tests sending + the join experience. It does **not** test the trial→paid **flows** —
those are triggered automations that need Claude Code's event/profile feed into Klaviyo first
(see `trial_to_paid_flow.md` → Go-live dependencies). Joining ten-fifty5 won't auto-fire flow emails
until that pipe exists.

---

# Coach cold-outreach plan

# Coach Cold-Outreach Plan (built to scale)

_Owner: Cowork. Cold B2B outreach to tennis coaches & academies — **separate from Klaviyo** (which
is permission-only). Set up now, fire when ready._
_Last updated: 2026-06-16._

## The model this must respect
A coach can't add players — **players grant access**. So cold outreach doesn't sell "manage your
players here"; it sells: *create a free coach account, get your players to analyse a match and share
it with you, and coach from real data.* The coach becomes an advocate who pulls their players in.
First shared player is free forever; Coach Pro ($50/mo) when more connect.

## Why a separate setup (not Gmail, not Klaviyo)
- **Klaviyo** = permission/opt-in only. Cold lists there would breach terms + wreck deliverability.
- **Your main Gmail / ten-fifty5.com** = your real reputation. Never send cold volume from it.
- **Solution:** a dedicated cold-email tool on a **separate sending domain**, so cold sending can
  never damage your product/transactional email. This is the scale-safe foundation.

---

## 1. Infrastructure to set up (start now — warmup takes ~2–3 weeks)

**a. Buy a separate sending domain.** Something close to brand, e.g. `gettenfifty5.com`,
`tryten-fifty5.com`, or `ten-fifty5.net`. Cold email sends from this, never from `ten-fifty5.com`.

**b. Create 1–2 sending mailboxes on it** (e.g. `coaching@gettenfifty5.com`). Each mailbox safely
sends ~30–40/day, so 2 mailboxes ≈ 60–80/day at full ramp.

**c. Authenticate** SPF, DKIM, DMARC on the new domain (the tool walks you through it).

**d. Warm up** the mailboxes for 2–3 weeks before real sends (the tools automate this). **This is the
clock that matters — start it as soon as you've picked the domain.**

**e. Pick the tool.** All have built-in sequences, inbox rotation, warmup, and a light pipeline/CRM:
- **Instantly** — simplest, cheapest, great for solo. _Recommended starting point._
- **Smartlead** — more power/automation as you scale.
- **Apollo** — combines a B2B contact database + sending (useful if sourcing contacts is the bottleneck).

> The tool's built-in pipeline IS your outreach CRM for now — no HubSpot needed (per our earlier call).

---

## 2. Sender identity (keeps you anonymous)
Send under a **brand/role identity**, never your own name:
- From name: **"Ten-Fifty5 Coaching"** or a consistent team-style alias.
- Avoid impersonating a specific real named person (deceptive + risky). A brand/team alias is honest and fine.
- Signature: brand name, link to ten-fifty5.com, physical address (compliance), clear opt-out line.

---

## 3. Targeting — who, and how to build the list

**Who (best-fit first):**
1. **Independent high-performance / private coaches** — fast decisions, own their players' relationships.
2. **Tennis academies & junior development programs** — one signup → many players.
3. **Club head coaches / directors of tennis.**
4. **School & college tennis programs.**

**Beachhead:** pick ONE focused geography to start (so you can follow up and build local proof) —
e.g. your home market first, then expand to other English-speaking markets (US/UK/AUS) where the
USD pricing fits.

**Where to source contacts:**
- National federation / association coach directories (e.g. LTA, USTA, Tennis SA registers).
- Club & academy websites (coach/staff pages list emails).
- Google Maps ("tennis academy", "tennis coach" + city) → site → contact.
- LinkedIn / Sales Navigator; Instagram coach accounts → bio email.
- Apollo for bulk B2B contacts if you go that route.

**Hygiene (protects deliverability):** verify every email (the tools include verification) and skip
role-catchalls where possible.

---

## 4. The cold sequence (4 touches over ~2 weeks, plain text)
Cold email works **plain and personal**, not designed HTML. Short. One idea per email. Always one
soft CTA. Personalise the first line per recipient (club/academy name) — the tools support variables.

### Email 1 — Day 0 · the hook
> **Subject:** your players' matches, in pro-level data
>
> Hi {{first_name}},
>
> Quick one — your players are already recording their matches. Ten-Fifty5 turns one of those videos
> into the kind of analysis tour coaches build by hand: serve placement, rally patterns, and
> frame-by-frame technique scoring, plus an AI coach trained on that player's own data.
>
> You'd create a free coach account; when a player analyses a match and shares it with you, their
> full dashboard lands on your roster. Your first shared player is free, forever.
>
> Worth a look for {{company}}?
>
> — Ten-Fifty5 Coaching · ten-fifty5.com

### Email 2 — Day 3 · the thing you can't see by eye
> **Subject:** the serve fault six years of coaching missed
>
> Hi {{first_name}},
>
> One academy coach told us the technique breakdown showed a player's kinetic chain firing in the
> wrong order on the serve — something nobody had caught in six years courtside.
>
> That's the gap Ten-Fifty5 fills: it measures what the eye can't, every match, and trends it over a
> season so you can prove a player is improving. Free for your first shared player.
>
> Want me to send a sample report so you can see exactly what you'd get?
>
> — Ten-Fifty5 Coaching

### Email 3 — Day 7 · coach the ones you barely see
> **Subject:** like having you courtside — even at 200 miles
>
> Hi {{first_name}},
>
> If you coach players you only see once a week (or remotely), this is built for you: every match
> they share comes back as serve/rally breakdowns, technique scores and an AI coach grounded in
> their real patterns — so your session time goes on fixing, not guessing.
>
> Free to start with your first player. Happy to set you up — shall I send the link?
>
> — Ten-Fifty5 Coaching

### Email 4 — Day 12 · close the loop
> **Subject:** should I close the file?
>
> Hi {{first_name}},
>
> Haven't heard back, so I'll assume the timing's off — no problem. If it's ever useful to see your
> players' matches in real data, we're at ten-fifty5.com and the first player's free.
>
> Wishing your squad a strong season.
>
> — Ten-Fifty5 Coaching

_(Any reply → exit the sequence and respond personally. Replies are the goal, not opens.)_

---

## 5. Compliance (cold B2B)
- Identify the sender + include a **physical postal address** and a clear **opt-out** in every email.
- Target **business** addresses and keep it relevant (legitimate-interest basis). Honour every
  opt-out immediately and suppress.
- EU/UK + South Africa (POPIA): a solo coach's address can be personal data — keep volume sane,
  relevance high, and unsubscribes instant. If unsure on a market, check before blasting it.

---

## 6. Tracking & what "good" looks like
Use the tool's built-in pipeline. Stages: **Contacted → Opened → Replied → Signed up → Player connected.**
Watch: reply rate (aim ~5–10%+ on a tight list), account signups, and the multiplier — **players
connected per coach** (the number that makes this channel worth it).

## 7. Scale-later (when it's working)
- Add a **second channel touch** (a LinkedIn connect/message alongside the email) — multi-touch lifts replies.
- Give coaches a reason to push their players (e.g. spotlight/feature, or simply that it makes them look good).
- Feed signed-up coaches into the **Coach Engagement flow** (`../klaviyo/coach_onboarding_flow.md`) once they've opted in — cold tool hands off to Klaviyo at the opt-in line.

---

## Your immediate setup checklist
1. Buy the sending domain.
2. Create + authenticate 1–2 mailboxes on it; start warmup (the ~3-week clock).
3. Pick the tool (Instantly to start).
4. While it warms: I build the first target list approach + finalise copy with you.
5. Fire once warmup's done.
