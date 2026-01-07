# ==================================================================================================
# powerbi_app.py
# ==================================================================================================
# SERVICE: NextPoint Power BI Service (Render)
#
# PURPOSE
# -------
# Single Flask web service that provides Power BI Embedded support for Wix:
#
#   1) Capacity control (A1 / Embedded):
#        POST /capacity/warmup   -> resumes capacity (if paused)
#        POST /capacity/suspend  -> pauses capacity (cost control)
#
#   2) Dataset refresh trigger:
#        POST /dataset/refresh   -> triggers a refresh for the configured dataset
#        Intended caller: SportAI pipeline after ingest completes (server-to-server)
#
#   3) Embed support for Wix UI:
#        GET  /embed/config      -> returns embedUrl + IDs (workspace/report/dataset)
#        POST /embed/token       -> returns an embed token for the report
#
# SECURITY MODEL
# --------------
# - All endpoints (except /health) are protected by OPS_KEY via header: x-ops-key
# - This service is intended to be called only from:
#     - Wix backend (powerbi.jsw) or
#     - NextPoint backend services (e.g., webhook-server / upload_app.py)
# - Never call these endpoints directly from Wix browser code with OPS_KEY exposed.
#
# ENVIRONMENT VARIABLES
# ---------------------
# Required:
#   OPS_KEY                Shared secret for server-to-server calls (x-ops-key header)
#   PBI_TENANT_ID           Azure AD tenant ID
#   PBI_CLIENT_ID           Azure AD app (service principal) client ID
#   PBI_CLIENT_SECRET       Azure AD app secret VALUE (not secret id)
#   PBI_WORKSPACE_ID        Power BI workspace (group) ID
#   PBI_REPORT_ID           Power BI report ID
#   PBI_DATASET_ID          Power BI dataset ID
#
# Optional:
#   PORT                   Render sets this automatically
#
# For capacity endpoints (only if you use warmup/suspend):
#   AZ_SUBSCRIPTION_ID      Azure subscription containing the capacity resource
#   AZ_RESOURCE_GROUP       Resource group name for the capacity resource
#   AZ_CAPACITY_NAME        Capacity resource name (e.g., pbinextpointa1)
#
# OPERATIONAL RULES / BEST PRACTICE
# ---------------------------------
# - /embed/config MUST be fast and must not do capacity management or refresh work.
# - Capacity management is decoupled from embedding:
#     - Call /capacity/warmup shortly before embedding (optional), OR after SportAI completes.
# - Dataset refresh is a side-effect operation:
#     - Caller should be idempotent on their side (e.g., only call once per task_id).
# - Keep this service stateless. All state is in:
#     - Power BI / Azure
#     - Render env vars
# ==================================================================================================

import os
from flask import Flask, request, jsonify

from powerbi_embed import (
    resolve_ids_if_needed,
    generate_embed_token,
    trigger_dataset_refresh,
)

app = Flask(__name__)


# ==================================================================================================
# AUTH
# ==================================================================================================
def _require_ops_key(req) -> bool:
    """
    Enforces server-to-server authentication.
    Expect caller to send: x-ops-key: <OPS_KEY>
    """
    expected = os.getenv("OPS_KEY", "").strip()
    sent = (req.headers.get("x-ops-key", "") or "").strip()
    return bool(expected) and (sent == expected)


# ==================================================================================================
# HEALTH
# ==================================================================================================
@app.get("/health")
def health():
    """Public-ish health check for Render uptime monitoring."""
    return jsonify({"ok": True})


# ==================================================================================================
# CAPACITY CONTROL (OPTIONAL)
# ==================================================================================================
@app.post("/capacity/warmup")
def capacity_warmup():
    """
    Resume Power BI Embedded capacity if paused.
    Protected by OPS_KEY.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    # Lazy import to avoid startup failures if Azure SDK/config is misconfigured.
    from azure_capacity import ensure_capacity_running

    ensure_capacity_running()
    return jsonify({"ok": True})


@app.post("/capacity/suspend")
def capacity_suspend():
    """
    Pause Power BI Embedded capacity (cost control).
    Protected by OPS_KEY.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    # Lazy import to avoid startup failures if Azure SDK/config is misconfigured.
    from azure_capacity import suspend_capacity

    suspend_capacity()
    return jsonify({"ok": True})


# ==================================================================================================
# DATASET REFRESH
# ==================================================================================================
@app.post("/dataset/refresh")
def dataset_refresh():
    """
    Trigger a dataset refresh in the configured workspace.
    Intended caller: backend pipeline after SportAI ingest completes.
    Protected by OPS_KEY.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    trigger_dataset_refresh(workspace_id, dataset_id)

    return jsonify({"ok": True})


# ==================================================================================================
# EMBED SUPPORT
# ==================================================================================================
@app.get("/embed/config")
def embed_config():
    """
    Returns IDs + embedUrl for Wix.
    Protected by OPS_KEY to avoid leaking workspace/report IDs publicly.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()

    embed_url = (
        "https://app.powerbi.com/reportEmbed"
        f"?reportId={report_id}&groupId={workspace_id}"
    )

    return jsonify(
        {
            "workspaceId": workspace_id,
            "reportId": report_id,
            "datasetId": dataset_id,
            "embedUrl": embed_url,
        }
    )


@app.post("/embed/token")
def embed_token():
    """
    Returns an embed token for the configured report (and dataset).
    Protected by OPS_KEY.
    RLS hook:
      - pass 'username' (later: parent/player email or child UUID mapping)
      - generate_embed_token can add identities when you implement RLS roles.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()

    body = request.get_json(silent=True) or {}
    username = body.get("username")

    token = generate_embed_token(
        workspace_id=workspace_id,
        report_id=report_id,
        dataset_id=dataset_id,
        username=username,
    )

    return jsonify(token)


# ==================================================================================================
# LOCAL DEV ONLY
# ==================================================================================================
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )
