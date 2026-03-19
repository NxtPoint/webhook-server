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

from powerbi_capacity_sessions import (
    powerbi_capacity_sessions_init,
    start_session,
    heartbeat_session,
    end_session,
    sweep_sessions,
    has_active_sessions,
)

app = Flask(__name__)

powerbi_capacity_sessions_init()


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
        return "running"
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

def _lease_seconds() -> int:
    try:
        return max(60, int(_env("PBI_SESSION_LEASE_SECONDS", "180")))
    except Exception:
        return 180


def _extract_username_from_body(body: Dict[str, Any]) -> str:
    username_raw = body.get("username")
    username = str(username_raw or "").strip().lower()
    if not username or "@" not in username:
        raise RuntimeError("missing_or_invalid_username")
    return username


def _safe_has_active_sessions() -> bool:
    try:
        return has_active_sessions()
    except Exception:
        return True

def _refresh_is_active() -> bool:
    """
    True when latest dataset refresh is queued/running.
    This prevents sweep from suspending capacity mid-refresh.
    """
    try:
        workspace_id, _, dataset_id = resolve_ids_if_needed()
        out = get_latest_refresh_status(workspace_id, dataset_id)
        norm = _normalize_refresh_payload(out)
        return norm.get("status") in ("queued", "running")
    except Exception as e:
        print(f"PBI refresh activity check failed err={str(e)}")
        # Fail-safe: assume refresh may be active so we do NOT suspend by mistake
        return True

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
# SESSION LEASE CONTROL
# ==================================================================================================
@app.post("/session/start")
def session_start():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        body: Dict[str, Any] = request.get_json(silent=True) or {}
        username = _extract_username_from_body(body)

        workspace_id, report_id, dataset_id = resolve_ids_if_needed()

        sess = start_session(
            username=username,
            lease_seconds=_lease_seconds(),
            report_id=report_id,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            created_by="embed",
        )

        # Ensure capacity is running AFTER session row exists.
        # This is safer because the sweeper can now see active demand.
        _maybe_warmup_capacity()

        print(
            f"PBI session start ok username={username} "
            f"session_id={sess.get('session_id')} lease_s={_lease_seconds()}"
        )

        return jsonify(
            {
                "ok": True,
                "session_id": sess.get("session_id"),
                "username": username,
                "lease_seconds": _lease_seconds(),
                "workspaceId": workspace_id,
                "reportId": report_id,
                "datasetId": dataset_id,
                "started_at": sess.get("started_at"),
                "last_seen_at": sess.get("last_seen_at"),
                "expires_at": sess.get("expires_at"),
            }
        )

    except Exception as e:
        print(f"PBI session start exception err={str(e)}")
        return jsonify({"ok": False, "error": "session_start_failed", "detail": str(e)}), 500


@app.post("/session/heartbeat")
def session_heartbeat():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        body: Dict[str, Any] = request.get_json(silent=True) or {}
        username = _extract_username_from_body(body)
        session_id = str(body.get("session_id") or "").strip()

        if not session_id:
            return jsonify({"ok": False, "error": "missing_session_id"}), 400

        out = heartbeat_session(
            session_id=session_id,
            username=username,
            lease_seconds=_lease_seconds(),
        )

        print(
            f"PBI session heartbeat username={username} "
            f"session_id={session_id} found={out.get('found')} status={out.get('status')}"
        )

        return jsonify(out)

    except Exception as e:
        print(f"PBI session heartbeat exception err={str(e)}")
        return jsonify({"ok": False, "error": "session_heartbeat_failed", "detail": str(e)}), 500


@app.post("/session/end")
def session_end():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        body: Dict[str, Any] = request.get_json(silent=True) or {}
        username = _extract_username_from_body(body)
        session_id = str(body.get("session_id") or "").strip()
        reason = str(body.get("reason") or "client_end").strip()

        if not session_id:
            return jsonify({"ok": False, "error": "missing_session_id"}), 400

        out = end_session(
            session_id=session_id,
            username=username,
            reason=reason,
        )

        has_active_sessions_remaining = _safe_has_active_sessions()
        suspended = False

        if not has_active_sessions_remaining:
            from azure_capacity import suspend_capacity
            suspend_capacity()
            suspended = True

        print(
            f"PBI session end username={username} session_id={session_id} "
            f"status={out.get('status')} has_active_sessions_remaining={has_active_sessions_remaining} "
            f"suspended={suspended}"
        )

        return jsonify(
            {
                **out,
                "has_active_sessions_remaining": has_active_sessions_remaining,
                "capacity_suspend_attempted": suspended,
            }
        )

    except Exception as e:
        print(f"PBI session end exception err={str(e)}")
        return jsonify({"ok": False, "error": "session_end_failed", "detail": str(e)}), 500


@app.post("/session/sweep")
def session_sweep():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        out = sweep_sessions()
        active_count = int(out.get("active_session_count") or 0)
        refresh_active = _refresh_is_active()

        suspended = False
        if active_count == 0 and not refresh_active:
            from azure_capacity import suspend_capacity
            suspend_capacity()
            suspended = True

        print(
            f"PBI session sweep expired={out.get('expired_count')} "
            f"active={active_count} refresh_active={refresh_active} suspended={suspended}"
        )

        return jsonify(
            {
                "ok": True,
                **out,
                "refresh_active": refresh_active,
                "capacity_suspend_attempted": suspended,
                "checked_at": _utc_now_iso(),
            }
        )

    except Exception as e:
        print(f"PBI session sweep exception err={str(e)}")
        return jsonify({"ok": False, "error": "session_sweep_failed", "detail": str(e)}), 500

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