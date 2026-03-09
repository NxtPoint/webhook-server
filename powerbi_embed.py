# ==================================================================================================
# powerbi_embed.py  (PRODUCTION BASELINE vNext - SAFE LOGGING / REFRESH NORMALIZATION)
# ==================================================================================================
# PURPOSE
# -------
# Handles Power BI REST API calls for embedding + refresh.
#
# What this module does:
# - Obtains OAuth access tokens (client credentials flow) for Power BI REST API
# - Resolves workspace/report/dataset IDs from environment variables
# - Triggers dataset refresh
# - Generates embed tokens for Wix (supports RLS identities)
# - Reads latest dataset refresh status
#
# What this module does NOT do:
# - It does NOT talk to Azure Resource Manager (capacity pause/resume is separate).
# - It does NOT wait synchronously for refresh completion.
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


def _safe_text(resp: requests.Response, limit: int = 500) -> str:
    try:
        txt = (resp.text or "").strip()
        if len(txt) > limit:
            return txt[:limit] + "...[truncated]"
        return txt
    except Exception:
        return ""


def _safe_json(resp: requests.Response) -> Dict[str, Any]:
    try:
        return resp.json()
    except Exception:
        return {}


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
        raise RuntimeError(f"Power BI token request failed ({resp.status_code}): {_safe_text(resp)}")

    data = _safe_json(resp)
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Power BI token response missing access_token")

    _TOKEN_CACHE["access_token"] = access_token
    _TOKEN_CACHE["expires_at"] = now + int(data.get("expires_in", 3600))
    return access_token


def _pbi_get(url: str) -> Dict[str, Any]:
    token = _get_access_token()
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_timeout_s(),
    )

    if resp.status_code >= 400:
        raise RuntimeError(f"Power BI GET failed ({resp.status_code}) {url}: {_safe_text(resp)}")

    return _safe_json(resp)


def _pbi_post(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    token = _get_access_token()
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=_timeout_s(),
    )

    if resp.status_code >= 400:
        raise RuntimeError(f"Power BI POST failed ({resp.status_code}) {url}: {_safe_text(resp)}")

    if not (resp.text or "").strip():
        return {}

    return _safe_json(resp)


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


def trigger_dataset_refresh(workspace_id: str, dataset_id: str) -> Dict[str, Any]:
    """
    Triggers a dataset refresh. Non-blocking.
    Returns compact metadata only.
    """
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes"
    body = {"notifyOption": "NoNotification"}
    out = _pbi_post(url, body)

    return {
        "accepted": True,
        "dataset_id": dataset_id,
        "request_id": out.get("requestId") or out.get("id"),
        "raw": out,
    }


def generate_embed_token(
    workspace_id: str,
    report_id: str,
    dataset_id: str,
    username: Optional[str] = None,
    roles: Optional[list[str]] = None,
) -> Dict[str, Any]:
    require_identity = _env("PBI_REQUIRE_RLS_IDENTITY", "1") == "1"

    norm_user = (username or "").strip().lower()
    if require_identity and not norm_user:
        raise RuntimeError("Missing username for Power BI embed token (fail-closed).")

    eff_roles = roles or ["rls_email"]

    body: Dict[str, Any] = {
        "accessLevel": "View",
        "datasets": [{"id": dataset_id}],
    }

    if norm_user:
        body["identities"] = [
            {
                "username": norm_user,
                "roles": eff_roles,
                "datasets": [dataset_id],
            }
        ]

    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/GenerateToken"
    return _pbi_post(url, body)


def get_latest_refresh_status(workspace_id: str, dataset_id: str) -> Dict[str, Any]:
    """
    TEMP DIAGNOSTIC VERSION
    """
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/refreshes?$top=5"
    data = _pbi_get(url)

    rows = data.get("value", []) or []
    print(f"PBI refresh history dataset_id={dataset_id} row_count={len(rows)} rows={rows}")

    if not rows:
        return {
            "status": None,
            "raw": {"value": []},
            "requestId": None,
            "startTime": None,
            "endTime": None,
        }

    row = rows[0] or {}
    status = str(row.get("status") or "").strip() or None

    return {
        "status": status,
        "raw": row,
        "requestId": row.get("requestId") or row.get("id"),
        "startTime": row.get("startTime"),
        "endTime": row.get("endTime"),
    }