# ==================================================================================================
# powerbi_app.py  (PRODUCTION BASELINE vNext)
# ==================================================================================================
# SERVICE: NextPoint Power BI Service (Render)
#
# CHANGE SUMMARY (vs your current file)
# ------------------------------------
# 1) /dataset/refresh and /dataset/refresh_once NO LONGER call _maybe_warmup_capacity()
#    - Prevents ARM resume collisions and unintended cost.
#    - Refresh is a Power BI REST call (dataset refresh) and does not require capacity running.
# 2) Capacity warmup remains ONLY on:
#    - POST /capacity/warmup (explicit)
#    - POST /embed/token (optional autowarmup gate)
#
# Everything else preserved.
# ==================================================================================================

import os
import time
from typing import Any, Dict

from flask import Flask, request, jsonify

from powerbi_embed import (
    resolve_ids_if_needed,
    generate_embed_token,
    trigger_dataset_refresh,
    get_latest_refresh_status,
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
    Resume capacity if paused (ONLY used for embed path or explicit warmup).
    Safe to call often due to caching/guarding in azure_capacity.py.
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

from powerbi_embed import _pbi_get  # or expose a safe wrapper

@app.get("/debug/report_dataset")
def debug_report_dataset():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()

    rep = _pbi_get(f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}")
    ds  = _pbi_get(f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}")

    return jsonify({
        "workspaceId": workspace_id,
        "reportId": report_id,
        "report.datasetId": rep.get("datasetId"),
        "dataset.isEffectiveIdentityRequired": ds.get("isEffectiveIdentityRequired"),
        "dataset.isEffectiveIdentityRolesRequired": ds.get("isEffectiveIdentityRolesRequired"),
        "dataset.name": ds.get("name"),
    })

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

    IMPORTANT:
    - Does NOT warm up capacity.
    - Dataset refresh is a Power BI REST API operation and can run while capacity is paused.
    - Avoids ARM resume collisions and unintended costs.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    trigger_dataset_refresh(workspace_id, dataset_id)

    return jsonify({"ok": True})


@app.post("/dataset/refresh_once")
def dataset_refresh_once():
    """
    Refresh trigger with best-effort idempotency by task_id.
    Body: { "task_id": "<uuid>", "force": false }

    IMPORTANT:
    - Does NOT warm up capacity.
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
        return jsonify(
            {"ok": True, "skipped": True, "reason": "already_refreshed", "task_id": task_id}
        )

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
    Requires username for RLS (fail-closed).
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    body: Dict[str, Any] = request.get_json(silent=True) or {}

    # Fail-closed: Wix must send logged-in member email
    username_raw = body.get("username")
    username = (str(username_raw or "").strip().lower())

    if not username or "@" not in username:
        return jsonify({"error": "missing_or_invalid_username"}), 400

    # Ensure capacity is running before token mint + immediate embed usage.
    _maybe_warmup_capacity()

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()

    tok = generate_embed_token(
        workspace_id=workspace_id,
        report_id=report_id,
        dataset_id=dataset_id,
        username=username,
        roles=["rls_email"],  # REQUIRED
    )
    return jsonify(tok)

@app.get("/dataset/refresh_status")
def dataset_refresh_status():
    """
    Returns latest refresh status row.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    out = get_latest_refresh_status(workspace_id, dataset_id)
    return jsonify({"ok": True, **out})


@app.post("/dataset/refresh_and_wait")
def dataset_refresh_and_wait():
    """
    Triggers dataset refresh, polls until terminal status, then returns terminal outcome.
    Body:
      {
        "timeout_s": 900,
        "poll_s": 15
      }
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    timeout_s = int(body.get("timeout_s") or 900)
    poll_s = int(body.get("poll_s") or 15)

    workspace_id, _, dataset_id = resolve_ids_if_needed()

    trigger_dataset_refresh(workspace_id, dataset_id)

    deadline = time.time() + timeout_s
    last = None

    while time.time() < deadline:
        out = get_latest_refresh_status(workspace_id, dataset_id)
        last = out
        status = str(out.get("status") or "").strip().lower()

        if status in ("completed", "failed", "cancelled", "disabled"):
            return jsonify({
                "ok": status == "completed",
                "terminal": True,
                "status": out.get("status"),
                "raw": out.get("raw"),
            })

        time.sleep(max(5, poll_s))

    return jsonify({
        "ok": False,
        "terminal": False,
        "status": (last or {}).get("status"),
        "raw": (last or {}).get("raw"),
        "error": "refresh_timeout",
    }), 504

# ==================================================================================================
# LOCAL DEV
# ==================================================================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(_env("PORT", "5000")), debug=False)


