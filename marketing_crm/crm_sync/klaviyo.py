# marketing_crm/crm_sync/klaviyo.py — Klaviyo profile upsert + event tracking.
#
# Auth: KLAVIYO_API_KEY (private key). No-op if unset. Profiles carry marketing-consent state
# (marketing_opt_in) so flows can gate on it. Events are forwarded so Klaviyo flows trigger.
# API revision pinned. Untested against a live Klaviyo account (no key in dev) — standard payloads.

import logging
import os

log = logging.getLogger("marketing_crm.crm_sync.klaviyo")
_BASE = "https://a.klaviyo.com/api"
_REVISION = "2024-10-15"


def _key():
    return os.getenv("KLAVIYO_API_KEY")


def _headers():
    return {
        "Authorization": f"Klaviyo-API-Key {_key()}",
        "revision": _REVISION,
        "accept": "application/json",
        "content-type": "application/json",
    }


def upsert_profile(traits):
    """Create-or-update a Klaviyo profile by email (uses the profile-import upsert endpoint)."""
    if not _key():
        return False
    email = (traits or {}).get("email")
    if not email:
        return False
    import requests
    attrs = {
        "email": email,
        "properties": {
            "ttf_stage": traits.get("stage"),
            "ttf_plan": traits.get("plan_code"),
            "ttf_mrr": round((traits.get("mrr_cents") or 0) / 100.0, 2),
            "ttf_matches_remaining": traits.get("matches_remaining"),
            "ttf_role": traits.get("role"),
            "ttf_marketing_opt_in": bool(traits.get("marketing_opt_in")),
            "ttf_signup_source": traits.get("source"),
        },
    }
    if traits.get("display_name"):
        attrs["first_name"] = str(traits["display_name"]).split(" ")[0]
    body = {"data": {"type": "profile", "attributes": attrs}}
    try:
        # POST creates; on duplicate (409) Klaviyo returns the existing id → PATCH it.
        r = requests.post(f"{_BASE}/profiles/", headers=_headers(), json=body, timeout=8)
        if r.status_code == 409:
            dup_id = (((r.json() or {}).get("errors") or [{}])[0].get("meta") or {}).get("duplicate_profile_id")
            if dup_id:
                body["data"]["id"] = dup_id
                r = requests.patch(f"{_BASE}/profiles/{dup_id}/", headers=_headers(), json=body, timeout=8)
        ok = r.status_code < 300
        if not ok:
            log.warning("klaviyo upsert %s -> %s %s", email, r.status_code, r.text[:200])
        return ok
    except Exception:
        log.exception("klaviyo profile upsert failed for %s", email)
        return False


def track_event(email, metric, properties=None):
    """Forward a product event to Klaviyo (drives flow triggers). No-op without key/email."""
    if not _key() or not email:
        return False
    import requests
    body = {"data": {"type": "event", "attributes": {
        "metric": {"data": {"type": "metric", "attributes": {"name": metric}}},
        "profile": {"data": {"type": "profile", "attributes": {"email": email}}},
        "properties": properties or {},
    }}}
    try:
        r = requests.post(f"{_BASE}/events/", headers=_headers(), json=body, timeout=8)
        ok = r.status_code < 300
        if not ok:
            log.warning("klaviyo event %s/%s -> %s %s", metric, email, r.status_code, r.text[:200])
        return ok
    except Exception:
        log.exception("klaviyo track_event failed for %s/%s", metric, email)
        return False
