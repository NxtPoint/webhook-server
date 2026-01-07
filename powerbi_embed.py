# ==================================================================================================
# powerbi_embed.py
# ==================================================================================================
# PURPOSE
# -------
# Handles Power BI REST API calls for embedding + refresh.
#
# What this module does:
# - Obtains OAuth access tokens (client credentials flow) for Power BI REST API
# - Resolves workspace/report/dataset IDs from environment variables
# - Triggers dataset refresh
# - Generates embed tokens for Wix (supports future RLS identities)
#
# What this module does NOT do:
# - It does NOT talk to Azure Resource Manager (capacity pause/resume is separate).
#
# REQUIRED ENV VARS
# -----------------
#   PBI_TENANT_ID
#   PBI_CLIENT_ID
#   PBI_CLIENT_SECRET   (secret VALUE, not secret id)
#   PBI_WORKSPACE_ID
#   PBI_REPORT_ID
#   PBI_DATASET_ID
#
# OPTIONAL ENV VARS
# -----------------
#   PBI_SCOPE            default: https://analysis.windows.net/powerbi/api/.default
#   PBI_HTTP_TIMEOUT_S   default: 30
#   PBI_ALLOW_FALLBACK_ID_RESOLUTION  default: 0
#       If set to "1", missing REPORT/DATASET IDs will be resolved by taking the first report/dataset
#       in the workspace (debug convenience only; not recommended for production).
# ==================================================================================================

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

_TOKEN_CACHE: Dict[str, Any] = {"access_token": None, "expires_at": 0}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _timeout_s() -> int:
    try:
        return int(_env("PBI_HTTP_TIMEOUT_S", "30"))
    except Exception:
        return 30


def _scope() -> str:
    return _env("PBI_SCOPE", "https://analysis.windows.net/powerbi/api/.default")


def _require(name: str) -> str:
    v = _env(name)
    if not v:
        raise RuntimeError(f"Missing {name}")
    return v


def _get_access_token() -> str:
    now = int(time.time())
    if _TOKEN_CACHE["access_token"] and now < int(_TOKEN_CACHE["expires_at"]) - 60:
        return str(_TOKEN_CACHE["access_token"])

    tenant_id = _require("PBI_TENANT_ID")
    client_id = _require("PBI_CLIENT_ID")
    client_secret = _require("PBI_CLIENT_SECRET")

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": _scope(),
        },
        timeout=_timeout_s(),
    )

    if resp.status_code >= 400:
        raise RuntimeError(f"Power BI token request failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    _TOKEN_CACHE["access_token"] = data["access_token"]
    _TOKEN_CACHE["expires_at"] = now + int(data.get("expires_in", 3600))
    return str(_TOKEN_CACHE["access_token"])


def _pbi_get(url: str) -> Dict[str, Any]:
    token = _get_access_token()
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=_timeout_s())
    if resp.status_code >= 400:
        raise RuntimeError(f"Power BI GET failed ({resp.status_code}) {url}: {resp.text}")
    return resp.json()


def _pbi_post(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    token = _get_access_token()
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=_timeout_s(),
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Power BI POST failed ({resp.status_code}) {url}: {resp.text}")
    # Some Power BI POSTs return empty body, but token endpoints return JSON.
    return resp.json() if resp.text else {}


def resolve_ids_if_needed() -> Tuple[str, str, str]:
    """
    Production-safe resolution:
    - Requires WORKSPACE_ID, REPORT_ID, DATASET_ID by default.
    - Optional debug fallback if PBI_ALLOW_FALLBACK_ID_RESOLUTION=1.
    """
    workspace_id = _require("PBI_WORKSPACE_ID")
    report_id = _env("PBI_REPORT_ID")
    dataset_id = _env("PBI_DATASET_ID")

    allow_fallback = _env("PBI_ALLOW_FALLBACK_ID_RESOLUTION", "0") == "1"

    if not report_id:
        if not allow_fallback:
            raise RuntimeError("Missing PBI_REPORT_ID (set PBI_ALLOW_FALLBACK_ID_RESOLUTION=1 for debug fallback)")
        reports = _pbi_get(f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports").get("value", [])
        if not reports:
            raise RuntimeError("No reports found in workspace")
        report_id = reports[0]["id"]

    if not dataset_id:
        if not allow_fallback:
            raise RuntimeError("Missing PBI_DATASET_ID (set PBI_ALLOW_FALLBACK_ID_RESOLUTION=1 for debug fallback)")
        datasets = _pbi_get(f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets").get("value", [])
        if not datasets:
            raise RuntimeError("No datasets found in workspace")
        dataset_id = datasets[0]["id"]

    return workspace_id, str(report_id), str(dataset_id)


def trigger_dataset_refresh(workspace_id: str, dataset_id: str) -> None:
    """
    Triggers a dataset refresh. This does not wait for completion.
    """
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes"
    body = {"notifyOption": "NoNotification"}
    _pbi_post(url, body)


def generate_embed_token(
    workspace_id: str,
    report_id: str,
    dataset_id: str,
    username: Optional[str] = None,
    roles: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """
    Generates an embed token for a report (App-Owns-Data).
    IMPORTANT: include datasets explicitly; otherwise some tenants return tokens
    that fail at embed-time with 401 "Get report failed".
    Supports future RLS via identities.
    """
    body: Dict[str, Any] = {
        "accessLevel": "View",
        "datasets": [{"id": dataset_id}],
    }

    if username:
        body["identities"] = [
            {
                "username": username,
                "roles": roles or [],
                "datasets": [dataset_id],
            }
        ]

    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/GenerateToken"
    return _pbi_post(url, body)
