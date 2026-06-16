# core_db/repositories/subscriptions.py — plans, subscriptions, and the credit ledger.

from datetime import datetime, timezone

from sqlalchemy import func, select

from core_db.models import CreditLedger, Plan, Subscription, SubscriptionEvent


def _now():
    return datetime.now(timezone.utc)


# ---- Plans ---------------------------------------------------------------

def upsert_plan(session, *, code, name, plan_type, price_cents=0, currency="USD",
                billing_interval=None, matches_included=0, techniques_included=0,
                external_wix_plan_id=None, is_active=True):
    plan = session.execute(select(Plan).where(Plan.code == code)).scalar_one_or_none()
    if plan is None:
        plan = Plan(code=code)
        session.add(plan)
    plan.name = name
    plan.plan_type = plan_type
    plan.price_cents = price_cents
    plan.currency = currency
    plan.billing_interval = billing_interval
    plan.matches_included = matches_included
    plan.techniques_included = techniques_included
    plan.external_wix_plan_id = external_wix_plan_id
    plan.is_active = is_active
    plan.updated_at = _now()
    session.flush()
    return plan


def _mrr_cents(price_cents, plan_type, billing_interval):
    if plan_type != "recurring":
        return 0
    if billing_interval == "year":
        return round((price_cents or 0) / 12)
    return price_cents or 0


# ---- Subscriptions -------------------------------------------------------

def get_active_subscription(session, account_id):
    return session.execute(
        select(Subscription).where(
            Subscription.account_id == account_id, Subscription.status == "active"
        )
    ).scalar_one_or_none()


def upsert_subscription(session, *, account_id, plan_code=None, plan_type=None,
                        external_plan_id=None, status="active", billing_provider="wix_paypal",
                        price_cents=0, billing_interval=None, matches_per_period=None,
                        current_period_start=None, current_period_end=None, plan_id=None):
    """Create or update the account's subscription. Keeps one active row per account
    (the partial unique index enforces it). Computes mrr_cents."""
    sub = get_active_subscription(session, account_id)
    if sub is None:
        sub = Subscription(account_id=account_id, started_at=_now())
        session.add(sub)
    sub.plan_id = plan_id
    sub.plan_code = plan_code
    sub.plan_type = plan_type
    sub.external_plan_id = external_plan_id
    sub.status = status
    sub.billing_provider = billing_provider
    sub.mrr_cents = _mrr_cents(price_cents, plan_type, billing_interval)
    sub.matches_per_period = matches_per_period
    if current_period_start is not None:
        sub.current_period_start = current_period_start
    if current_period_end is not None:
        sub.current_period_end = current_period_end
    if status in ("cancelled", "expired"):
        sub.cancelled_at = _now()
    sub.updated_at = _now()
    session.flush()
    return sub


def record_subscription_event(session, *, event_id, account_id, event_type,
                              provider="wix", subscription_id=None, payload=None):
    """Idempotent on event_id — returns (event, created:bool)."""
    existing = session.execute(
        select(SubscriptionEvent).where(SubscriptionEvent.event_id == event_id)
    ).scalar_one_or_none()
    if existing:
        return existing, False
    ev = SubscriptionEvent(
        event_id=event_id, account_id=account_id, event_type=event_type,
        provider=provider, subscription_id=subscription_id, payload=payload,
    )
    session.add(ev)
    session.flush()
    return ev, True


# ---- Credit ledger (append-only; balance = SUM(deltas)) ------------------

def grant_credits(session, *, account_id, matches=0, techniques=0, source="manual",
                  plan_code=None, external_wix_id=None, valid_to=None):
    """Append a grant entry. Idempotent on (account, source, plan_code, external_wix_id)
    via the partial unique index — a duplicate grant is a no-op."""
    if external_wix_id is not None:
        dup = session.execute(
            select(CreditLedger).where(
                CreditLedger.account_id == account_id,
                CreditLedger.entry_type == "grant",
                CreditLedger.source == source,
                CreditLedger.plan_code == plan_code,
                CreditLedger.external_wix_id == external_wix_id,
            )
        ).scalar_one_or_none()
        if dup:
            return dup
    entry = CreditLedger(
        account_id=account_id, entry_type="grant",
        matches_delta=matches, techniques_delta=techniques,
        source=source, plan_code=plan_code, external_wix_id=external_wix_id,
        valid_from=_now(), valid_to=valid_to,
    )
    session.add(entry)
    session.flush()
    return entry


def consume_match(session, *, account_id, task_id, source="match_upload"):
    """Append a consume entry for a match. Idempotent: a task is consumed once
    (partial unique index on (ref_type, ref_id) where entry_type='consume')."""
    dup = session.execute(
        select(CreditLedger).where(
            CreditLedger.entry_type == "consume",
            CreditLedger.ref_type == "match",
            CreditLedger.ref_id == str(task_id),
        )
    ).scalar_one_or_none()
    if dup:
        return dup
    entry = CreditLedger(
        account_id=account_id, entry_type="consume",
        matches_delta=-1, techniques_delta=0,
        source=source, ref_type="match", ref_id=str(task_id),
    )
    session.add(entry)
    session.flush()
    return entry


def balance(session, account_id):
    """Current credit balance for an account."""
    row = session.execute(
        select(
            func.coalesce(func.sum(CreditLedger.matches_delta), 0),
            func.coalesce(func.sum(CreditLedger.techniques_delta), 0),
        ).where(CreditLedger.account_id == account_id)
    ).one()
    return {"matches_remaining": int(row[0]), "techniques_remaining": int(row[1])}


def total_mrr_cents(session):
    return int(session.execute(
        select(func.coalesce(func.sum(Subscription.mrr_cents), 0)).where(
            Subscription.status == "active", Subscription.plan_type == "recurring"
        )
    ).scalar_one())
