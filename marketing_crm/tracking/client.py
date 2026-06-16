# marketing_crm/tracking/client.py — the track() implementation.
#
# Guarantees: never raises, never blocks the caller (daemon thread), no-op unless TRACKING_ENABLED=1.
# Emits to core.usage_event (account/user resolved by email best-effort) + Amplitude (optional).

import logging
import os
import threading

log = logging.getLogger("marketing_crm.tracking")

_AMPLITUDE_URL = "https://api2.amplitude.com/2/httpapi"


def _enabled():
    return os.getenv("TRACKING_ENABLED", "0") == "1"


def track(event_type, *, email=None, account_id=None, user_id=None, person_id=None,
          ref_type=None, ref_id=None, properties=None):
    """Record a product event. Fire-and-forget — safe to call from any request handler.

    Resolves account/user from `email` if ids not supplied. Writes core.usage_event and (if
    AMPLITUDE_API_KEY is set) Amplitude. Silent no-op unless TRACKING_ENABLED=1."""
    if not _enabled():
        return
    try:
        threading.Thread(
            target=_emit,
            args=(event_type, email, account_id, user_id, person_id, ref_type, ref_id,
                  dict(properties or {})),
            daemon=True,
        ).start()
    except Exception:
        log.exception("track: failed to spawn emit thread for %s", event_type)


def _emit(event_type, email, account_id, user_id, person_id, ref_type, ref_id, properties):
    # 1) Durable: core.usage_event
    try:
        from core_db.db import session_scope
        from core_db.repositories import accounts, matches
        with session_scope() as s:
            if email:
                if account_id is None:
                    a = accounts.get_account_by_email(s, email)
                    if a:
                        account_id = a.id
                if user_id is None:
                    u = accounts.get_user_by_email(s, email)
                    if u:
                        user_id = u.id
            meta = dict(properties)
            # If we couldn't link to a core account yet (pre-backfill), keep the email so the
            # event can be linked later. Otherwise don't duplicate PII into the metadata blob.
            if account_id is None and email:
                meta["email_unmatched"] = email
            matches.record_usage(
                s, event_type=event_type, account_id=account_id, user_id=user_id,
                person_id=person_id, ref_type=ref_type, ref_id=ref_id, metadata=meta or None,
            )
    except Exception:
        log.exception("track: usage_event write failed for %s", event_type)

    # 2) Best-effort: Amplitude
    try:
        _amplitude(event_type, email, account_id, properties)
    except Exception:
        log.exception("track: amplitude emit failed for %s", event_type)

    # 3) Best-effort: forward to Klaviyo so marketing flows can trigger (no-op unless CRM_SYNC_ENABLED)
    try:
        from marketing_crm.crm_sync import forward_event
        forward_event(event_type, email, properties)
    except Exception:
        log.exception("track: crm forward failed for %s", event_type)


def _amplitude(event_type, email, account_id, properties):
    key = os.getenv("AMPLITUDE_API_KEY")
    if not key:
        return
    import requests
    user_id = email or (f"account:{account_id}" if account_id else "anonymous")
    payload = {
        "api_key": key,
        "events": [{
            "user_id": user_id,
            "event_type": event_type,
            "event_properties": properties or {},
        }],
    }
    requests.post(_AMPLITUDE_URL, json=payload, timeout=5)
