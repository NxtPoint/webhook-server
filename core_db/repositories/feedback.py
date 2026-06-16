# core_db/repositories/feedback.py — NPS, surveys, support tickets.

from datetime import datetime, timezone

from sqlalchemy import func, select

from core_db.models import NpsResponse, SurveyResponse, Ticket, TicketMessage


def _now():
    return datetime.now(timezone.utc)


def _nps_bucket(score):
    if score is None:
        return None
    if score >= 9:
        return "promoter"
    if score >= 7:
        return "passive"
    return "detractor"


def record_nps(session, *, score, account_id=None, user_id=None, comment=None, survey_id=None):
    resp = NpsResponse(
        account_id=account_id, user_id=user_id, score=score,
        bucket=_nps_bucket(score), comment=comment, survey_id=survey_id,
    )
    session.add(resp)
    session.flush()
    return resp


def nps_score(session):
    """Net Promoter Score (% promoters − % detractors) across all responses."""
    total = session.execute(select(func.count()).select_from(NpsResponse)).scalar_one()
    if not total:
        return {"nps": None, "responses": 0}
    proms = session.execute(
        select(func.count()).select_from(NpsResponse).where(NpsResponse.bucket == "promoter")
    ).scalar_one()
    dets = session.execute(
        select(func.count()).select_from(NpsResponse).where(NpsResponse.bucket == "detractor")
    ).scalar_one()
    return {"nps": round((proms - dets) * 100.0 / total, 1), "responses": total}


def record_survey(session, *, survey_key, responses, account_id=None, user_id=None):
    resp = SurveyResponse(
        survey_key=survey_key, responses=responses, account_id=account_id, user_id=user_id,
    )
    session.add(resp)
    session.flush()
    return resp


def open_ticket(session, *, subject, body, account_id=None, user_id=None,
                channel="portal", priority=None, first_message=None, author="customer"):
    ticket = Ticket(
        subject=subject, body=body, account_id=account_id, user_id=user_id,
        channel=channel, priority=priority, status="open",
    )
    session.add(ticket)
    session.flush()
    if first_message:
        add_ticket_message(session, ticket_id=ticket.id, author=author, body=first_message)
    return ticket


def add_ticket_message(session, *, ticket_id, author, body):
    msg = TicketMessage(ticket_id=ticket_id, author=author, body=body)
    session.add(msg)
    session.flush()
    return msg


def set_ticket_status(session, ticket_id, status):
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        return None
    ticket.status = status
    ticket.updated_at = _now()
    if status in ("resolved", "closed"):
        ticket.resolved_at = _now()
    session.flush()
    return ticket
