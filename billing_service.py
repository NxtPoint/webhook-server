#=======================================================================
# billing_service.py  (FINAL BASELINE v2)
# - Adds idempotency for entitlement grants (prevents double credits on retries)
# - Adds helper for monthly no-rollover resets (expire excess via consumption)
#=======================================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Dict, Any

from sqlalchemy import text, select
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account, Member

import uuid

# ----------------------------
# Internal helpers
# ----------------------------

def _norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _norm_role(role: str | None) -> str:
    """
    Canonical Render roles: 'player_parent' | 'coach'

    Accept:
      - 'player' (child rows from Wix) -> normalize to 'player_parent'
    """
    r = (role or "").strip().lower()
    if r == "player":
        r = "player_parent"
    return r


def _validate_role(role: str | None) -> str:
    r = _norm_role(role)
    if r not in ("player_parent", "coach"):
        raise ValueError("invalid role")
    return r

def _to_uuid(v: str | uuid.UUID) -> uuid.UUID:
    if isinstance(v, uuid.UUID):
        return v
    s = (v or "").strip()
    if not s:
        raise ValueError("task_id required")
    try:
        return uuid.UUID(s)
    except Exception:
        # deterministic UUID from arbitrary string
        return uuid.uuid5(uuid.NAMESPACE_URL, s)


# ----------------------------
# Accounts / Members
# ----------------------------

def create_account_with_primary_member(
    email: str,
    primary_full_name: str,
    currency_code: str = "USD",
    external_wix_id: str | None = None,
    role: str = "player_parent",
) -> Account:
    """
    Create account + primary member if account does not exist.

    Stability rules (to avoid accidental overwrites):
      - If account exists: do NOT overwrite fields (email, name, currency, external_wix_id, role)
        except:
          - If external_wix_id is provided and account.external_wix_id is empty -> set it
          - If no primary member exists -> create it

    This function is intentionally conservative. Wix 'sync_account' owns full snapshot replacement.
    """
    email_n = _norm_email(email)
    if not email_n:
        raise ValueError("email required")

    primary_full_name_n = (primary_full_name or "").strip() or email_n
    role_n = _validate_role(role)

    with Session(engine) as session:
        account = session.execute(
            select(Account).where(Account.email == email_n)
        ).scalar_one_or_none()

        if account is None:
            account = Account(
                email=email_n,
                primary_full_name=primary_full_name_n,
                currency_code=(currency_code or "USD").strip().upper() or "USD",
                external_wix_id=external_wix_id,
            )
            session.add(account)
            session.flush()  # account.id

            primary_member = Member(
                account_id=account.id,
                full_name=primary_full_name_n,
                is_primary=True,
                role=role_n,
                active=True,
            )
            session.add(primary_member)

            session.commit()
            session.refresh(account)
            return account

        # If Wix id is newly available, persist it (safe, non-destructive)
        if external_wix_id and not getattr(account, "external_wix_id", None):
            account.external_wix_id = external_wix_id

        # Ensure there is exactly one primary member at least (guard rail)
        primary_exists = session.execute(
            select(Member.id).where(Member.account_id == account.id, Member.is_primary == True)  # noqa: E712
        ).scalar_one_or_none()

        if primary_exists is None:
            session.add(
                Member(
                    account_id=account.id,
                    full_name=primary_full_name_n,
                    is_primary=True,
                    role=role_n,
                    active=True,
                )
            )
            session.commit()

        return account


def add_member_to_account(
    account_id: int,
    full_name: str,
    role: str = "player_parent",
) -> Member:
    full_name_n = (full_name or "").strip()
    if not full_name_n:
        raise ValueError("full_name required")

    role_n = _validate_role(role)

    with Session(engine) as session:
        member = Member(
            account_id=account_id,
            full_name=full_name_n,
            is_primary=False,
            role=role_n,
            active=True,
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
    if matches_granted < 0:
        raise ValueError("matches_granted must be >= 0")
    if not plan_code:
        raise ValueError("plan_code required")
    if source not in ("wix_subscription", "wix_payg", "manual_adjustment"):
        raise ValueError("invalid source")

    vf = valid_from or datetime.now(timezone.utc)

    with Session(engine) as session:
        # ---------- IDEMPOTENCY GUARD ----------
        if external_wix_id:
            existing = session.execute(
                text("""
                    SELECT id
                    FROM billing.entitlement_grant
                    WHERE account_id = :account_id
                      AND source = :source
                      AND plan_code = :plan_code
                      AND external_wix_id = :external_wix_id
                    LIMIT 1
                """),
                {
                    "account_id": account_id,
                    "source": source,
                    "plan_code": plan_code,
                    "external_wix_id": external_wix_id,
                },
            ).scalar_one_or_none()

            if existing is not None:
                return int(existing)
        # --------------------------------------

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
    Remaining credits = active grants (not expired) minus consumptions.
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

def remaining_for_window(account_id: int, start_dt: datetime, end_dt: datetime) -> int:
    with Session(engine) as session:
        row = session.execute(
            text("""
                WITH grants AS (
                  SELECT COALESCE(SUM(matches_granted), 0) AS g
                  FROM billing.entitlement_grant
                  WHERE account_id = :account_id
                    AND is_active = true
                    AND valid_from >= :start_dt
                    AND valid_from < :end_dt
                ),
                cons AS (
                  SELECT COALESCE(SUM(consumed_matches), 0) AS c
                  FROM billing.entitlement_consumption
                  WHERE account_id = :account_id
                    AND consumed_at >= :start_dt
                    AND consumed_at < :end_dt
                )
                SELECT (SELECT g FROM grants) - (SELECT c FROM cons) AS remaining
            """),
            {"account_id": account_id, "start_dt": start_dt, "end_dt": end_dt},
        ).one()

        return int(row[0] or 0)

def consume_match_for_task(
    *,
    account_id: int,
    task_id: str,
    source: str = "sportai",
) -> bool:
    """
    Consume 1 match credit for task_id.
    Idempotent by DB unique(task_id).
    Returns True if inserted, False if already existed.
    """
    task_id_n = (task_id or "").strip()
    if not task_id_n:
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
            {"account_id": account_id, "task_id": task_id_n, "source": source},
        )
        inserted = (res.rowcount or 0) == 1
        session.commit()
        return inserted


