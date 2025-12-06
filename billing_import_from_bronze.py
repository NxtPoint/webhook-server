#=========================== git add billing_api.py ====================
#=======================================================================

"""
Billing import from SportAI transaction log (bronze.submission_context).

Each COMPLETED submission becomes one billing.usage_video row.

Rules:
- Bill only if last_status = 'completed'.
- Bill processing time: (ingest_finished_at - created_at) in minutes.
- Customer identity is email; customer_name is from Wix (column).
- One billing.usage_video per task_id for idempotency.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import text, select
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account, UsageVideo
from billing_service import create_account_with_primary_member, record_video_usage


def _minutes_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    """
    Return minutes between two datetimes, or None if invalid.
    """
    if not start or not end:
        return None
    if end <= start:
        return None
    delta = end - start
    return delta.total_seconds() / 60.0


def _find_or_create_account(session: Session, email: str, customer_name: Optional[str]) -> Account:
    """
    Look up Account by email. If missing, create one using Wix customer_name
    as primary_full_name (fallback to email if empty).
    """
    acct = session.execute(
        select(Account).where(Account.email == email)
    ).scalar_one_or_none()

    if acct is not None:
        return acct

    primary_full_name = (customer_name or email).strip() or email

    # Use the service to create account + primary member
    acct = create_account_with_primary_member(
        email=email,
        primary_full_name=primary_full_name,
        currency_code="USD",       # base currency for now
        external_wix_id=None,      # future: wire actual Wix id
    )

    # Reload into this session
    acct = session.execute(
        select(Account).where(Account.email == email)
    ).scalar_one()

    return acct


def sync_usage_from_submission_context(
    status_filter: str = "completed",
    dry_run: bool = True,
) -> None:
    """
    Scan bronze.submission_context and create billing.usage_video entries
    for each SportAI submission that:
      - has last_status = status_filter (default: 'completed')
      - has not already been imported (task_id in billing.usage_video)

    Processing minutes = ingest_finished_at - created_at.
    """

    with Session(engine) as session:
        rows = session.execute(
            text(
                """
                SELECT
                    task_id,            -- SportAI submission id
                    email,              -- customer email from Wix
                    customer_name,      -- customer full name from Wix
                    last_status,
                    created_at,
                    ingest_finished_at
                FROM bronze.submission_context
                WHERE last_status = :status
                """
            ),
            {"status": status_filter},
        ).mappings().all()

        total = 0
        skipped_already_imported = 0
        skipped_no_duration = 0
        created_usage = 0

        for row in rows:
            total += 1
            task_id = row["task_id"]
            email = row["email"]
            customer_name = row["customer_name"]
            last_status = row["last_status"]
            created_at = row["created_at"]
            ingest_finished_at = row["ingest_finished_at"]

            # Safety, though WHERE already filters
            if last_status != status_filter:
                continue

            # Idempotency: skip if this submission already billed
            existing = session.execute(
                select(UsageVideo).where(UsageVideo.task_id == task_id)
            ).scalar_one_or_none()
            if existing is not None:
                skipped_already_imported += 1
                continue

            minutes = _minutes_between(created_at, ingest_finished_at)
            if minutes is None or minutes <= 0:
                skipped_no_duration += 1
                continue

            if dry_run:
                created_usage += 1
                continue

            # Ensure account exists based on email (Wix is master)
            account = _find_or_create_account(
                session,
                email=email,
                customer_name=customer_name,
            )

            # Use billing_service to apply standard pricing logic
            record_video_usage(
                account_id=account.id,
                member_id=None,          # later: map to specific member if needed
                video_minutes=minutes,
                task_id=task_id,         # ensures no double billing
            )

            created_usage += 1

        if dry_run:
            print(f"[DRY RUN] Total rows with last_status='{status_filter}': {total}")
            print(f"[DRY RUN] Would skip already imported: {skipped_already_imported}")
            print(f"[DRY RUN] Would skip no/invalid processing duration: {skipped_no_duration}")
            print(f"[DRY RUN] Would create usage rows: {created_usage}")
        else:
            session.commit()
            print(f"Processed rows with last_status='{status_filter}': {total}")
            print(f"Skipped already imported: {skipped_already_imported}")
            print(f"Skipped no/invalid processing duration: {skipped_no_duration}")
            print(f"Created usage rows: {created_usage}")


if __name__ == "__main__":
    # First run as dry-run. Flip to False once you are happy.
    sync_usage_from_submission_context(dry_run=True)
