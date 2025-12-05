from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Numeric,
    Boolean,
    DateTime,
    Date,
    CHAR,
    ForeignKey,
    text,
)
from sqlalchemy.orm import relationship, declarative_base

# Local Base for billing models
Base = declarative_base()


class PricingComponent(Base):
    __tablename__ = "pricing_component"
    __table_args__ = {"schema": "billing"}

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=False)

    billing_metric = Column(String, nullable=False)
    unit = Column(String, nullable=False)

    currency_code = Column(CHAR(3), nullable=False, server_default=text("'USD'"))
    unit_amount = Column(Numeric(10, 4), nullable=False)

    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class Account(Base):
    __tablename__ = "account"
    __table_args__ = {"schema": "billing"}

    id = Column(BigInteger, primary_key=True)
    external_wix_id = Column(String, nullable=True)
    email = Column(String, nullable=False)
    primary_full_name = Column(String, nullable=False)

    currency_code = Column(CHAR(3), nullable=False, server_default=text("'USD'"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    members = relationship("Member", back_populates="account", cascade="all, delete-orphan")
    invoices = relationship("Invoice", back_populates="account", cascade="all, delete-orphan")


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
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    account = relationship("Account", back_populates="members")


class UsageVideo(Base):
    __tablename__ = "usage_video"
    __table_args__ = {"schema": "billing"}

    id = Column(BigInteger, primary_key=True)
    account_id = Column(
        BigInteger,
        ForeignKey("billing.account.id", ondelete="CASCADE"),
        nullable=False,
    )
    member_id = Column(
        BigInteger,
        ForeignKey("billing.member.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_id = Column(String, nullable=False)

    video_minutes = Column(Numeric(10, 2), nullable=False)
    billable_minutes = Column(Numeric(10, 2), nullable=False)

    source = Column(String, nullable=False, server_default=text("'sportai'"))
    processed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class Invoice(Base):
    __tablename__ = "invoice"
    __table_args__ = {"schema": "billing"}

    id = Column(BigInteger, primary_key=True)
    account_id = Column(
        BigInteger,
        ForeignKey("billing.account.id", ondelete="CASCADE"),
        nullable=False,
    )

    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)

    currency_code = Column(CHAR(3), nullable=False)
    total_amount = Column(Numeric(12, 4), nullable=False, server_default=text("0"))

    status = Column(String, nullable=False, server_default=text("'draft'"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    account = relationship("Account", back_populates="invoices")
    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan")


class InvoiceLine(Base):
    __tablename__ = "invoice_line"
    __table_args__ = {"schema": "billing"}

    id = Column(BigInteger, primary_key=True)
    invoice_id = Column(
        BigInteger,
        ForeignKey("billing.invoice.id", ondelete="CASCADE"),
        nullable=False,
    )

    pricing_component_code = Column(String, nullable=False)
    description = Column(String, nullable=False)

    quantity = Column(Numeric(12, 4), nullable=False)
    unit_amount = Column(Numeric(10, 4), nullable=False)
    line_amount = Column(Numeric(12, 4), nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    invoice = relationship("Invoice", back_populates="lines")