def consume_matches_for_task(
    *,
    account_id: int,
    task_id: str,
    consumed_matches: int = 1,
    source: str = "sportai",
) -> bool:
    """
    Consume N match credits for task_id.
    Idempotent by DB unique(task_id).
    task_id column is UUID, so we normalize to UUID (uuid5 for non-uuid strings).
    Returns True if inserted, False if already existed.
    """
    if consumed_matches <= 0:
        raise ValueError("consumed_matches must be > 0")

    task_uuid = _to_uuid(task_id)

    with Session(engine) as session:
        res = session.execute(
            text(
                """
                INSERT INTO billing.entitlement_consumption
                    (account_id, task_id, consumed_matches, source)
                VALUES
                    (:account_id, :task_id, :consumed_matches, :source)
                ON CONFLICT (task_id) DO NOTHING
                """
            ),
            {
                "account_id": account_id,
                "task_id": str(task_uuid),
                "consumed_matches": int(consumed_matches),
                "source": source,
            },
        )
        inserted = (res.rowcount or 0) == 1
        session.commit()
        return inserted



def get_usage_summary(account_id: int) -> Dict[str, Any]:
    """
    Convenience helper for UI/debugging.
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


# ----------------------------
# Legacy-safe helper (KEEP)
# ----------------------------

def sync_members_for_account(
    *,
    account_id: int,
    members: list[dict],
) -> dict:
    """
    Keep this function for compatibility, but DO NOT use it for Wix onboarding.

    Your chosen best practice is:
      - Wix -> /api/billing/sync_account replaces the snapshot atomically.

    This function remains as a conservative "ensure rows exist" helper only.
    """
    if not isinstance(members, list) or not members:
        return {"ok": True, "inserted": 0, "note": "no members provided"}

    cleaned = []
    for m in members:
        if not isinstance(m, dict):
            continue
        name = (m.get("full_name") or "").strip()
        if not name:
            continue
        cleaned.append(
            {
                "full_name": name,
                "is_primary": bool(m.get("is_primary")),
                "role": _norm_role(m.get("role") or "player_parent") or "player_parent",
            }
        )

    if not cleaned:
        return {"ok": True, "inserted": 0, "note": "no valid members"}

    # ensure exactly one primary in payload
    primaries = [i for i, m in enumerate(cleaned) if m["is_primary"]]
    if len(primaries) == 0:
        cleaned[0]["is_primary"] = True
    else:
        keep = primaries[0]
        for i in primaries[1:]:
            cleaned[i]["is_primary"] = False

    inserted = 0

    with Session(engine) as session:
        existing = session.execute(
            select(Member).where(Member.account_id == account_id)
        ).scalars().all()

        def _exists(name: str, is_primary: bool) -> bool:
            n = name.strip().lower()
            for em in existing:
                if (em.full_name or "").strip().lower() == n and bool(em.is_primary) == bool(is_primary):
                    return True
            return False

        for m in cleaned:
            role_n = _validate_role(m.get("role"))
            if _exists(m["full_name"], m["is_primary"]):
                continue

            session.add(
                Member(
                    account_id=account_id,
                    full_name=m["full_name"],
                    is_primary=m["is_primary"],
                    role=role_n,
                    active=True,
                )
            )
            inserted += 1

        # enforce exactly one primary in DB
        prim_db = session.execute(
            select(Member)
            .where(Member.account_id == account_id, Member.is_primary == True)  # noqa: E712
            .order_by(Member.created_at.asc())
        ).scalars().all()

        if len(prim_db) == 0:
            first = session.execute(
                select(Member).where(Member.account_id == account_id).order_by(Member.created_at.asc())
            ).scalars().first()
            if first:
                first.is_primary = True
        elif len(prim_db) > 1:
            keep = prim_db[0]
            for extra in prim_db[1:]:
                extra.is_primary = False

        session.commit()

    return {"ok": True, "inserted": inserted}
