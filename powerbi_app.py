# ==================================================================================================
# powerbi_app.py
# ==================================================================================================
# SERVICE: NextPoint Power BI Service (Render)
#
# PURPOSE
# -------
# Single Flask web service that provides Power BI Embedded support for Wix + backend automation:
#
#   1) Capacity control (A1 / Embedded) via Azure ARM:
#        POST /capacity/warmup    -> resumes capacity (if paused)
#        POST /capacity/suspend   -> pauses capacity (cost control)
#
#   2) Dataset refresh trigger (for SportAI completion):
#        POST /dataset/refresh          -> triggers refresh (legacy/simple)
#        POST /dataset/refresh_once     -> triggers refresh once per task_id (recommended)
#
#   3) Embed support for Wix UI:
#        GET  /embed/config       -> returns embedUrl + IDs (workspace/report/dataset)
#        POST /embed/token        -> resumes capacity (if configured) + returns embed token
#
# SECURITY MODEL
# --------------
# - All endpoints (except /health) are protected by OPS_KEY via header: x-ops-key
# - Callers:
#     - Wix backend (powerbi.jsw)
#     - NextPoint backend services (SportAI completion pipeline)
# - Never call from browser code with OPS_KEY.
#
# ENVIRONMENT VARIABLES
# ---------------------
# Required:
#   OPS_KEY
#   PBI_TENANT_ID
#   PBI_CLIENT_ID
#   PBI_CLIENT_SECRET
#   PBI_WORKSPACE_ID
#   PBI_REPORT_ID
#   PBI_DATASET_ID
#
# Optional (Capacity Control):
#   AZ_SUBSCRIPTION_ID
#   AZ_RESOURCE_GROUP
#   AZ_CAPACITY_NAME
#   AZ_ARM_OK_CACHE_S          (handled in azure_capacity.py)
#
# Optional (Behavior):
#   PBI_AUTOWARMUP_ON_EMBED    default: "1"
#       If "1": /embed/token will call ensure_capacity_running() before minting token.
#       If "0": you must manage capacity externally (not recommended).
#
# Idempotent refresh:
#   PBI_REFRESH_ONCE_TTL_S     default: "3600"
#       In-memory TTL window for /dataset/refresh_once task_id dedupe.
#       This is best-effort; caller should still be idempotent where possible.
#
# NOTES
# -----
# - This service is intended to be stateless. The refresh-once cache is in-memory and best-effort,
#   sufficient for "don’t spam refresh during one completion flow".
# - If you need durable idempotency, store task_id in Postgres; we can add that later.
# ==================================================================================================

import os
import time
from typing import Any, Dict

from flask import Flask, request, jsonify

from powerbi_embed import (
    resolve_ids_if_needed,
    generate_embed_token,
    trigger_dataset_refresh,
)

app = Flask(__name__)

_REFRESH_ONCE_CACHE: Dict[str, int] = {}  # task_id -> last_refresh_epoch_s


# ==================================================================================================
# HELPERS
# ==================================================================================================
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _require_ops_key(req) -> bool:
    expected = _env("OPS_KEY", "")
    sent = (req.headers.get("x-ops-key", "") or "").strip()
    return bool(expected) and (sent == expected)


def _autowarmup_enabled() -> bool:
    return _env("PBI_AUTOWARMUP_ON_EMBED", "1") == "1"


def _refresh_once_ttl_s() -> int:
    try:
        return int(_env("PBI_REFRESH_ONCE_TTL_S", "3600"))
    except Exception:
        return 3600


def _maybe_warmup_capacity() -> None:
    """
    Resume capacity if paused. Safe to call often due to caching in azure_capacity.py.
    """
    if not _autowarmup_enabled():
        return

    # Lazy import so this service can still run without Azure capacity env during dev.
    from azure_capacity import ensure_capacity_running

    ensure_capacity_running()


def _prune_refresh_once_cache(now: int) -> None:
    ttl = _refresh_once_ttl_s()
    dead = [k for k, ts in _REFRESH_ONCE_CACHE.items() if now - ts > ttl]
    for k in dead:
        _REFRESH_ONCE_CACHE.pop(k, None)


# ==================================================================================================
# HEALTH
# ==================================================================================================
@app.get("/health")
def health():
    return jsonify({"ok": True})


# ==================================================================================================
# CAPACITY CONTROL
# ==================================================================================================
@app.post("/capacity/warmup")
def capacity_warmup():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    from azure_capacity import ensure_capacity_running

    ensure_capacity_running()
    return jsonify({"ok": True})


@app.post("/capacity/suspend")
def capacity_suspend():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    from azure_capacity import suspend_capacity

    suspend_capacity()
    return jsonify({"ok": True})


# ==================================================================================================
# DATASET REFRESH
# ==================================================================================================
@app.post("/dataset/refresh")
def dataset_refresh():
    """
    Simple refresh trigger (no idempotency).
    Intended caller: backend pipeline after ingest completes.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    # Optional: warmup so refresh doesn't fail if capacity is paused
    _maybe_warmup_capacity()

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    trigger_dataset_refresh(workspace_id, dataset_id)

    return jsonify({"ok": True})


@app.post("/dataset/refresh_once")
def dataset_refresh_once():
    """
    Refresh trigger with best-effort idempotency by task_id.
    Body: { "task_id": "<uuid>", "force": false }
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    task_id = str(body.get("task_id") or "").strip()
    force = bool(body.get("force") is True)

    if not task_id:
        return jsonify({"ok": False, "error": "Missing task_id"}), 400

    now = int(time.time())
    _prune_refresh_once_cache(now)

    if (not force) and task_id in _REFRESH_ONCE_CACHE:
        return jsonify({"ok": True, "skipped": True, "reason": "already_refreshed", "task_id": task_id})

    _maybe_warmup_capacity()

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    trigger_dataset_refresh(workspace_id, dataset_id)

    _REFRESH_ONCE_CACHE[task_id] = now
    return jsonify({"ok": True, "skipped": False, "task_id": task_id})


# ==================================================================================================
# EMBED SUPPORT
# ==================================================================================================
@app.get("/embed/config")
def embed_config():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()
    embed_url = "https://app.powerbi.com/reportEmbed" f"?reportId={report_id}&groupId={workspace_id}"

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
    Resumes capacity (if enabled) then returns embed token.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    # Ensure capacity is running before token mint + immediate embed usage.
    _maybe_warmup_capacity()

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    username = body.get("username")

    tok = generate_embed_token(
        workspace_id=workspace_id,
        report_id=report_id,
        dataset_id=dataset_id,
        username=username,
    )
    return jsonify(tok)


# ==================================================================================================
# LOCAL DEV
# ==================================================================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(_env("PORT", "5000")), debug=False)
