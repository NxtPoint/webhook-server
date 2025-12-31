#=======================================================================
# billing_import_from_bronze.py
#=======================================================================

"""
Billing import from bronze.submission_context.

Model:
- 1 SportAI COMPLETED submission (task_id) == 1 match consumed
- Consumption is written to billing.entitlement_consumption
- Idempotent by task_id (unique constraint in DB)

Notes:
- We do NOT calculate money here (Wix/PayPal owns payments)
- We do NOT use invoices, invoice lines, pricing components, or usage_video
"""

from typing import Optional, Dict, Any

from sqlalchemy import text, select
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account
from billing_service import create_account_with_primary_member


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

    # Creates account + primary member in its own session; then reload here.
    create_account_with_primary_member(
        email=email,
        primary_full_name=primary_full_name,
        currency_code="USD",       # base currency for now
        external_wix_id=None,      # future: wire actual Wix id
    )

    acct = session.execute(
        select(Account).where(Account.email == email)
    ).scalar_one()

    return acct


def _consume_match_for_task(session: Session, account_id: int, task_id: str) -> bool:
    """
    Insert a consumption record for this task_id (1 match).
    Returns True if inserted, False if already existed.
    """
    # Rely on DB uniqueness: entitlement_consumption.unique(task_id)
    res = session.execute(
        text(
            """
            INSERT INTO billing.entitlement_consumption
                (account_id, task_id, consumed_matches, source)
            VALUES
                (:account_id, :task_id, 1, 'sportai')
            ON CONFLICT (task_id) DO NOTHING
            """
        ),
        {"account_id": account_id, "task_id": task_id},
    )

    # rowcount = 1 if inserted, 0 if conflict/no-op
    return (res.rowcount or 0) == 1


def sync_usage_from_submission_context(
    status_filter: str = "completed",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Scan bronze.submission_context and create billing.entitlement_consumption entries
    for each SportAI submission that:
      - has last_status = status_filter (default: 'completed')
      - has not already been imported (task_id already in entitlement_consumption)

    Returns counters.
    """

    with Session(engine) as session:
        rows = session.execute(
            text(
                """
                SELECT
                    task_id,
                    email,
                    customer_name,
                    last_status
                FROM bronze.submission_context
                WHERE last_status = :status
                """
            ),
            {"status": status_filter},
        ).mappings().all()

        total = 0
        skipped_missing_email = 0
        skipped_missing_task_id = 0
        skipped_already_consumed = 0
        created_consumption = 0

        for row in rows:
            total += 1

            task_id = row.get("task_id")
            if not task_id:
                skipped_missing_task_id += 1
                continue

            email = (row.get("email") or "").strip().lower()
            if not email:
                skipped_missing_email += 1
                continue

            customer_name = row.get("customer_name")

            # Ensure account exists (Wix is master identity)
            account = _find_or_create_account(
                session,
                email=email,
                customer_name=customer_name,
            )

            inserted = _consume_match_for_task(
                session=session,
                account_id=account.id,
                task_id=str(task_id),
            )

            if inserted:
                created_consumption += 1
            else:
                skipped_already_consumed += 1

        if not dry_run:
            session.commit()
        else:
            session.rollback()  # ensure no accidental writes in dry run

        return {
            "status_filter": status_filter,
            "dry_run": dry_run,
            "total_rows": total,
            "skipped_missing_task_id": skipped_missing_task_id,
            "skipped_missing_email": skipped_missing_email,
            "skipped_already_consumed": skipped_already_consumed,
            "created_consumption_rows": created_consumption,  # 1 row per task_id consumed
        }


def run_billing_import(dry_run: bool = False):
    return sync_usage_from_submission_context(dry_run=dry_run)


if __name__ == "__main__":
    out = run_billing_import(dry_run=True)
    print("[DRY RUN] sync_usage_from_submission_context result:")
    for k, v in out.items():
        print(f"  {k}: {v}")
