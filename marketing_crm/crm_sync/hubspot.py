# marketing_crm/crm_sync/hubspot.py — HubSpot contact upsert (one-way mirror of core.*).
#
# ⚠️ DEPRECATED / RETAINED (decision 2026-06-18): we do NOT use a separate CRM tool. Our own
# core.*/billing.* + the cockpit + /api/crm/* ARE the CRM (single source of truth, no sync drift,
# no per-seat cost). Klaviyo is the only active marketing destination. This module is kept dormant
# as a zero-cost escape hatch IF a sales-led motion ever needs HubSpot — it self-gates (no-op
# without a token), so leaving it costs nothing. Don't invest further here; don't set a HubSpot key.
#
# Auth: HUBSPOT_PRIVATE_APP_TOKEN (preferred) or HUBSPOT_API_KEY. No-op if unset.
# Maps traits → HubSpot contact properties per contracts/hubspot_field_map.md. Upsert by email.

import logging
import os

log = logging.getLogger("marketing_crm.crm_sync.hubspot")
_BASE = "https://api.hubapi.com/crm/v3/objects/contacts"


def _token():
    return os.getenv("HUBSPOT_PRIVATE_APP_TOKEN") or os.getenv("HUBSPOT_API_KEY")


def _properties(t):
    """core trait dict → HubSpot property names (field map). PII/minor/biometric excluded by design."""
    props = {
        "email": t.get("email"),
        "ttf_account_public_id": str(t.get("public_id") or ""),
        "ttf_role": t.get("role"),
        "ttf_lifecycle_stage": t.get("stage"),
        "ttf_plan": t.get("plan_code"),
        "ttf_mrr": round((t.get("mrr_cents") or 0) / 100.0, 2),
        "ttf_matches_uploaded": t.get("matches_uploaded"),
        "ttf_matches_remaining": t.get("matches_remaining"),
        "ttf_nps": t.get("nps_latest"),
        "ttf_signup_source": t.get("source"),
        "ttf_signup_campaign": t.get("campaign"),
    }
    if t.get("display_name"):
        props["firstname"] = str(t["display_name"]).split(" ")[0]
    # drop Nones (HubSpot rejects nulls for some types)
    return {k: v for k, v in props.items() if v is not None}


def upsert_contact(traits):
    """Create-or-update a HubSpot contact by email. Returns True on success, False if disabled/failed."""
    token = _token()
    email = (traits or {}).get("email")
    if not token or not email:
        return False
    import requests
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"properties": _properties(traits)}
    try:
        # PATCH upsert by email (idProperty=email); create on 404
        r = requests.patch(f"{_BASE}/{email}?idProperty=email", headers=headers, json=body, timeout=8)
        if r.status_code == 404:
            r = requests.post(_BASE, headers=headers, json=body, timeout=8)
        ok = r.status_code < 300
        if not ok:
            log.warning("hubspot upsert %s -> %s %s", email, r.status_code, r.text[:200])
        return ok
    except Exception:
        log.exception("hubspot upsert failed for %s", email)
        return False
