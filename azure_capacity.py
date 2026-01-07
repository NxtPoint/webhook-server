# =========================================
# azure_capacity.py
# =========================================
# PURPOSE
# -------
# Controls the Power BI Embedded (A1) capacity lifecycle.
#
# What this file does:
# - Authenticates to Azure using the App Registration (service principal)
# - Checks if the Power BI Embedded capacity is running
# - Resumes the capacity if paused (cost control)
# - Optionally suspends the capacity
#
# This talks to Azure Resource Manager (NOT Power BI REST APIs).
# =========================================

import os
from azure.identity import ClientSecretCredential
from azure.mgmt.powerbidedicated import PowerBIDedicatedManagementClient


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _client():
    tenant_id = _env("PBI_TENANT_ID")
    client_id = _env("PBI_CLIENT_ID")
    client_secret = _env("PBI_CLIENT_SECRET")

    subscription_id = _env("AZ_SUBSCRIPTION_ID")
    if not subscription_id:
        raise RuntimeError("Missing AZ_SUBSCRIPTION_ID")

    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )

    return PowerBIDedicatedManagementClient(
        credential=credential,
        subscription_id=subscription_id,
    )


def ensure_capacity_running():
    resource_group = _env("AZ_RESOURCE_GROUP")
    capacity_name = _env("AZ_CAPACITY_NAME")

    if not resource_group or not capacity_name:
        raise RuntimeError("Missing AZ_RESOURCE_GROUP or AZ_CAPACITY_NAME")

    client = _client()
    capacity = client.capacities.get(resource_group, capacity_name)

    state = (capacity.state or "").lower()
    provisioning = (capacity.provisioning_state or "").lower()

    is_running = (
        "active" in state
        or "succeeded" in provisioning
    )

    if not is_running:
        poller = client.capacities.resume(resource_group, capacity_name)
        poller.result()


def suspend_capacity():
    resource_group = _env("AZ_RESOURCE_GROUP")
    capacity_name = _env("AZ_CAPACITY_NAME")

    if not resource_group or not capacity_name:
        raise RuntimeError("Missing AZ_RESOURCE_GROUP or AZ_CAPACITY_NAME")

    client = _client()
    poller = client.capacities.suspend(resource_group, capacity_name)
    poller.result()
