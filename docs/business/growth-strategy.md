# Growth Strategy & Roadmap (Cowork)

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)).
> **Owner: Cowork.** Forward-looking growth strategy + roadmap. This doc deliberately holds only
> what isn't already operationalised elsewhere; it **defers to the living docs** for specifics:
> growth/CRM status → [`growth-and-crm.md`](growth-and-crm.md); Klaviyo flows + outreach + site/SEO →
> [`marketing-and-seo.md`](marketing-and-seo.md); privacy/consent → [`privacy-and-consent.md`](privacy-and-consent.md).
> If anything here ever conflicts with those, **they win.** Last updated: 2026-06-18.

## Operating constraints (shape every choice here)
- Solo founder, **evenings + weekends only**; everything must be automation-first.
- Founder stays **anonymous** — **no face-on-camera content**; brand/role identities only.
- Lean stack, single source of truth: **we are our own CRM** (`core.*`/`billing.*` + cockpit);
  **Klaviyo is the only marketing destination**; HubSpot deprecated-but-dormant.

## The three machines (priority order)
1. **Conversion** (highest ROI, fully automatable) — turn free-first-match users into subscribers.
   Built as Klaviyo lifecycle flows. Spec: `marketing-and-seo.md` (Trial→Paid flow). This is where
   money leaks first, so it's priority one.
2. **Retention** — the progression chart is the moat; monthly "your trend" emails + 30-day
   re-engagement (Klaviyo). NPS/feedback capture is already built in-product (`marketing_crm/feedback/`).
3. **Acquisition** — get qualified tennis players + coaches to the free first match.

## Acquisition channels (ranked for this business)
1. **SEO + content** — already running (weekly audit + auto-blog). Sharpen toward buyer-intent
   long-tail; off-page/backlinks are the real lever for a young domain. Detail: `marketing-and-seo.md`.
2. **GEO / AI-visibility** — be the named answer when players ask ChatGPT/Claude/Perplexity "best AI
   tennis analysis". Structured content + presence on cited sources. Low effort, few competitors. _(Not
   yet started — Cowork lane.)_
3. **Faceless short-form video** — the anonymous-friendly social play; the *data and footage* are the
   content, not the founder. Screen-recorded dashboard breakdowns, anonymised clips with stat
   overlays, AI-presenter (HeyGen/Captions) as a brand host. Batch + schedule (Metricool/Buffer).
   3–5 clips/week. _(Not yet started — Cowork lane.)_
4. **Communities** — r/tennis, TalkTennis, coaching groups; value-first, link where it fits.
5. **Coach / academy outreach (cold)** — high-ceiling: one coach → many players. Runs on a **separate
   sending domain + cold-email tool (Instantly)**, NOT Klaviyo. Full plan + sequence + sample list:
   `marketing-and-seo.md` (Coach Cold-Outreach) + `marketing_crm/outreach/`. **Model guardrail:** a
   coach can never add players — players grant access; outreach sells "create a free account, your
   players share their matches with you."
6. **Referral loop** — give-a-match/get-a-match. Design exists (README §11.3); **not built**.
7. **Paid ads** — only once the funnel converts and the rate is known. Adspirer connected.

## What's already built (don't re-plan — see `growth-and-crm.md`)
Funnel/page tracking, cockpit, feedback+NPS capture, consent write-path, CRM-sync feed to Klaviyo,
Clerk auth, direct PayPal. So the "foundations/analytics" tier of the original plan is largely **done**;
remaining foundation work is: ad pixels (for later paid), reviews capture, and legal sign-off on the
privacy/consent docs (the gate for any marketing send).

## 30 / 60 / 90 (re-baselined 2026-06-18)
- **Now → 30d:** get the Klaviyo flows live — finish the go-live gates (Klaviyo key live + sync on,
  sender-domain auth + sender + postal address, consent legal sign-off → real `marketing_opt_in`).
  Assemble Trial→Paid + Coach Engagement flows in Flow Builder. Reviews capture.
- **30 → 60d:** launch faceless short-form video (batched/scheduled); ship GEO/AI-visibility content;
  start coach cold-outreach (domain warmed); begin community participation.
- **60 → 90d:** referral loop; small paid-ads test against the known conversion rate; double down on
  whichever channel is actually producing free uploads (the tracking will show it).

## Cowork ⇄ Claude Code lane (recap)
Claude Code builds the pipes + data (events, `core.*`, the Klaviyo feed, in-app widgets, transactional
SES email). **Cowork** designs/writes/runs everything inside Klaviyo (flows, templates, timing,
segments) + social/SEO/GEO/outreach/analysis. Cowork never writes code (git-hook enforced). Full lane
table: `growth-and-crm.md`.
