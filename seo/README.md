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

Two auth modes. **Use OAuth** — it's keyless and works even when your org blocks service-account
key creation (the `iam.disableServiceAccountKeyCreation` "Secure by Default" policy).

### Mode A — OAuth refresh token (recommended, keyless)

1. **Google Cloud Console → APIs & Services → Enable "Google Search Console API."**
2. **Credentials → Create credentials → OAuth client ID → type "Desktop app."** (If asked,
   configure the OAuth consent screen: External, and add your Google account as a **Test user**.)
   Download that client's JSON — this is an *OAuth client secret*, NOT a service-account key, so the
   org policy does not block it.
3. **Search Console** → your property → **Settings → Users and permissions** → make sure the Google
   account you'll log in with has access (it's your own property, so it does).
4. Run the one-time browser login locally:
   ```bash
   pip install google-auth-oauthlib
   .venv/Scripts/python -m seo.authorize path/to/client_secret.json
   ```
   It prints `GSC_OAUTH_CLIENT_ID`, `GSC_OAUTH_CLIENT_SECRET`, `GSC_OAUTH_REFRESH_TOKEN`.
5. In the **Render dashboard**, add those three + `GSC_SITE_URL`
   (`sc-domain:ten-fifty5.com`, or `https://www.ten-fifty5.com/` for a URL-prefix property).

### Mode B — Service account (only if your org permits key creation)

Enable the API, create a Service Account → **Keys → Add key → JSON**, add its email as a user in
Search Console, then set `GSC_SERVICE_ACCOUNT_JSON` (full JSON) + `GSC_SITE_URL` in Render.
*Blocked by `iam.disableServiceAccountKeyCreation` — use Mode A if you hit that.*

## Run

```bash
.venv/Scripts/python -m seo.weekly_seo --all              # audit EVERY site in seo/sites.py
.venv/Scripts/python -m seo.weekly_seo --site nextpoint    # one registered site by slug
.venv/Scripts/python -m seo.weekly_seo                     # single site via GSC_SITE_URL env
.venv/Scripts/python -m seo.weekly_seo --all --out seo/reports   # write one .md per site
```

**Multi-site:** one Google account / OAuth token covers every verified property, so the console
audits all sites at once. Add a site = one line in `seo/sites.py` (after verifying it in Search
Console under the same Google account). No new credentials, no new deploy.

**Render Cron Job** (weekly): create a Cron Job with command `python -m seo.weekly_seo` and the two
env vars above. It self-gates — with the vars unset it prints a clear "not configured" notice and
exits cleanly, so it's safe to ship before setup.

## Dependency

`google-auth` (mints the service-account token); `requests` (already present) calls the REST API.
