# core_db/repositories/matches.py — match business records + usage events.

from datetime import datetime, timezone

from sqlalchemy import select

from core_db.models import Match, UsageEvent


def _now():
    return datetime.now(timezone.utc)


# ---- Matches -------------------------------------------------------------

def upsert_match(session, *, task_id, account_id, sport_type=None, pipeline=None,
                 uploaded_by_user_id=None, subject_person_id=None, status="uploaded",
                 **fields):
    """Create or update the canonical match record (keyed by task_id)."""
    match = session.execute(
        select(Match).where(Match.task_id == str(task_id))
    ).scalar_one_or_none()
    if match is None:
        match = Match(task_id=str(task_id), account_id=account_id, uploaded_at=_now())
        session.add(match)
    match.account_id = account_id
    if sport_type is not None:
        match.sport_type = sport_type
    if pipeline is not None:
        match.pipeline = pipeline
    if uploaded_by_user_id is not None:
        match.uploaded_by_user_id = uploaded_by_user_id
    if subject_person_id is not None:
        match.subject_person_id = subject_person_id
    if status is not None:
        match.status = status
    for k, v in fields.items():
        if hasattr(match, k):
            setattr(match, k, v)
    match.updated_at = _now()
    session.flush()
    return match


def mark_processed(session, *, task_id, kpi_summary=None, trim_s3_key=None):
    match = session.execute(
        select(Match).where(Match.task_id == str(task_id))
    ).scalar_one_or_none()
    if match is None:
        return None
    match.status = "complete"
    match.processed_at = _now()
    if kpi_summary is not None:
        match.kpi_summary = kpi_summary
    if trim_s3_key is not None:
        match.trim_s3_key = trim_s3_key
    match.updated_at = _now()
    session.flush()
    return match


def list_matches_for_account(session, account_id, include_deleted=False, limit=200):
    q = select(Match).where(Match.account_id == account_id)
    if not include_deleted:
        q = q.where(Match.deleted_at.is_(None))
    return list(session.execute(q.order_by(Match.uploaded_at.desc().nullslast()).limit(limit)).scalars())


# ---- Usage events --------------------------------------------------------

def record_usage(session, *, event_type, account_id=None, user_id=None, person_id=None,
                 ref_type=None, ref_id=None, metadata=None, occurred_at=None):
    ev = UsageEvent(
        event_type=event_type,
        account_id=account_id,
        user_id=user_id,
        person_id=person_id,
        ref_type=ref_type,
        ref_id=(str(ref_id) if ref_id is not None else None),
        event_metadata=metadata,
        occurred_at=occurred_at or _now(),
    )
    session.add(ev)
    session.flush()
    return ev
