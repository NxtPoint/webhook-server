# =========================================
# powerbi_app.py
# =========================================
# PURPOSE
# -------
# Single Flask service that:
# - Warms / suspends Power BI Embedded capacity
# - Refreshes datasets when SportAI completes
# - Provides embed config + embed tokens to Wix
#
# All sensitive endpoints are protected by OPS_KEY.
# =========================================

import os
from flask import Flask, request, jsonify


from powerbi_embed import (
    resolve_ids_if_needed,
    generate_embed_token,
    trigger_dataset_refresh,
)

app = Flask(__name__)


def _require_ops_key(req):
    expected = os.getenv("OPS_KEY", "")
    sent = req.headers.get("x-ops-key", "")
    return bool(expected and sent == expected)


@app.get("/health")
def health():
    return jsonify({"ok": True})


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



@app.post("/dataset/refresh")
def dataset_refresh():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    ensure_capacity_running()

    workspace_id, _, dataset_id = resolve_ids_if_needed()
    trigger_dataset_refresh(workspace_id, dataset_id)

    return jsonify({"ok": True})


@app.get("/embed/config")
def embed_config():
    if not _require_ops_key(request):
        return jsonify({"error": "unauthorized"}), 401

    ensure_capacity_running()

    workspace_id, report_id, dataset_id = resolve_ids_if_needed()

    embed_url = (
        f"https://app.powerbi.com/reportEmbed"
        f"?reportId={report_id}&groupId={workspace_id}"
    )

    return jsonify({
        "workspaceId": workspace_id,
        "reportId": report_id,
        "datasetId": dataset_id,
        "embedUrl": embed_url,
    })


@app.post("/embed/token")
def embed_token():
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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )
