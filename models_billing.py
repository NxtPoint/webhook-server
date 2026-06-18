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
    # comp = sponsored/free account: bypasses the credit gate (free, unlimited uploads + AI
    # coach). Usage is still recorded so analytics work; revenue naturally shows $0. See the
    # entitlements gate (entitlements_api) + coach gate (tennis_coach).
    comp = Column(Boolean, nullable=False, server_default=text("false"))
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


# ---------------------------------------------------------------------------
# Schema bootstrap — billing_init()
# ---------------------------------------------------------------------------
# The ORM models above (account, member, entitlement_grant, entitlement_consumption)
# are created via Base.metadata.create_all. The remaining billing.* objects
# (subscription_state, subscription_event_log, monthly_refill_log, coaches_permission,
# entitlements, security_access, vw_customer_usage) were historically created
# out-of-band in prod and had NO create-DDL in code — a fresh DB could not reproduce
# them. The raw DDL below mirrors the live schema EXACTLY (introspected 2026-06-18) so
# billing is reproducible. All idempotent (IF NOT EXISTS / existence-guarded view), so
# on the live DB billing_init() is a pure no-op. Column *additions* still live in their
# existing _ensure_* owners (billing_service._ensure_technique_columns,
# subscriptions_api._ensure_subscription_state_columns, entitlements_api._ensure_entitlements_schema);
# billing_init only guarantees the base objects exist.

_BILLING_RAW_DDL = [
    # subscription_state — one row per account (current subscription); written by subscriptions_api
    """
    CREATE TABLE IF NOT EXISTS billing.subscription_state (
        account_id               bigint PRIMARY KEY REFERENCES billing.account(id),
        plan_id                  text,
        plan_code                text,
        plan_type                text CHECK (plan_type = ANY (ARRAY['recurring'::text, 'payg'::text])),
        status                   text NOT NULL DEFAULT 'NONE'
                                 CHECK (status = ANY (ARRAY['NONE'::text,'ACTIVE'::text,'PAST_DUE'::text,'CANCELLED'::text,'EXPIRED'::text])),
        current_period_start     timestamptz,
        current_period_end       timestamptz,
        cancelled_at             timestamptz,
        payment_cancelled_at     timestamptz,
        updated_at               timestamptz NOT NULL DEFAULT now(),
        matches_granted          integer,
        billing_provider         text NOT NULL DEFAULT 'wix',
        provider_subscription_id text
    )
    """,
    # subscription_event_log — idempotent webhook audit (event_id = sha256)
    """
    CREATE TABLE IF NOT EXISTS billing.subscription_event_log (
        event_id   text PRIMARY KEY,
        account_id bigint NOT NULL REFERENCES billing.account(id),
        event_type text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        payload    jsonb NOT NULL
    )
    """,
    # monthly_refill_log — Wix-only monthly refill claim
    """
    CREATE TABLE IF NOT EXISTS billing.monthly_refill_log (
        account_id bigint NOT NULL REFERENCES billing.account(id),
        year_month text NOT NULL,
        grant_id   bigint,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (account_id, year_month)
    )
    """,
    # coaches_permission — coach<->owner links (table; coach_invite/db.py owns the token index)
    """
    CREATE TABLE IF NOT EXISTS billing.coaches_permission (
        id               bigserial PRIMARY KEY,
        owner_account_id bigint NOT NULL REFERENCES billing.account(id) ON DELETE CASCADE,
        coach_account_id bigint REFERENCES billing.account(id) ON DELETE CASCADE,
        coach_email      text NOT NULL,
        status           text NOT NULL DEFAULT 'INVITED',
        active           boolean NOT NULL DEFAULT true,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        coach_full_name  text,
        invite_token     text
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_coaches_permission_owner_email "
    "ON billing.coaches_permission (owner_account_id, coach_email)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_coaches_permission_token "
    "ON billing.coaches_permission (invite_token) WHERE invite_token IS NOT NULL",
    # entitlements — cached upload-gate state (entitlements_api UPSERTs into it)
    """
    CREATE TABLE IF NOT EXISTS billing.entitlements (
        account_id                 bigint PRIMARY KEY,
        email                      text NOT NULL,
        role                       text NOT NULL,
        account_active             boolean NOT NULL,
        subscription_status        text,
        current_period_end         timestamptz,
        paid_active                boolean NOT NULL DEFAULT false,
        matches_granted            integer NOT NULL DEFAULT 0,
        matches_consumed           integer NOT NULL DEFAULT 0,
        matches_remaining          integer NOT NULL DEFAULT 0,
        can_upload                 boolean NOT NULL DEFAULT false,
        block_reason               text,
        updated_at                 timestamptz NOT NULL DEFAULT now(),
        can_view_dashboards        boolean,
        dashboard_block_reason     text,
        techniques_granted         integer NOT NULL DEFAULT 0,
        techniques_consumed        integer NOT NULL DEFAULT 0,
        techniques_remaining       integer NOT NULL DEFAULT 0,
        coach_linked_players       integer NOT NULL DEFAULT 0,
        can_link_additional_player boolean NOT NULL DEFAULT true
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_entitlements_email ON billing.entitlements (email)",
    # security_access — viewer->customer access map (legacy; viewer_email/customer_email)
    """
    CREATE TABLE IF NOT EXISTS billing.security_access (
        viewer_email   text,
        customer_email text
    )
    """,
    # payment — record of every money movement (PayPal sale/capture/refund/reversal).
    # RECORD-ONLY: money was previously stored nowhere (the webhook re-fetched the
    # authoritative amount and discarded it). Refunds are recorded with a negative
    # amount but DO NOT revoke credits (business decision, 2026-06-18). Idempotent on
    # (provider, provider_payment_id) so webhook retries never double-record.
    """
    CREATE TABLE IF NOT EXISTS billing.payment (
        id                       bigserial PRIMARY KEY,
        provider                 text NOT NULL DEFAULT 'paypal',
        kind                     text NOT NULL,   -- subscription_payment|payg_capture|refund|reversal
        provider_payment_id      text,            -- sale/capture/refund id (unique money movement)
        provider_subscription_id text,
        order_id                 text,
        account_id               bigint REFERENCES billing.account(id),
        buyer_email              text,
        plan_code                text,
        amount_cents             integer,         -- positive=in, negative=refund/reversal
        currency                 text,
        status                   text,
        event_type               text,            -- the PayPal webhook event_type
        occurred_at              timestamptz,
        raw                      jsonb,
        created_at               timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_provider_id "
    "ON billing.payment (provider, provider_payment_id) WHERE provider_payment_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_payment_account ON billing.payment (account_id, occurred_at)",
    "CREATE INDEX IF NOT EXISTS ix_payment_email ON billing.payment (buyer_email, occurred_at)",
]

# vw_customer_usage — matches_remaining = grants - consumption. Created only if missing
# (never CREATE OR REPLACE over the live view, to avoid clobbering it with a hand-copy).
_BILLING_VW_CUSTOMER_USAGE = """
CREATE VIEW billing.vw_customer_usage AS
WITH grants AS (
    SELECT eg.account_id,
           COALESCE(sum(eg.matches_granted), 0::bigint) AS matches_granted
    FROM billing.entitlement_grant eg
    WHERE eg.is_active = true AND (eg.valid_to IS NULL OR eg.valid_to >= now())
    GROUP BY eg.account_id
), cons AS (
    SELECT ec.account_id,
           COALESCE(sum(ec.consumed_matches), 0::bigint) AS matches_consumed_total,
           COALESCE(sum(ec.consumed_matches) FILTER (WHERE ec.source = 'sportai'::text), 0::bigint) AS matches_consumed_usage,
           COALESCE(sum(ec.consumed_matches) FILTER (WHERE ec.source = 'monthly_expire'::text), 0::bigint) AS matches_consumed_expired,
           max(ec.consumed_at) AS last_consumed_at
    FROM billing.entitlement_consumption ec
    GROUP BY ec.account_id
)
SELECT a.id AS account_id,
       a.email AS customer_email,
       a.primary_full_name AS customer_name,
       a.currency_code,
       COALESCE(g.matches_granted, 0::bigint) AS matches_granted,
       COALESCE(c.matches_consumed_usage, 0::bigint) AS matches_consumed,
       COALESCE(g.matches_granted, 0::bigint) - COALESCE(c.matches_consumed_total, 0::bigint) AS matches_remaining,
       c.last_consumed_at AS last_processed_at,
       COALESCE(c.matches_consumed_total, 0::bigint) AS matches_consumed_total,
       COALESCE(c.matches_consumed_expired, 0::bigint) AS matches_consumed_expired
FROM billing.account a
LEFT JOIN grants g ON g.account_id = a.id
LEFT JOIN cons c ON c.account_id = a.id
"""


def billing_init(engine=None):
    """Idempotently create the entire billing.* schema (reproducible from code).

    No-op on the live DB (every statement is IF NOT EXISTS / existence-guarded). Column
    additions remain owned by the existing _ensure_* functions. Safe to call on boot."""
    if engine is None:
        from db_init import engine as _eng
        engine = _eng
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS billing"))
        Base.metadata.create_all(conn, checkfirst=True)
        # comp flag (sponsored accounts) — additive on existing prod tables.
        conn.execute(text(
            "ALTER TABLE billing.account ADD COLUMN IF NOT EXISTS comp boolean NOT NULL DEFAULT false"))
        for stmt in _BILLING_RAW_DDL:
            conn.execute(text(stmt))
        exists = conn.execute(text(
            "SELECT 1 FROM information_schema.views WHERE table_schema='billing' "
            "AND table_name='vw_customer_usage'"
        )).first()
        if not exists:
            conn.execute(text(_BILLING_VW_CUSTOMER_USAGE))
    return engine
