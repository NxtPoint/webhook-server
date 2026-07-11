# core_db/repositories/acquisition.py — signup ad/UTM attribution capture (Google Ads / Client-360).
#
# Mirrors nextpoint's core/repositories/acquisition.py. record_acquisition() upserts the 1:1
# core.acquisition row for a signed-in buyer, keyed by email → core.app_user. FIRST-TOUCH WINS: a
# column is filled only while still NULL, so the original ad click (gclid/utm) is never overwritten by
# a later organic visit. Feeds the Google Ads offline-conversion feed (offline_conversions/). Takes an
# explicit session, never commits (callers compose via session_scope()).

from datetime import datetime

from sqlalchemy import text

from core_db.repositories.accounts import get_user_by_email

# Incoming attr key -> core.acquisition column. Values are trimmed + length-capped on write.
_FIELD_MAP = {
    "source": "source", "medium": "medium", "campaign": "campaign", "term": "term",
    "content": "content", "referrer": "referrer", "landing_page": "landing_page",
    "gclid": "gclid", "fbclid": "fbclid",
}
_CAP = 512


def _clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return v[:_CAP] or None


def record_acquisition(session, *, email, attr=None):
    """Upsert the buyer's core.acquisition row (first-touch wins). Returns the app_user id, or None
    when there's nothing to attribute (no ad/UTM params) or the email has no core.app_user."""
    attr = attr or {}
    if not any(_clean(attr.get(k)) for k in _FIELD_MAP):
        return None
    if not (email and email.strip()):
        return None
    user = get_user_by_email(session, email.strip().lower())
    if user is None:
        return None

    now = datetime.utcnow()
    session.execute(text("""
        INSERT INTO core.acquisition (user_id, first_seen_at, signed_up_at, created_at)
        VALUES (:u, :t, :t, :t)
        ON CONFLICT (user_id) DO NOTHING
    """), {"u": user.id, "t": now})

    # First-touch wins: only fill a column while it's still NULL.
    sets, params = [], {"u": user.id}
    for src_key, col in _FIELD_MAP.items():
        val = _clean(attr.get(src_key))
        if val:
            sets.append(f"{col} = COALESCE({col}, :{col})")
            params[col] = val
    if sets:
        session.execute(text(f"UPDATE core.acquisition SET {', '.join(sets)} WHERE user_id = :u"), params)
    return user.id
