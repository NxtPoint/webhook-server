# =========================================
# powerbi_embed.py
# =========================================
# PURPOSE
# -------
# Handles all Power BI REST API calls.
#
# What this file does:
# - Gets OAuth access tokens (client credentials)
# - Resolves workspace / report / dataset IDs
# - Triggers dataset refresh
# - Generates embed tokens for Wix
#
# This file NEVER talks to Azure Resource Manager.
# =========================================

import os
import time
import requests

_TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0,
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_access_token() -> str:
    now = int(time.time())

    if (
        _TOKEN_CACHE["access_token"]
        and now < _TOKEN_CACHE["expires_at"] - 60
    ):
        return _TOKEN_CACHE["access_token"]

    tenant_id = _env("PBI_TENANT_ID")
    client_id = _env("PBI_CLIENT_ID")
    client_secret = _env("PBI_CLIENT_SECRET")

    token_url = (
        f"https://login.microsoftonline.com/"
        f"{tenant_id}/oauth2/v2.0/token"
    )

    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://analysis.windows.net/powerbi/api/.default",
        },
        timeout=30,
    )
    resp.raise_for_status()

    data = resp.json()
    _TOKEN_CACHE["access_token"] = data["access_token"]
    _TOKEN_CACHE["expires_at"] = now + int(data.get("expires_in", 3600))

    return _TOKEN_CACHE["access_token"]


def _pbi_get(url: str):
    token = _get_access_token()
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _pbi_post(url: str, body: dict):
    token = _get_access_token()
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def resolve_ids_if_needed():
    workspace_id = _env("PBI_WORKSPACE_ID")
    report_id = _env("PBI_REPORT_ID")
    dataset_id = _env("PBI_DATASET_ID")

    if not workspace_id:
        raise RuntimeError("Missing PBI_WORKSPACE_ID")

    if not report_id:
        reports = _pbi_get(
            f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports"
        )["value"]
        report_id = reports[0]["id"]

    if not dataset_id:
        datasets = _pbi_get(
            f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets"
        )["value"]
        dataset_id = datasets[0]["id"]

    return workspace_id, report_id, dataset_id


def trigger_dataset_refresh(workspace_id: str, dataset_id: str):
    url = (
        f"https://api.powerbi.com/v1.0/myorg/"
        f"groups/{workspace_id}/datasets/{dataset_id}/refreshes"
    )

    body = {"notifyOption": "NoNotification"}
    _pbi_post(url, body)


def generate_embed_token(
    workspace_id: str,
    report_id: str,
    dataset_id: str,
    username: str | None = None,
):
    body = {
        "accessLevel": "View",
        "datasets": [{"id": dataset_id}],
        "reports": [{"id": report_id}],
    }

    if username:
        body["identities"] = [{
            "username": username,
            "roles": [],
            "datasets": [dataset_id],
        }]

    url = (
        f"https://api.powerbi.com/v1.0/myorg/"
        f"groups/{workspace_id}/GenerateToken"
    )

    return _pbi_post(url, body)
