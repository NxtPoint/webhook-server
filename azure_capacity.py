# ==================================================================================================
# azure_capacity.py
# ==================================================================================================
# SERVICE MODULE: Azure Capacity Control (Power BI Embedded / A1)
#
# PURPOSE
# -------
# Control the lifecycle of an Azure Power BI Embedded capacity (e.g., A1) via Azure Resource Manager
# REST API (NOT Power BI REST APIs).
#
# Why REST (not azure-mgmt-powerbidedicated)?
# - The azure-mgmt-powerbidedicated SDK has inconsistent import/export paths across versions.
# - REST + AAD token is stable and avoids dependency/import issues that can crash your service.
#
# WHAT THIS MODULE DOES
# ---------------------
# - Authenticates using the same service principal you already use for Power BI embedding:
#     PBI_TENANT_ID / PBI_CLIENT_ID / PBI_CLIENT_SECRET
# - Calls Azure ARM endpoints to:
#     - GET capacity status
#     - POST resume
#     - POST suspend
#
# REQUIRED ENV VARS (Capacity Control)
# -----------------------------------
#   AZ_SUBSCRIPTION_ID   Azure subscription that contains the capacity resource
#   AZ_RESOURCE_GROUP    Resource group name that contains the capacity
#   AZ_CAPACITY_NAME     Capacity resource name (e.g., "pbinextpointa1")
#
# REQUIRED ENV VARS (Authentication)
# ---------------------------------
#   PBI_TENANT_ID        Azure AD tenant ID
#   PBI_CLIENT_ID        Azure AD app client ID
#   PBI_CLIENT_SECRET    Azure AD app secret VALUE (not secret id)
#
# OPTIONAL ENV VARS
# -----------------
#   AZ_CAPACITY_PROVIDER   Default: "Microsoft.PowerBIDedicated"
#       (Future-proofing: Fabric capacities use "Microsoft.Fabric", but A1 Embedded uses PowerBIDedicated.)
#   AZ_API_VERSION         Default: "2021-01-01"
#   AZ_HTTP_TIMEOUT_S      Default: "30"
#
# BEHAVIOR RULES
# --------------
# - Functions raise RuntimeError with actionable messages if misconfigured or if ARM returns an error.
# - Resume/Suspend are asynchronous (often HTTP 202). We poll once via GET to confirm a state change,
#   but do not block indefinitely.
# ==================================================================================================

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import requests
from azure.identity import ClientSecretCredential


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _timeout_s() -> int:
    try:
        return int(_env("AZ_HTTP_TIMEOUT_S", "30"))
    except Exception:
        return 30


def _provider() -> str:
    # A1 / Power BI Embedded capacity in Azure uses Microsoft.PowerBIDedicated
    return _env("AZ_CAPACITY_PROVIDER", "Microsoft.PowerBIDedicated")


def _api_version() -> str:
    # REST docs show 2021-01-01 for Power BI Embedded capacities resume/suspend/get
    return _env("AZ_API_VERSION", "2021-01-01")


def _subscription_id() -> str:
    sub = _env("AZ_SUBSCRIPTION_ID")
    if not sub:
        raise RuntimeError("Missing AZ_SUBSCRIPTION_ID")
    return sub


def _resource_group() -> str:
    rg = _env("AZ_RESOURCE_GROUP")
    if not rg:
        raise RuntimeError("Missing AZ_RESOURCE_GROUP")
    return rg


def _capacity_name() -> str:
    name = _env("AZ_CAPACITY_NAME")
    if not name:
        raise RuntimeError("Missing AZ_CAPACITY_NAME")
    return name


def _credential() -> ClientSecretCredential:
    tenant_id = _env("PBI_TENANT_ID")
    client_id = _env("PBI_CLIENT_ID")
    client_secret = _env("PBI_CLIENT_SECRET")

    if not (tenant_id and client_id and client_secret):
        raise RuntimeError("Missing PBI_TENANT_ID / PBI_CLIENT_ID / PBI_CLIENT_SECRET")

    return ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )


def _arm_token() -> str:
    """
    Fetch an ARM (management.azure.com) bearer token.
    """
    cred = _credential()
    token = cred.get_token("https://management.azure.com/.default")
    return token.token


def _arm_base_url() -> str:
    sub = _subscription_id()
    rg = _resource_group()
    name = _capacity_name()
    prov = _provider()
    return (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/{prov}"
        f"/capacities/{name}"
    )


def _arm_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_arm_token()}",
        "Content-Type": "application/json",
    }


def _arm_get(url: str) -> Dict[str, Any]:
    r = requests.get(url, headers=_arm_headers(), timeout=_timeout_s())
    if r.status_code >= 400:
        raise RuntimeError(f"ARM GET failed ({r.status_code}): {r.text}")
    return r.json()


def _arm_post(url: str) -> None:
    r = requests.post(url, headers=_arm_headers(), timeout=_timeout_s())
    # Resume/Suspend typically returns 202 Accepted, sometimes 200.
    if r.status_code not in (200, 202, 204):
        raise RuntimeError(f"ARM POST failed ({r.status_code}): {r.text}")


def get_capacity_status() -> Dict[str, Any]:
    """
    Returns raw ARM resource JSON for the capacity.
    Useful for debugging state/provisioningState.
    """
    url = f"{_arm_base_url()}?api-version={_api_version()}"
    return _arm_get(url)


def _capacity_state_lower(cap_json: Dict[str, Any]) -> str:
    props = (cap_json or {}).get("properties") or {}
    state = (props.get("state") or "").strip().lower()
    # Some fields of interest:
    # provisioningState can be present; state is the key indicator ("Paused"/"Active")
    return state


def ensure_capacity_running(poll_seconds: int = 10) -> None:
    """
    If capacity is paused, resume it.
    Poll once after resume to reduce race conditions, but keep total wait bounded.
    """
    base = _arm_base_url()
    api = _api_version()

    cap = _arm_get(f"{base}?api-version={api}")
    state = _capacity_state_lower(cap)

    if state != "paused":
        # Consider anything not explicitly "paused" as running enough for embed usage
        return

    _arm_post(f"{base}/resume?api-version={api}")

    # Poll once (bounded) to confirm it transitions away from Paused
    deadline = time.time() + max(1, poll_seconds)
    while time.time() < deadline:
        time.sleep(2)
        cap2 = _arm_get(f"{base}?api-version={api}")
        if _capacity_state_lower(cap2) != "paused":
            return

    # If still paused after bounded wait, surface a clear error
    raise RuntimeError("Capacity resume requested but capacity still reports state=Paused after polling.")


def suspend_capacity(poll_seconds: int = 120) -> None:
    """
    Suspend (pause) capacity. Idempotent.
    Azure can take a while; poll up to poll_seconds.
    """
    base = _arm_base_url()
    api = _api_version()

    # If already paused, return cleanly
    cap0 = _arm_get(f"{base}?api-version={api}")
    if _capacity_state_lower(cap0) == "paused":
        return

    _arm_post(f"{base}/suspend?api-version={api}")

    # Poll until paused (Azure commonly takes 30-120s)
    deadline = time.time() + max(5, poll_seconds)
    while time.time() < deadline:
        time.sleep(5)
        cap = _arm_get(f"{base}?api-version={api}")
        state = _capacity_state_lower(cap)

        # Some tenants show intermediate provisioningState; state is still the key
        if state == "paused":
            return

    # If we get here, we didn't observe paused in time. Don't guess.
    raise RuntimeError(
        f"Capacity suspend requested but still not paused after {poll_seconds}s. "
        f"Check Azure portal activity log for completion."
    )
