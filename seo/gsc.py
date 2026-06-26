# seo/gsc.py — thin Google Search Console API client (service-account auth from a Render env var).
#
# Uses google-auth (to mint a bearer token from the service-account JSON) + requests (to call the
# Search Console REST API). No google-api-python-client dependency. Read-only scope.

import json
import os

import requests

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
_BASE = "https://searchconsole.googleapis.com/webmasters/v3"

# Env vars (set in the Render dashboard, NOT committed):
#   GSC_SERVICE_ACCOUNT_JSON — the full service-account JSON (one line/blob)
#   GSC_SITE_URL             — the GSC property, e.g. "sc-domain:ten-fifty5.com"
#                              or "https://www.ten-fifty5.com/"
_SA_ENV = "GSC_SERVICE_ACCOUNT_JSON"
_SITE_ENV = "GSC_SITE_URL"


class GSCNotConfigured(RuntimeError):
    """Raised when the GSC env vars are absent — callers degrade cleanly."""


def configured() -> bool:
    return bool(os.getenv(_SA_ENV) and os.getenv(_SITE_ENV))


def site_url() -> str:
    s = os.getenv(_SITE_ENV)
    if not s:
        raise GSCNotConfigured(f"{_SITE_ENV} is not set")
    return s


def _token() -> str:
    raw = os.getenv(_SA_ENV)
    if not raw:
        raise GSCNotConfigured(f"{_SA_ENV} is not set")
    try:
        info = json.loads(raw)
    except Exception as e:
        raise GSCNotConfigured(f"{_SA_ENV} is not valid JSON: {e}") from e
    # Imported lazily so the module imports even when google-auth isn't installed.
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def query(*, start, end, dimensions=("query",), row_limit=1000, filters=None,
          search_type="web", data_state="final"):
    """One Search Console searchAnalytics.query call. Returns the list of rows (each row has
    `keys` aligned to `dimensions`, plus clicks / impressions / ctr / position)."""
    site = site_url()
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
