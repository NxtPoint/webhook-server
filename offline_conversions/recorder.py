# offline_conversions/recorder.py — money-event → gclid conversion row.
#
# SHARED, PORTABLE. record_from_emit() is called from the single event funnel (each repo's
# track()/emit() worker). For a mapped conversion event it resolves the buyer's gclid (captured
# earlier on core.acquisition) and writes a core.offline_conversion row that the CSV feed later serves
# to Google Ads. No gclid → no row (organic buyer; nothing to attribute). NEVER raises.
#
# THE ONLY per-repo glue is CONVERSION_MAP. It holds the money events of BOTH repos (an event that
# never fires in a given repo is simply inert), so this file is byte-identical across them.

import logging
from datetime import datetime, timezone

from sqlalchemy import text

log = logging.getLogger("offline_conversions.recorder")

# emit/track event name -> how to turn its payload into a conversion.
#   action:       the Google Ads conversion action name this maps to (MUST match the one in Ads).
#   value_key:    payload key holding the value in MINOR units (cents). Missing/None -> 0 (count-only).
#   currency_key: payload key holding the ISO currency. Missing -> 'ZAR'.
CONVERSION_MAP = {
    # CourtFlow (nextpoint) — a real payment carries the amount → value-based.
    "payment_succeeded":    {"action": "Offline purchase", "value_key": "amount_minor", "currency_key": "currency"},
    # ten-fifty5 — PAYG top-up + recurring start. No amount on the event yet → recorded count-only (0).
    "credit_purchased":     {"action": "Offline purchase", "value_key": "amount_minor", "currency_key": "currency"},
    "subscription_started": {"action": "Offline purchase", "value_key": "amount_minor", "currency_key": "currency"},
}


def _resolve_gclid(session, *, email, iam_user_id):
    """core.acquisition.gclid for a buyer, via email (→ core.app_user) or the platform's iam.user UUID
    (→ core.person bridge, where that column exists). Each branch is SAVEPOINT-guarded so a schema that
    lacks core.person.iam_user_id (repos differ) can't poison the transaction. Returns gclid or None."""
    if email:
        try:
            with session.begin_nested():
                row = session.execute(text("""
                    SELECT a.gclid FROM core.acquisition a
                    JOIN core.app_user u ON u.id = a.user_id
                    WHERE lower(u.email) = :email AND a.gclid IS NOT NULL
                    LIMIT 1
                """), {"email": email}).first()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    if iam_user_id:
        try:
            with session.begin_nested():
                row = session.execute(text("""
                    SELECT a.gclid FROM core.acquisition a
                    JOIN core.person p ON p.user_id = a.user_id
                    WHERE p.iam_user_id = :iam AND a.gclid IS NOT NULL
                    LIMIT 1
                """), {"iam": str(iam_user_id)}).first()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    return None


def record_from_emit(session, event, payload):
    """If `event` is a mapped conversion and the buyer arrived via a gclid, ledger it. Idempotent
    (ON CONFLICT DO NOTHING against the order ref / click+action+second). Returns True if written."""
    cfg = CONVERSION_MAP.get(event)
    if not cfg:
        return False
    email = (payload.get("email") or "").strip().lower() or None
    iam_user_id = payload.get("user_id")   # some producers pass the iam.user UUID here (not a core id)
    gclid = _resolve_gclid(session, email=email, iam_user_id=iam_user_id)
    if not gclid:
        return False
    try:
        value_minor = int(payload.get(cfg["value_key"]) or 0)
    except (TypeError, ValueError):
        value_minor = 0
    currency = (str(payload.get(cfg["currency_key"]) or "ZAR")).upper()[:3]
    source_ref = payload.get("ref_id") or payload.get("order_id")
    now = datetime.now(timezone.utc)   # the purchase moment — always after the ad click
    session.execute(text("""
        INSERT INTO core.offline_conversion
            (gclid, action_name, occurred_at, value_minor, currency, source_event, source_ref)
        VALUES (:g, :a, :t, :v, :c, :e, :r)
        ON CONFLICT DO NOTHING
    """), {"g": gclid, "a": cfg["action"], "t": now, "v": value_minor, "c": currency,
           "e": event, "r": (str(source_ref) if source_ref else None)})
    return True
