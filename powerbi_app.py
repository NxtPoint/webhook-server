# ==================================================================================================
# powerbi_app.py  (PRODUCTION BASELINE vNext - ASYNC REFRESH SAFE)
# ==================================================================================================
# SERVICE: NextPoint Power BI Service (Render)
#
# DESIGN
# ------
# - Refresh endpoints are NON-BLOCKING
# - No synchronous wait endpoint
# - No in-memory refresh idempotency as source of truth
# - Status endpoint returns normalized lifecycle fields
# - Capacity warmup remains ONLY on:
#     - POST /capacity/warmup
#     - POST /embed/token (optional autowarmup gate)
# - Dashboard readiness is decided in upload_app.py, NOT here
# ==================================================================================================

import os
from datetime import datetime, timezone
from typing import Any, Dict

from flask import Flask, request, jsonify

from powerbi_embed import (
    resolve_ids_if_needed,
    generate_embed_token,
    trigger_dataset_refresh,
    get_latest_refresh_status,
    _pbi_get,
)

app = Flask(__name__)


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


def _debug_endpoints_enabled() -> bool:
    return _env("PBI_DEBUG_ENDPOINTS", "0") == "1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _maybe_warmup_capacity() -> None:
    """
    Resume capacity if paused (ONLY used for embed path or explicit warmup).
    Safe to call often due to caching/guarding in azure_capacity.py.
    """
    if not _autowarmup_enabled():
        return

    from azure_capacity import ensure_capacity_running

    ensure_capacity_running()


def _norm_status(raw_status: str) -> str:
    s = (raw_status or "").strip().lower()

    if s in ("unknown", ""):
        return "unknown"
    if s in ("queued",):
        return "queued"
    if s in ("inprogress", "in_progress", "running"):
        return "running"
    if s in ("completed", "succeeded", "success"):
        return "completed"
    if s in ("failed",):
        return "failed"
    if s in ("cancelled", "canceled"):
        return "cancelled"
    if s in ("disabled",):
        return "failed"

    return s


def _is_terminal(norm_status: str) -> bool:
    return norm_status in ("completed", "failed", "cancelled")


def _extract_error_message(raw: Any) -> str:
    if isinstance(raw, dict):
        for key in ("serviceExceptionJson", "error", "message", "extendedStatus"):
            val = raw.get(key)
            if val:
                return str(val)

    return ""


def _normalize_refresh_payload(out: Dict[str, Any]) -> Dict[str, Any]:
    raw_status = str(out.get("status") or "").strip()
    status = _norm_status(raw_status)
    raw = out.get("raw") or {}

    started_at = (
        out.get("startTime")
        or out.get("started_at")
        or (raw.get("startTime") if isinstance(raw, dict) else None)
    )
    ended_at = (
        out.get("endTime")
        or out.get("endTimeUtc")
        or out.get("finished_at")
        or (raw.get("endTime") if isinstance(raw, dict) else None)
    )
    request_id = (
        out.get("requestId")
        or out.get("id")
        or (raw.get("requestId") if isinstance(raw, dict) else None)
        or (raw.get("id") if isinstance(raw, dict) else None)
    )
    error_message = _extract_error_message(raw)

    return {
        "ok": True,
        "status": status,
        "raw_status": raw_status,
        "is_terminal": _is_terminal(status),
        "is_success": status == "completed",
        "started_at": started_at,
        "ended_at": ended_at,
        "request_id": request_id,
        "error_message": error_message,
        "checked_at": _utc_now_iso(),
        "raw": raw,
    }


# ==================================================================================================
# HEALTH
# ==================================================================================================
@app.get("/health")
def health():
    return jsonify({"ok": True})


# ==================================================================================================
# DEBUG
# ==================================================================================================
@app.get("/debug/report_dataset")
def debug_report_dataset():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    if not _debug_endpoints_enabled():
        return jsonify({"error": "debug_endpoints_disabled"}), 404

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()

    rep = _pbi_get(f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}")
    ds = _pbi_get(f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}")

    return jsonify(
        {
            "workspaceId": workspace_id,
            "reportId": report_id,
            "report.datasetId": rep.get("datasetId"),
            "dataset.isEffectiveIdentityRequired": ds.get("isEffectiveIdentityRequired"),
            "dataset.isEffectiveIdentityRolesRequired": ds.get("isEffectiveIdentityRolesRequired"),
            "dataset.name": ds.get("name"),
        }
    )


