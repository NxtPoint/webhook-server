# seo/gsc.py — thin Google Search Console API client (service-account auth from a Render env var).
#
# Uses google-auth (to mint a bearer token from the service-account JSON) + requests (to call the
# Search Console REST API). No google-api-python-client dependency. Read-only scope.

import json
import os

import requests

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
_BASE = "https://searchconsole.googleapis.com/webmasters/v3"

# Env vars (set in the Render dashboard, NOT committed). Two auth modes — OAuth is preferred and
# is the path to use when an org policy blocks service-account key creation
# (iam.disableServiceAccountKeyCreation):
#
#   OAuth user creds (recommended, keyless):
#     GSC_OAUTH_CLIENT_ID      — OAuth 2.0 client id
#     GSC_OAUTH_CLIENT_SECRET  — OAuth 2.0 client secret
#     GSC_OAUTH_REFRESH_TOKEN  — refresh token from the one-time `python -m seo.authorize` flow
#
#   Service account (only if your org allows key creation):
#     GSC_SERVICE_ACCOUNT_JSON — the full service-account JSON
#
#   Always:
#     GSC_SITE_URL             — the GSC property, e.g. "sc-domain:ten-fifty5.com"
#                                or "https://www.ten-fifty5.com/"
_SA_ENV = "GSC_SERVICE_ACCOUNT_JSON"
_SITE_ENV = "GSC_SITE_URL"
_OA_ID = "GSC_OAUTH_CLIENT_ID"
_OA_SECRET = "GSC_OAUTH_CLIENT_SECRET"
_OA_REFRESH = "GSC_OAUTH_REFRESH_TOKEN"

TOKEN_URI = "https://oauth2.googleapis.com/token"


class GSCNotConfigured(RuntimeError):
    """Raised when the GSC env vars are absent — callers degrade cleanly."""


def _oauth_ready() -> bool:
    return bool(os.getenv(_OA_ID) and os.getenv(_OA_SECRET) and os.getenv(_OA_REFRESH))


def auth_ready() -> bool:
    """True when credentials are present (independent of any single GSC_SITE_URL) — used by the
    multi-site console, which supplies each property explicitly from seo/sites.py."""
    return _oauth_ready() or bool(os.getenv(_SA_ENV))


def configured() -> bool:
    return bool(auth_ready() and os.getenv(_SITE_ENV))


def site_url() -> str:
    s = os.getenv(_SITE_ENV)
    if not s:
        raise GSCNotConfigured(f"{_SITE_ENV} is not set")
    return s


def _token() -> str:
    # google-auth imported lazily so the module imports even when it isn't installed.
    from google.auth.transport.requests import Request
    # Preferred: OAuth user credentials (keyless — works under the no-key-creation org policy).
    if _oauth_ready():
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=None,
            refresh_token=os.getenv(_OA_REFRESH),
            client_id=os.getenv(_OA_ID),
            client_secret=os.getenv(_OA_SECRET),
            token_uri=TOKEN_URI,
            scopes=[SCOPE],
        )
        creds.refresh(Request())
        return creds.token
    # Fallback: service-account key (only if your org permits key creation).
    raw = os.getenv(_SA_ENV)
    if not raw:
        raise GSCNotConfigured(
            f"set either the OAuth vars ({_OA_ID}/{_OA_SECRET}/{_OA_REFRESH}) or {_SA_ENV}")
    try:
        info = json.loads(raw)
    except Exception as e:
        raise GSCNotConfigured(f"{_SA_ENV} is not valid JSON: {e}") from e
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def query(*, start, end, dimensions=("query",), row_limit=1000, filters=None,
          search_type="web", data_state="final", site=None):
    """One Search Console searchAnalytics.query call. Returns the list of rows (each row has
    `keys` aligned to `dimensions`, plus clicks / impressions / ctr / position).

    `site` overrides the GSC property for this call (multi-site); defaults to GSC_SITE_URL."""
    site = site or site_url()
    url = f"{_BASE}/sites/{requests.utils.quote(site, safe='')}/searchAnalytics/query"
    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": list(dimensions),
        "rowLimit": int(row_limit),
        "type": search_type,
        "dataState": data_state,
    }
    if filters:
        # filters: list of {"dimension","operator","expression"}
        body["dimensionFilterGroups"] = [{"filters": filters}]
    r = requests.post(url, headers={"Authorization": f"Bearer {_token()}"},
                      json=body, timeout=30)
    r.raise_for_status()
    return r.json().get("rows", [])
