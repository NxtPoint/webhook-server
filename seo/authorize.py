# seo/authorize.py — ONE-TIME, run LOCALLY, to get a GSC OAuth refresh token (keyless auth).
#
# Use this instead of a service-account key when your org enforces
# iam.disableServiceAccountKeyCreation (Google "Secure by Default"). It opens a browser, you log in
# with the Google account that has access to the Search Console property, and it prints the three
# values to paste into the Render dashboard:
#     GSC_OAUTH_CLIENT_ID, GSC_OAUTH_CLIENT_SECRET, GSC_OAUTH_REFRESH_TOKEN
#
# Prerequisites (one-time, in Google Cloud Console — NONE of this creates a service-account key):
#   1. APIs & Services → Enable "Google Search Console API".
#   2. APIs & Services → Credentials → Create credentials → OAuth client ID → type "Desktop app".
#      (If prompted, configure the OAuth consent screen: External, add yourself as a Test user.)
#   3. Download that client's JSON (the "client secret" file — allowed by the policy).
#
# Run locally (needs a browser on this machine):
#     pip install google-auth-oauthlib
#     .venv/Scripts/python -m seo.authorize path/to/client_secret.json
#
# The refresh token is long-lived; store it in Render and you never run this again (unless revoked).

import sys

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m seo.authorize <client_secret.json>", file=sys.stderr)
        return 2
    client_secret = argv[0]
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing dependency. Run:  pip install google-auth-oauthlib", file=sys.stderr)
        return 3

    flow = InstalledAppFlow.from_client_secrets_file(client_secret, scopes=[SCOPE])
    # Loopback flow (Google deprecated the copy/paste OOB flow). Opens the browser; access_type
    # 'offline' + prompt 'consent' guarantees a refresh token comes back.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    print("\n" + "=" * 70)
    print("SUCCESS — set these three env vars in the Render dashboard:")
    print("=" * 70)
    print(f"GSC_OAUTH_CLIENT_ID      = {creds.client_id}")
    print(f"GSC_OAUTH_CLIENT_SECRET  = {creds.client_secret}")
    print(f"GSC_OAUTH_REFRESH_TOKEN  = {creds.refresh_token}")
    print("=" * 70)
    print("(plus GSC_SITE_URL = sc-domain:ten-fifty5.com  — or your URL-prefix property)")
    print("Keep these secret. Then run:  python -m seo.weekly_seo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
