#=======================================================================
# billing_service.py
#=======================================================================

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account, Member, UsageVideo, PricingComponent


# ----------------------------
# Pricing
# ----------------------------

def get_price(session: Session, code: str) -> PricingComponent:
    pc = (
        session.query(PricingComponent)
        .filter_by(code=code, active=True)
        .one_or_none()
    )
    if pc is None:
        raise ValueError(f"Pricing component '{code}' not found or inactive")
    return pc


# ----------------------------
# Accounts / Members
# ----------------------------

def create_account_with_primary_member(
    email: str,
    primary_full_name: str,
    currency_code: str = "USD",
    external_wix_id: str | None = None,
) -> Account:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email required")

    primary_full_name = (primary_full_name or "").strip() or email

    with Session(engine) as session:
        account = Account(
            email=email,
            primary_full_name=primary_full_name,
            currency_code=currency_code,
            external_wix_id=external_wix_id,
        )
        session.add(account)
        session.flush()  # account.id

        primary_member = Member(
            account_id=account.id,
            full_name=primary_full_name,
            is_primary=True,
        )
        session.add(primary_member)

        session.commit()
        session.refresh(account)
        return account


def add_member_to_account(account_id: int, full_name: str) -> Member:
    full_name = (full_name or "").strip()
    if not full_name:
        raise ValueError("full_name required")

    with Session(engine) as session:
        member = Member(
            account_id=account_id,
            full_name=full_name,
            is_primary=False,
        )
        session.add(member)
        session.commit()
        session.refresh(member)
        return member


# ----------------------------
# Usage
# ----------------------------

def record_video_usage(
    account_id: int,
    member_id: int | None,
    task_id: str,
    *,
    matches: int | float | None = None,
    billable_matches: int | float | None = None,
    video_minutes: int | float | None = None,         # legacy support
    billable_minutes: int | float | None = None,      # legacy support
    source: str = "sportai",
) -> UsageVideo:
    """
    Canonical (now): per-match usage via matches/billable_matches.
    Legacy-compatible: minutes can still be written if supplied.

    IMPORTANT: UsageVideo(task_id) must be unique at the DB level for true idempotency.
    """

    task_id = (task_id or "").strip()
    if not task_id:
        raise ValueError("task_id required")

    def _dec(v) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v))

    m = _dec(matches)
    bm = _dec(billable_matches if billable_matches is not None else matches)

    vm = _dec(video_minutes)
    bvm = _dec(billable_minutes if billable_minutes is not None else video_minutes)

    with Session(engine) as session:
        # Idempotency guard (keeps safe even if DB constraint missing)
        existing = session.execute(
            select(UsageVideo).where(UsageVideo.task_id == task_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        usage = UsageVideo(
            account_id=account_id,
            member_id=member_id,
            task_id=task_id,
            matches=m,
            billable_matches=bm,
            video_minutes=vm,
            billable_minutes=bvm,
            source=source,
            processed_at=datetime.utcnow(),
        )
        session.add(usage)
        session.commit()
        session.refresh(usage)
        return usage
