# models_billing.py — SQLAlchemy ORM models and schema bootstrap for the billing schema.
#
# Defines the billing.account, billing.member, billing.entitlement_grant, and
# billing.entitlement_consumption tables plus the billing.vw_customer_usage view.
# Schema is created idempotently on import via billing_init().
#
# Business rules:
#   - Account: one per customer email, has active flag for termination
#   - Member: multiple per account (primary + children/coaches), soft-delete via active flag
#   - EntitlementGrant: credits added — unique on (account_id, source, plan_code, external_wix_id)
#   - EntitlementConsumption: credits used — unique on task_id (1 task = 1 match consumed)
#   - vw_customer_usage: view computing matches_granted, matches_consumed, matches_remaining

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    Date,
    DateTime,
    CHAR,
    ForeignKey,
    text,
)
from sqlalchemy.orm import relationship, declarative_base

# Local Base for billing models
Base = declarative_base()


# ----------------------------
# Core identity
# ----------------------------

class Account(Base):
    __tablename__ = "account"
    __table_args__ = {"schema": "billing"}

    id = Column(BigInteger, primary_key=True)
    external_wix_id = Column(String, nullable=True)

    email = Column(String, nullable=False, unique=True)
    primary_full_name = Column(String, nullable=False)

    currency_code = Column(CHAR(3), nullable=False, server_default=text("'USD'"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    members = relationship(
        "Member",
        back_populates="account",
        cascade="all, delete-orphan",
    )

    entitlement_grants = relationship(
        "EntitlementGrant",
        back_populates="account",
        cascade="all, delete-orphan",
    )

    entitlement_consumptions = relationship(
        "EntitlementConsumption",
        back_populates="account",
        cascade="all, delete-orphan",
    )


class Member(Base):
    __tablename__ = "member"
    __table_args__ = {"schema": "billing"}

    id = Column(BigInteger, primary_key=True)

    account_id = Column(
        BigInteger,
        ForeignKey("billing.account.id", ondelete="CASCADE"),
        nullable=False,
    )

    full_name = Column(String, nullable=False)
    email = Column(String, nullable=True)  # child email only; primary should be NULL
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    role = Column(String, nullable=False, server_default=text("'player_parent'"))
    active = Column(Boolean, nullable=False, server_default=text("true"))

    # Profile fields (Locker Room)
    surname = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    utr = Column(String, nullable=True)
    dominant_hand = Column(String, nullable=True)   # left / right
    country = Column(String, nullable=True)
    area = Column(String, nullable=True)

    # Child profile fields (Players' Enclosure)
    dob = Column(Date, nullable=True)
    skill_level = Column(String, nullable=True)
    club_school = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    profile_photo_url = Column(String, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    account = relationship("Account", back_populates="members")


# ----------------------------
# Entitlements (credits model)
# ----------------------------

class EntitlementGrant(Base):
    __tablename__ = "entitlement_grant"
    __table_args__ = {"schema": "billing"}

    id = Column(BigInteger, primary_key=True)

    account_id = Column(
        BigInteger,
        ForeignKey("billing.account.id", ondelete="CASCADE"),
        nullable=False,
    )

    source = Column(String, nullable=False)       # wix_subscription / wix_payg / manual_adjustment
    plan_code = Column(String, nullable=False)
    external_wix_id = Column(String, nullable=True)

    matches_granted = Column(Integer, nullable=False)

    valid_from = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    valid_to = Column(DateTime(timezone=True), nullable=True)

    is_active = Column(Boolean, nullable=False, server_default=text("true"))

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    account = relationship("Account", back_populates="entitlement_grants")


class EntitlementConsumption(Base):
    __tablename__ = "entitlement_consumption"
    __table_args__ = {"schema": "billing"}

    id = Column(BigInteger, primary_key=True)

    account_id = Column(
        BigInteger,
        ForeignKey("billing.account.id", ondelete="CASCADE"),
        nullable=False,
    )

    task_id = Column(String, nullable=False)      # matches DB unique(task_id)
    consumed_matches = Column(Integer, nullable=False, server_default=text("1"))

    consumed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    source = Column(String, nullable=False, server_default=text("'sportai'"))

    account = relationship("Account", back_populates="entitlement_consumptions")
