#=======================================================================
# models_billing.py
#=======================================================================

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
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
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    role = Column(String, nullable=False, server_default=text("'player_parent'"))
    active = Column(Boolean, nullable=False, server_default=text("true"))

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
