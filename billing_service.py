#=======================================================================
# billing_service.py
#=======================================================================

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from db_init import engine
from models_billing import (
    Account,
    Member,
    UsageVideo,
    Invoice,
    InvoiceLine,
    PricingComponent,
)


def get_price(session: Session, code: str) -> PricingComponent:
    pc = (
        session.query(PricingComponent)
        .filter_by(code=code, active=True)
        .one_or_none()
    )
    if pc is None:
        raise ValueError(f"Pricing component '{code}' not found or inactive")
    return pc


def create_account_with_primary_member(
    email: str,
    primary_full_name: str,
    currency_code: str = "USD",
    external_wix_id: str | None = None,
) -> Account:
    with Session(engine) as session:
        account = Account(
            email=email,
            primary_full_name=primary_full_name,
            currency_code=currency_code,
            external_wix_id=external_wix_id,
        )
        session.add(account)
        session.flush()

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


def record_video_usage(
    account_id: int,
    member_id: int | None,
    matches: int,
    task_id: str,
    source: str = "sportai",
) -> UsageVideo:
    billable_matches = Decimal(str(matches))



    with Session(engine) as session:
        usage = UsageVideo(
            account_id=account_id,
            member_id=member_id,
            task_id=task_id,
            matches=billable_matches,
            billable_matches=billable_matches,
            source=source,
            processed_at=datetime.utcnow(),
        )
        session.add(usage)
        session.commit()
        session.refresh(usage)
        return usage


def _period_datetime_bounds(period_start: date, period_end: date) -> tuple[datetime, datetime]:
    start_at = datetime.combine(period_start, datetime.min.time())
    end_next_day = period_end + timedelta(days=1)
    end_at = datetime.combine(end_next_day, datetime.min.time())
    return start_at, end_at


def generate_invoice_for_period(account_id: int, period_start: date, period_end: date) -> Invoice:
    with Session(engine) as session:
        account = session.query(Account).filter_by(id=account_id).one()

        if not account.active:
            raise ValueError(f"Account {account_id} is inactive")

        invoice = (
            session.query(Invoice)
            .filter(
                Invoice.account_id == account_id,
                Invoice.period_start == period_start,
                Invoice.period_end == period_end,
            )
            .one_or_none()
        )

        if invoice is not None and invoice.status != "draft":
            raise ValueError(
                f"Invoice already exists for {period_start}–{period_end} with status {invoice.status}"
            )

        if invoice is None:
            invoice = Invoice(
                account_id=account_id,
                period_start=period_start,
                period_end=period_end,
                currency_code=account.currency_code,
                status="draft",
            )
            session.add(invoice)
            session.flush()
        else:
            session.query(InvoiceLine).filter_by(invoice_id=invoice.id).delete()

        base_price = get_price(session, "MEMBERSHIP_BASE_USD")
        addon_price = get_price(session, "MEMBER_ADDON_USD")
        video_price = get_price(session, "VIDEO_ANALYSIS_USD")

        if account.currency_code != base_price.currency_code:
            raise ValueError(
                f"Currency mismatch: account {account.currency_code} vs pricing {base_price.currency_code}"
            )

        total_members = (
            session.query(Member)
            .filter_by(account_id=account_id, active=True)
            .count()
        )
        extra_members = max(total_members - 1, 0)

        start_at, end_at = _period_datetime_bounds(period_start, period_end)
        total_minutes_rows = (
            session.query(UsageVideo.billable_minutes)
            .filter(
                UsageVideo.account_id == account_id,
                UsageVideo.processed_at >= start_at,
                UsageVideo.processed_at < end_at,
            )
            .all()
        )

        minutes_sum = sum((row[0] or Decimal("0")) for row in total_minutes_rows)
        hours = (Decimal(minutes_sum) / Decimal("60")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        total_amount = Decimal("0.00")

        base_line_amount = Decimal(base_price.unit_amount)
        session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                pricing_component_code=base_price.code,
                description=base_price.description,
                quantity=Decimal("1.00"),
                unit_amount=base_price.unit_amount,
                line_amount=base_line_amount,
            )
        )
        total_amount += base_line_amount

        if extra_members > 0:
            qty = Decimal(str(extra_members))
            addon_line_amount = qty * Decimal(addon_price.unit_amount)
            session.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    pricing_component_code=addon_price.code,
                    description=addon_price.description,
                    quantity=qty,
                    unit_amount=addon_price.unit_amount,
                    line_amount=addon_line_amount,
                )
            )
            total_amount += addon_line_amount

        if hours > 0:
            usage_line_amount = hours * Decimal(video_price.unit_amount)
            session.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    pricing_component_code=video_price.code,
                    description=video_price.description,
                    quantity=hours,
                    unit_amount=video_price.unit_amount,
                    line_amount=usage_line_amount,
                )
            )
            total_amount += usage_line_amount

        invoice.total_amount = total_amount
        invoice.updated_at = datetime.utcnow()

        session.commit()
        session.refresh(invoice)
        return invoice


def get_month_period(year: int, month: int) -> tuple[date, date]:
    first_day = date(year, month, 1)
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    return first_day, last_day
