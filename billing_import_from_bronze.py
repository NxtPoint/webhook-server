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
from billing_service import create_account_with_primary_member, consume_match_for_task


def _find_or_create_account(session: Session, email: str, customer_name: Optional[str]) -> Account:
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
        currency_code="USD",
        external_wix_id=None,
    )

    acct = session.execute(
        select(Account).where(Account.email == email)
    ).scalar_one_or_none()

    if acct is None:
        raise RuntimeError("account create succeeded but account not found on reload")

    return acct


def sync_usage_from_submission_context(
    status_filter: str = "completed",
    dry_run: bool = True,
) -> Dict[str, Any]:
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
                WHERE lower(coalesce(last_status,'')) = lower(:status)
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

            account = _find_or_create_account(session, email=email, customer_name=customer_name)

            inserted = consume_match_for_task(
                account_id=int(account.id),
                task_id=str(task_id),
                source="sportai",
            )

            if inserted:
                created_consumption += 1
            else:
                skipped_already_consumed += 1

        if not dry_run:
            session.commit()
        else:
            session.rollback()

        return {
            "status_filter": status_filter,
            "dry_run": dry_run,
            "total_rows": total,
            "skipped_missing_task_id": skipped_missing_task_id,
            "skipped_missing_email": skipped_missing_email,
            "skipped_already_consumed": skipped_already_consumed,
            "created_consumption_rows": created_consumption,
        }


def run_billing_import(dry_run: bool = False):
    return sync_usage_from_submission_context(dry_run=dry_run)


def sync_usage_for_task_id(task_id: str, dry_run: bool = True) -> Dict[str, Any]:
    task_id = (task_id or "").strip()
    if not task_id:
        raise ValueError("task_id required")

    with Session(engine) as session:
        row = session.execute(
            text(
                """
                SELECT task_id, email, customer_name, last_status
                FROM bronze.submission_context
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task_id},
        ).mappings().first()

        if not row:
            return {"ok": False, "error": "task_id not found in bronze.submission_context"}

        status = str((row.get("last_status") or "")).strip().lower()
        if status != "completed":
            return {"ok": False, "error": f"task_id not completed (status={status})"}

        email = (row.get("email") or "").strip().lower()
        if not email:
            return {"ok": False, "error": "missing email on submission_context row"}

        account = _find_or_create_account(session, email=email, customer_name=row.get("customer_name"))

        inserted = consume_match_for_task(
            account_id=int(account.id),
            task_id=str(task_id),
            source="sportai",
        )

        if not dry_run:
            session.commit()
        else:
            session.rollback()

        return {
            "ok": True,
            "dry_run": dry_run,
            "task_id": task_id,
            "inserted": bool(inserted),
            "last_status": row.get("last_status"),
        }


if __name__ == "__main__":
    out = run_billing_import(dry_run=True)
    print("[DRY RUN] sync_usage_from_submission_context result:")
    for k, v in out.items():
        print(f"  {k}: {v}")
