#=======================================================================
# billing_service.py
#=======================================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Dict, Any

from sqlalchemy import text, select
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account, Member


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
        account = session.execute(
            select(Account).where(Account.email == email)
        ).scalar_one_or_none()

        if account is None:
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

        # If it already exists, keep it stable (no accidental overwrites)
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
# Entitlements (Credits)
# ----------------------------

def grant_entitlement(
    *,
    account_id: int,
    source: str,
    plan_code: str,
    matches_granted: int,
    external_wix_id: Optional[str] = None,
    valid_from: Optional[datetime] = None,
    valid_to: Optional[datetime] = None,
    is_active: bool = True,
) -> int:
    """
    Grant match credits to an account.
    This is called from Wix webhook handlers (Step 3 later).

    Returns the inserted entitlement_grant.id.
    """
    if matches_granted < 0:
        raise ValueError("matches_granted must be >= 0")
    if not plan_code:
        raise ValueError("plan_code required")
    if source not in ("wix_subscription", "wix_payg", "manual_adjustment"):
        raise ValueError("invalid source")

    vf = valid_from or datetime.now(timezone.utc)

    with Session(engine) as session:
        res = session.execute(
            text(
                """
                INSERT INTO billing.entitlement_grant
                    (account_id, source, plan_code, external_wix_id, matches_granted, valid_from, valid_to, is_active)
                VALUES
                    (:account_id, :source, :plan_code, :external_wix_id, :matches_granted, :valid_from, :valid_to, :is_active)
                RETURNING id
                """
            ),
            {
                "account_id": account_id,
                "source": source,
                "plan_code": plan_code,
                "external_wix_id": external_wix_id,
                "matches_granted": matches_granted,
                "valid_from": vf,
                "valid_to": valid_to,
                "is_active": is_active,
            },
        )
        grant_id = int(res.scalar_one())
        session.commit()
        return grant_id


def get_remaining_matches(account_id: int) -> int:
    """
    Return remaining credits (granted - consumed) for an account.
    """
    with Session(engine) as session:
        row = session.execute(
            text(
                """
                with grants as (
                  select coalesce(sum(matches_granted),0) as g
                  from billing.entitlement_grant
                  where account_id = :account_id
                    and is_active = true
                    and (valid_to is null or valid_to >= now())
                ),
                cons as (
                  select coalesce(sum(consumed_matches),0) as c
                  from billing.entitlement_consumption
                  where account_id = :account_id
                )
                select (select g from grants) - (select c from cons) as remaining
                """
            ),
            {"account_id": account_id},
        ).one()

        remaining = row[0]
        return int(remaining or 0)


def consume_match_for_task(
    *,
    account_id: int,
    task_id: str,
    source: str = "sportai",
) -> bool:
    """
    Consume 1 match credit for a given task_id.
    Idempotent by DB unique(task_id).
    Returns True if inserted, False if already existed.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        raise ValueError("task_id required")

    with Session(engine) as session:
        res = session.execute(
            text(
                """
                INSERT INTO billing.entitlement_consumption
                    (account_id, task_id, consumed_matches, source)
                VALUES
                    (:account_id, :task_id, 1, :source)
                ON CONFLICT (task_id) DO NOTHING
                """
            ),
            {"account_id": account_id, "task_id": task_id, "source": source},
        )
        inserted = (res.rowcount or 0) == 1
        session.commit()
        return inserted


def get_usage_summary(account_id: int) -> Dict[str, Any]:
    """
    Convenience helper for UI/debugging.
    Mirrors billing.vw_customer_usage fields for a single account.
    """
    with Session(engine) as session:
        row = session.execute(
            text(
                """
                select
                  matches_granted,
                  matches_consumed,
                  matches_remaining,
                  last_processed_at
                from billing.vw_customer_usage
                where account_id = :account_id
                """
            ),
            {"account_id": account_id},
        ).mappings().one_or_none()

        if row is None:
            return {
                "account_id": account_id,
                "matches_granted": 0,
                "matches_consumed": 0,
                "matches_remaining": 0,
                "last_processed_at": None,
            }

        return {
            "account_id": account_id,
            "matches_granted": int(row["matches_granted"] or 0),
            "matches_consumed": int(row["matches_consumed"] or 0),
            "matches_remaining": int(row["matches_remaining"] or 0),
            "last_processed_at": row["last_processed_at"],
        }
