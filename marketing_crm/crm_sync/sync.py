# marketing_crm/crm_sync/sync.py — orchestration: build traits from core.*, push to HubSpot+Klaviyo.
#
# enabled()        : master gate (CRM_SYNC_ENABLED=1)
# build_traits()   : core.* → flat trait dict (owner-level only; no minor/biometric data)
# sync_profile()   : fire-and-forget upsert of one account to both destinations
# forward_event()  : forward a product event to Klaviyo (called from the tracking thread — synchronous)
# sync_all()       : batch upsert every account (nightly/manual via ops endpoint)

import logging
import os
import threading

from sqlalchemy import text

from core_db.db import get_engine, norm_email, session_scope
from marketing_crm.crm_sync import hubspot, klaviyo

log = logging.getLogger("marketing_crm.crm_sync")

# Account-level traits only — vw_customer_list is owner/account-scoped, no child PII.
_TRAITS_SQL = text("""
    SELECT cl.email, cl.display_name, cl.role, cl.stage, cl.plan_code, cl.plan_type,
           cl.mrr_cents, cl.matches_uploaded, cl.matches_remaining, cl.last_activity,
           cl.nps_latest, cl.public_id,
           u.marketing_opt_in, acq.source, acq.medium, acq.campaign
    FROM core.vw_customer_list cl
    JOIN core.account a ON a.id = cl.account_id
    LEFT JOIN core.app_user u  ON u.account_id = a.id AND u.is_account_owner
    LEFT JOIN core.acquisition acq ON acq.user_id = u.id
    WHERE lower(a.email) = :e
    LIMIT 1
""")


def enabled():
    return os.getenv("CRM_SYNC_ENABLED", "0") == "1"


def build_traits(session, email):
    email = norm_email(email)
    if not email:
        return None
    row = session.execute(_TRAITS_SQL, {"e": email}).mappings().first()
    return dict(row) if row else None


def _push(traits):
    if not traits:
        return
    try:
        hubspot.upsert_contact(traits)
    except Exception:
        log.exception("hubspot push failed")
    try:
        klaviyo.upsert_profile(traits)
    except Exception:
        log.exception("klaviyo push failed")


def sync_profile(email):
    """Fire-and-forget: upsert one account's profile to HubSpot + Klaviyo. Safe from request handlers."""
    if not enabled():
        return

    def _run():
        try:
            with session_scope() as s:
                traits = build_traits(s, email)
            _push(traits)
        except Exception:
            log.exception("sync_profile failed for %s", email)

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        log.exception("sync_profile: thread spawn failed")


def forward_event(event_type, email, properties=None):
    """Forward a product event to Klaviyo (flow trigger). Synchronous — call from a background
    context (the tracking client already runs on its own thread)."""
    if not enabled() or not email:
        return
    try:
        klaviyo.track_event(email, event_type, properties or {})
    except Exception:
        log.exception("forward_event failed for %s/%s", event_type, email)


def sync_all(limit=5000):
    """Batch upsert every (non-deleted) account. Returns count synced. For nightly/manual runs."""
    if not enabled():
        return 0
    n = 0
    with get_engine().connect() as c:
        emails = [r[0] for r in c.execute(text(
            "SELECT email FROM core.account WHERE deleted_at IS NULL ORDER BY id LIMIT :l"), {"l": limit})]
    for email in emails:
        try:
            with session_scope() as s:
                traits = build_traits(s, email)
            _push(traits)
            n += 1
        except Exception:
            log.exception("sync_all: failed for %s", email)
    log.info("crm_sync.sync_all synced %d profiles", n)
    return n
