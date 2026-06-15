# SEO Marketing-Site Migration — Cutover Runbook + Off-Page Plan

**Status:** Stage 0 (build) COMPLETE in code, not yet deployed/cut over.
**Goal:** Serve the public marketing site as native HTML from Render at `www.ten-fifty5.com`, replacing the Wix-built marketing pages (Wix buries content in JavaScript → thin for Google). The logged-in app (login + portal + checkout) stays on Wix, moved to `my.ten-fifty5.com`. Dashboards are unchanged — they're already Render-hosted, embedded in the Wix portal shell.

> **Why this exists.** 6 months live, ~6 visitors/month. Two causes: (1) on-page — the live marketing pages render through Wix JS so the only solid crawlable text was a block pasted at the bottom of each page; (2) off-page — Domain Rating 0.0, zero backlinks. This migration fixes (1). The backlink plan at the end addresses (2), which is the bigger traffic lever for a young domain.

---

## What was built (Stage 0 — in the repo now)

- **`marketing_app.py`** — new Render service serving the indexed URLs natively: `/`, `/overview`, `/pricing`, `/coaching`, `/blog`, `/post/<slug>`, `/contact-us`, plus generated `/robots.txt` and `/sitemap.xml`. Old paths 301 (`/home`→`/`, etc.). Config: `SITE_BASE_URL`, `APP_BASE_URL` (env vars in `render.yaml`).
- **`render.yaml`** — new `marketing` web service (service #6).
- **Home / Overview / Pricing / Coaching** — hardened: canonical, Open Graph + Twitter cards, JSON-LD (Organization + FAQPage on home; Product + Offers on pricing), all "Start free" CTAs repointed to `my.ten-fifty5.com` (would have 404'd otherwise).
- **`frontend/contact.html`** — new Contact page (meta, og, ContactPage schema, consistent `info@ten-fifty5.com`).
- **`build_blog.py` + `frontend/blog/_posts/*.md`** — static blog generator + the 6 migrated posts at their original `/post/<slug>` URLs (Article + BreadcrumbList schema). Re-run `.venv/Scripts/python build_blog.py` after adding a post.

Everything is tested locally via the Flask test client. **Nothing is live until DNS is cut over (Stage 3).**

---

## Staged cutover — reversible at every step

### Stage 1 — Deploy the marketing service (no domain change, ZERO user impact)
1. Push to `main`. Render Blueprint sync creates the new `marketing` service from `render.yaml`.
2. Open the service's `onrender.com` URL. Verify `/`, `/overview`, `/pricing`, `/coaching`, `/blog`, a `/post/...`, `/contact-us`, `/robots.txt`, `/sitemap.xml`.
3. Run each page through [Google Rich Results Test](https://search.google.com/test/rich-results) — confirm Organization / FAQ / Product / Article parse with no errors.
   - *Live site untouched. Nothing to roll back.*

### Stage 2 — Stand up `my.ten-fifty5.com` for the Wix app (while `www` still works)
1. In **Wix**, add `my.ten-fifty5.com` as an additional/primary connected domain for the existing site (Wix dashboard → Domains). The login, `/portal`, and checkout now answer at `my.` *and* still at `www.`.
2. Confirm at `https://my.ten-fifty5.com/portal`: login works, dashboards load, a test checkout opens PayPal.
   - *Rollback: remove the `my.` domain in Wix. `www` unaffected.*

### Stage 3 — Point `www` (+ apex) at Render (GO LIVE)
1. In your DNS host, change `www.ten-fifty5.com` to point at the Render `marketing` service (Render shows the exact CNAME/target when you add the custom domain to the service). Add the custom domain `www.ten-fifty5.com` (and apex `ten-fifty5.com` → 301 to `www`) in the Render `marketing` service settings.
2. Wait for DNS propagation + Render's automatic TLS cert issue (minutes to ~1 hour).
3. Verify `https://www.ten-fifty5.com/` now serves the Render home page (View Source → your `<h1>` and full body are in the HTML; no Wix JS).
   - **Rollback (minutes): point `www` DNS back to Wix.** This is why Stages 1–2 happen first — the app already works at `my.` regardless.

### Stage 4 — Tell Google + clean up
1. **Google Search Console** → add/confirm `www.ten-fifty5.com`, submit `https://www.ten-fifty5.com/sitemap.xml`. Use URL Inspection → Request Indexing on `/`, `/overview`, `/pricing`, `/coaching`, `/blog`.
2. **Bing Webmaster Tools** → submit the same sitemap.
3. The `/post/<slug>` and `/blog` URLs are unchanged, so existing blog rankings carry over. Spot-check 2–3 in Search Console after a week for crawl errors.
4. Optional: `noindex` the `/coach-accept` page (transactional, token-auth) so it doesn't sit in the index.

### Config reference (set in Render `marketing` service)
| Env var | Value |
|---|---|
| `SITE_BASE_URL` | `https://www.ten-fifty5.com` |
| `APP_BASE_URL` | `https://my.ten-fifty5.com` |

If you choose a different app subdomain than `my.`, change `APP_BASE_URL`, then re-run `build_blog.py` and update the hardcoded `my.ten-fifty5.com/portal` CTA links in `frontend/{home,how_it_works,pricing_public,for_coaches,contact}.html` (find-replace).

---

## Audit items — status

| # | Item | Status |
|---|---|---|
| 1 | Move content out of iframe / native content | ✅ Whole site is native HTML on Render |
| 2 | SEO copy in main flow, not buried | ✅ Content *is* the page, top-down with headings |
| 3 | JSON-LD schema | ✅ Organization, FAQPage, Product+Offers, ContactPage, Article, BreadcrumbList |
| 4 | Contact page meta + social image | ✅ Built with meta + og/twitter |
| 5 | Small on-page (email, alt, titles) | ✅ Email consistent, decorative SVGs, clean titles |
| 6 | Crawl infra (robots, sitemap, canonical, 301) | ✅ Generated robots + sitemap; self-canonicals; apex→www 301 at Stage 3 |
| 7 | Linked blog hub | ✅ `/blog` hub + 6 posts migrated; linked in every footer |
| — | Off-page (DR 0.0) | ⏳ Owner task — see below |

---

## Off-page: the real traffic lever (owner task, not code)

On-page is now fixed, but a domain with **zero backlinks** won't rank for competitive terms no matter how clean the HTML. This is the bigger job for a young domain, and it's yours (with my help on content/outreach copy anytime).

### Backlink hit-list (start here)
1. **Tool directories / aggregators** — submit Ten-Fifty5 to: AlternativeTo, Product Hunt, SaaS directories, "AI tools" directories, sports-tech directories.
2. **SwingVision-alternative space** — you already appear in comparison content; pursue inclusion/links on roundup posts ("best tennis analysis apps", "SwingVision alternatives"). Your migrated comparison post is the asset to pitch alongside.
3. **Tennis communities** — relevant subreddits (r/tennis), tennis forums, Facebook coaching groups: be genuinely useful, link where it adds value (not spam).
4. **Coach / academy partnerships** — every coach who uses Coach Pro is a potential backlink from their club/academy site ("we use Ten-Fifty5 for match analysis").
5. **Local business listings** — Google Business Profile, local sports directories.
6. **Guest posts / mentions** — offer a data-driven guest article to tennis blogs; earn a link back.

### Keyword strategy — chase winnable terms first
Don't target head terms ("tennis analysis") yet — they're owned by established domains. Target **long-tail, low-competition** queries your blog already addresses:
- "how to read tennis serve placement zones"
- "tennis rally length analysis"
- "what is tennis match analysis"
- "swingvision alternative android"
- "analyse tennis serve with data"

These match the migrated posts. As authority builds via backlinks, move up to harder terms. Realistic timeline: meaningful organic growth over **2–3 months**, compounding as backlinks + content accumulate.

---

*Owner-only steps are confined to Stages 2–4 (Wix domain + DNS + Search Console) and the off-page plan. Everything else is shipped in code.*
