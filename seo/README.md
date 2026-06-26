# seo/ — GSC-backed weekly SEO audit

Free, authoritative SEO automation built on **Google Search Console** (your own search data — no
paid tool). Produces a weekly report (performance + trend, striking-distance topic ideas, low-CTR
rewrite wins, top queries/pages) that the agent uses to pick and write the next blog post against
**real query demand** instead of volume guesses.

## What it gives you (and what it doesn't)

- ✅ Real queries you rank for, clicks/impressions/CTR/position, week-over-week movement.
- ✅ "Striking-distance" queries (position 5–20) — the highest-ROI next-post signal.
- ✅ Pages with high impressions + low CTR — title/meta rewrite opportunities.
- ❌ Search volumes for keywords you DON'T rank for yet, or competitor data. For that, add a market
  data source later (free Google Keyword Planner, or pay-as-you-go DataForSEO) — not required to start.

## One-time setup (credentials via Render env — nothing in git)

1. **Google Cloud Console** → create/pick a project → **APIs & Services → Enable** the
   **"Google Search Console API."**
2. **APIs & Services → Credentials → Create credentials → Service account.** Open it →
   **Keys → Add key → JSON** → download the file.
3. **Search Console** (search.google.com/search-console) → your property → **Settings → Users and
   permissions → Add user** → paste the service account's email
   (`name@project.iam.gserviceaccount.com`) → role **Full** (or Restricted/read).
4. In the **Render dashboard**, on the service that runs the cron, add two env vars:
   - `GSC_SERVICE_ACCOUNT_JSON` = the entire contents of the JSON key file (paste as one value).
   - `GSC_SITE_URL` = your property id — `sc-domain:ten-fifty5.com` for a domain property, or
     `https://www.ten-fifty5.com/` for a URL-prefix property (must match GSC exactly).

## Run

```bash
.venv/Scripts/python -m seo.weekly_seo                    # print the report
.venv/Scripts/python -m seo.weekly_seo --out seo/reports/latest.md
```

**Render Cron Job** (weekly): create a Cron Job with command `python -m seo.weekly_seo` and the two
env vars above. It self-gates — with the vars unset it prints a clear "not configured" notice and
exits cleanly, so it's safe to ship before setup.

## Dependency

`google-auth` (mints the service-account token); `requests` (already present) calls the REST API.
