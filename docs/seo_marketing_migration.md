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