# ==================================================================================================
# CAPACITY CONTROL
# ==================================================================================================
@app.post("/capacity/warmup")
def capacity_warmup():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    from azure_capacity import ensure_capacity_running

    ensure_capacity_running()
    print("PBI capacity warmup ok")
    return jsonify({"ok": True})


@app.post("/capacity/suspend")
def capacity_suspend():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    from azure_capacity import suspend_capacity

    suspend_capacity()
    print("PBI capacity suspend ok")
    return jsonify({"ok": True})


# ==================================================================================================
# DATASET REFRESH
# ==================================================================================================
@app.post("/dataset/refresh")
def dataset_refresh():
    """
    Simple refresh trigger (non-blocking, no idempotency).
    Capacity must be running before refresh.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    _maybe_warmup_capacity()

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    trigger_dataset_refresh(workspace_id, dataset_id)

    print(f"PBI refresh triggered dataset_id={dataset_id}")
    return jsonify(
        {
            "ok": True,
            "accepted": True,
            "terminal": False,
            "status": "queued",
            "dataset_id": dataset_id,
            "triggered_at": _utc_now_iso(),
        }
    )


@app.post("/dataset/refresh_once")
def dataset_refresh_once():
    """
    Refresh trigger endpoint for pipeline callers.
    NOTE:
    - Non-blocking
    - No durable idempotency here
    - task_id accepted for correlation/logging only
    - Capacity must be running before refresh
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    task_id = str(body.get("task_id") or "").strip()

    if not task_id:
        return jsonify({"ok": False, "error": "missing_task_id"}), 400

    _maybe_warmup_capacity()

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    trigger_dataset_refresh(workspace_id, dataset_id)

    print(f"PBI refresh_once triggered task_id={task_id} dataset_id={dataset_id}")
    return jsonify(
        {
            "ok": True,
            "accepted": True,
            "terminal": False,
            "status": "queued",
            "task_id": task_id,
            "dataset_id": dataset_id,
            "triggered_at": _utc_now_iso(),
        }
    )


@app.get("/dataset/refresh_status")
def dataset_refresh_status():
    """
    Returns latest refresh status row, normalized for upload_app.py polling.
    """
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    out = get_latest_refresh_status(workspace_id, dataset_id)
    norm = _normalize_refresh_payload(out)

    print(
        f"PBI refresh_status dataset_id={dataset_id} "
        f"status={norm.get('status')} terminal={norm.get('is_terminal')}"
    )

    return jsonify(norm)


# ==================================================================================================
# EMBED SUPPORT
# ==================================================================================================
@app.get("/embed/config")
def embed_config():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()
    embed_url = f"https://app.powerbi.com/reportEmbed?reportId={report_id}&groupId={workspace_id}"

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
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    body: Dict[str, Any] = request.get_json(silent=True) or {}

    username_raw = body.get("username")
    username = str(username_raw or "").strip().lower()

    if not username or "@" not in username:
        return jsonify({"error": "missing_or_invalid_username"}), 400

    try:
        _maybe_warmup_capacity()

        workspace_id, report_id, dataset_id = resolve_ids_if_needed()

        tok = generate_embed_token(
            workspace_id=workspace_id,
            report_id=report_id,
            dataset_id=dataset_id,
            username=username,
            roles=["rls_email"],
        )

        token = str((tok or {}).get("token") or "").strip()
        if not token:
            print(f"PBI embed token missing username={username}")
            return jsonify(
                {
                    "error": "embed_token_missing",
                    "workspace_id": workspace_id,
                    "report_id": report_id,
                    "dataset_id": dataset_id,
                    "username": username,
                }
            ), 500

        print(
            f"PBI embed token ok username={username} "
            f"tokenId={tok.get('tokenId')} expiration={tok.get('expiration')}"
        )
        return jsonify(tok)

    except Exception as e:
        print(f"PBI embed token exception username={username} err={str(e)}")
        return jsonify(
            {
                "error": "embed_token_exception",
                "detail": str(e),
                "username": username,
            }
        ), 500


# ==================================================================================================
# LOCAL DEV
# ==================================================================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(_env("PORT", "5000")), debug=False)