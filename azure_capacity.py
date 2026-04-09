# ==================================================================================================
# azure_capacity.py
# ==================================================================================================
# Azure ARM REST client for pausing and resuming the Power BI Embedded capacity (A1 SKU).
#
# Uses the Azure Resource Manager REST API directly (not the azure-mgmt-powerbidedicated SDK)
# because the SDK has unstable import paths across versions. Raw REST + AAD token is more
# reliable in a Render/containerised environment.
#
# Authenticates with the same service principal used for Power BI embedding
# (PBI_TENANT_ID / PBI_CLIENT_ID / PBI_CLIENT_SECRET).
#
# Key functions:
#   get_capacity_status()  — returns current state string (e.g. "Succeeded", "Paused")
#   resume_capacity()      — POST to ARM resume endpoint; no-op if already running
#   suspend_capacity()     — POST to ARM suspend endpoint; no-op if already paused
#
# Required env vars:
#   AZ_SUBSCRIPTION_ID    Azure subscription containing the capacity
#   AZ_RESOURCE_GROUP     Resource group name
#   AZ_CAPACITY_NAME      Capacity resource name (e.g. "pbinextpointa1")
#   PBI_TENANT_ID         Azure AD tenant ID
#   PBI_CLIENT_ID         Azure AD app client ID
#   PBI_CLIENT_SECRET     Azure AD app client secret value
#
# Optional env vars:
#   AZ_CAPACITY_PROVIDER  Default: "Microsoft.PowerBIDedicated"
#   AZ_API_VERSION        Default: "2021-01-01"
#   AZ_HTTP_TIMEOUT_S      Default: "30"
#
# BEHAVIOR RULES
# --------------
# - Functions raise RuntimeError with actionable messages if misconfigured or if ARM returns an error.
# - Resume/Suspend are asynchronous (often HTTP 202). We poll via GET to confirm a state change,
#   keeping total wait bounded.
# ==================================================================================================

from __future__ import annotations

import os
import time
from typing import Any, Dict
import threading
_CAPACITY_LOCK = threading.Lock()
import requests
from azure.identity import ClientSecretCredential

_ARM_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expires_at": 0}

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
    """Fetch an ARM bearer token with caching."""
    now = int(time.time())
    tok = _ARM_TOKEN_CACHE.get("token")
    exp = int(_ARM_TOKEN_CACHE.get("expires_at") or 0)
    if tok and now < exp - 60:
        return str(tok)

    cred = _credential()
    t = cred.get_token("https://management.azure.com/.default")
    # azure-identity token has expires_on (epoch seconds)
    expires_on = int(getattr(t, "expires_on", now + 3600))
    _ARM_TOKEN_CACHE["token"] = t.token
    _ARM_TOKEN_CACHE["expires_at"] = expires_on
    return str(t.token)


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

    # Resume/Suspend typically returns 202 Accepted, sometimes 200/204.
    if r.status_code in (200, 202, 204):
        return

    # IMPORTANT: Azure sometimes returns 400 "Service is not ready to be updated"
    # when a previous resume/suspend is already in-flight. Treat as "accepted".
    if r.status_code == 400:
        try:
            j = r.json()
            msg = (j.get("error") or {}).get("message", "") or ""
        except Exception:
            msg = r.text or ""
        if "Service is not ready to be updated" in msg:
            return

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
    # provisioningState can be present; state is the key indicator ("Paused"/"Active")
    return state


def ensure_capacity_running(poll_seconds: int = 30) -> None:
    base = _arm_base_url()
    api = _api_version()

    cap = _arm_get(f"{base}?api-version={api}")
    if _capacity_state_lower(cap) != "paused":
        return

    with _CAPACITY_LOCK:
        # Re-check after acquiring lock
        cap = _arm_get(f"{base}?api-version={api}")
        if _capacity_state_lower(cap) != "paused":
            return

        _arm_post(f"{base}/resume?api-version={api}")

        deadline = time.time() + max(5, poll_seconds)
        while time.time() < deadline:
            time.sleep(5)
            cap2 = _arm_get(f"{base}?api-version={api}")
            if _capacity_state_lower(cap2) != "paused":
                return

    raise RuntimeError(
        f"Capacity resume requested but capacity still reports state=Paused after {poll_seconds}s polling."
    )


def suspend_capacity(poll_seconds: int = 180) -> None:
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

    # Poll until paused (Azure commonly takes 30-180s)
    deadline = time.time() + max(10, poll_seconds)
    while time.time() < deadline:
        time.sleep(5)
        cap = _arm_get(f"{base}?api-version={api}")
        if _capacity_state_lower(cap) == "paused":
            return

    raise RuntimeError(
        f"Capacity suspend requested but still not paused after {poll_seconds}s. "
        f"Check Azure portal activity log for completion."
    )
