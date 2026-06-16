# core_db/models.py — SQLAlchemy ORM models for the canonical `core.*` schema.
#
# This file is the AUTHORITATIVE schema definition. core_db/schema.py::core_init()
# materialises it (create_all) plus supplementary DDL that ORM create_all can't express
# (partial-unique indexes, case-insensitive email indexes, derived views).
#
# Conventions:
#   - bigint identity PK `id` (internal) + `public_id uuid` (external/API/sync) on
#     externally-exposed entities. uuid default = gen_random_uuid() (Postgres 13+ built-in).
#   - email stored lowercased by the DAL; uniqueness enforced by functional UNIQUE index
#     on lower(email) in schema.py (avoids the citext extension dependency).
#   - created_at server-default now(); updated_at maintained by the DAL on write.
#   - soft-delete via deleted_at; retention via retention_until / anonymized_at where PII lives.
#
# Table `user` is reserved in Postgres, so the physical table is `core.app_user`
# (ORM class AppUser). Everything else maps 1:1 to the proposal.

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()
SCHEMA = "core"

_UUID_DEFAULT = text("gen_random_uuid()")
_NOW = text("now()")


def _ts(**kw):
    return Column(DateTime(timezone=True), **kw)


# ---------------------------------------------------------------------------
# Identity & ownership
# ---------------------------------------------------------------------------

class Account(Base):
    """Billing / ownership container — one per paying customer. Supersedes billing.account."""
    __tablename__ = "account"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    public_id = Column(UUID(as_uuid=True), nullable=False, server_default=_UUID_DEFAULT)
    email = Column(Text, nullable=False)               # owner/billing contact (lowercased by DAL)
    display_name = Column(Text, nullable=True)
    currency_code = Column(CHAR(3), nullable=False, server_default=text("'USD'"))
    status = Column(Text, nullable=False, server_default=text("'active'"))  # active|suspended|closed
    external_wix_id = Column(Text, nullable=True)      # sync/reconcile key

    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)
    deleted_at = _ts(nullable=True)

    users = relationship("AppUser", back_populates="account", cascade="all, delete-orphan")
    persons = relationship("Person", back_populates="account", cascade="all, delete-orphan")


class AppUser(Base):
    """Authenticatable login identity (NEW). Physical table core.app_user."""
    __tablename__ = "app_user"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    public_id = Column(UUID(as_uuid=True), nullable=False, server_default=_UUID_DEFAULT)
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="CASCADE"), nullable=False)

    email = Column(Text, nullable=False)               # lowercased by DAL
    auth_provider = Column(Text, nullable=False, server_default=text("'wix'"))  # wix|password|google|...
    auth_provider_uid = Column(Text, nullable=True)    # e.g. wixMemberId (today's external_wix_id)
    email_verified = Column(Boolean, nullable=False, server_default=text("false"))
    is_account_owner = Column(Boolean, nullable=False, server_default=text("false"))
    marketing_opt_in = Column(Boolean, nullable=False, server_default=text("false"))
    status = Column(Text, nullable=False, server_default=text("'active'"))  # active|disabled

    last_login_at = _ts(nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)
    deleted_at = _ts(nullable=True)

    account = relationship("Account", back_populates="users")
    acquisition = relationship("Acquisition", back_populates="user", uselist=False,
                               cascade="all, delete-orphan")


class Acquisition(Base):
    """Signup attribution (UTM / source), 1:1 with a user (NEW)."""
    __tablename__ = "acquisition"
    __table_args__ = {"schema": SCHEMA}

    user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="CASCADE"),
                     primary_key=True)
    source = Column(Text, nullable=True)
    medium = Column(Text, nullable=True)
    campaign = Column(Text, nullable=True)
    term = Column(Text, nullable=True)
    content = Column(Text, nullable=True)
    referrer = Column(Text, nullable=True)
    landing_page = Column(Text, nullable=True)
    gclid = Column(Text, nullable=True)
    fbclid = Column(Text, nullable=True)
    first_seen_at = _ts(nullable=True)
    signed_up_at = _ts(nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)

    user = relationship("AppUser", back_populates="acquisition")


class Person(Base):
    """Tennis profile (player/parent/coach). Supersedes billing.member.
    A minor (junior) is a player with is_minor=true (derived from dob)."""
    __tablename__ = "person"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    public_id = Column(UUID(as_uuid=True), nullable=False, server_default=_UUID_DEFAULT)
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="SET NULL"), nullable=True)

    role = Column(Text, nullable=False, server_default=text("'player'"))  # player|parent|coach
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    full_name = Column(Text, nullable=False)
    surname = Column(Text, nullable=True)

    dob = Column(Date, nullable=True)
    # is_minor is derived (age isn't immutable so it can't be a generated column);
    # the DAL/views compute it. Stored flag kept for fast filtering, refreshed on write.
    is_minor = Column(Boolean, nullable=True)

    utr = Column(Text, nullable=True)
    dominant_hand = Column(Text, nullable=True)
    country = Column(Text, nullable=True)
    area = Column(Text, nullable=True)
    phone = Column(Text, nullable=True)
    skill_level = Column(Text, nullable=True)
    club_school = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    profile_photo_s3_key = Column(Text, nullable=True)
    profile_photo_url = Column(Text, nullable=True)

    status = Column(Text, nullable=False, server_default=text("'active'"))
    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)
    deleted_at = _ts(nullable=True)
    # retention for profile PII (esp. minors)
    retention_until = _ts(nullable=True)
    anonymized_at = _ts(nullable=True)

    account = relationship("Account", back_populates="persons")


class Relationship(Base):
    """coach↔player, parent↔junior. Supersedes billing.coaches_permission (person-level now)."""
    __tablename__ = "relationship"
    __table_args__ = (
        UniqueConstraint("from_person_id", "to_person_id", "type",
                         name="uq_relationship_from_to_type"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True)
    from_person_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.person.id", ondelete="CASCADE"), nullable=False)
    to_person_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.person.id", ondelete="CASCADE"), nullable=False)
    type = Column(Text, nullable=False)               # coach_player | parent_junior
    status = Column(Text, nullable=False, server_default=text("'pending'"))  # pending|active|revoked
    invite_token = Column(Text, nullable=True)
    invited_email = Column(Text, nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)
    revoked_at = _ts(nullable=True)


# ---------------------------------------------------------------------------
# Subscriptions & billing
# ---------------------------------------------------------------------------

class Plan(Base):
    """Plan catalogue (NEW) — pulls the hardcoded frontend/pricing.html UUIDs into the DB."""
    __tablename__ = "plan"
    __table_args__ = (UniqueConstraint("code", name="uq_plan_code"), {"schema": SCHEMA})

    id = Column(BigInteger, primary_key=True)
    code = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    plan_type = Column(Text, nullable=False)          # recurring | payg
    price_cents = Column(Integer, nullable=False, server_default=text("0"))
    currency = Column(CHAR(3), nullable=False, server_default=text("'USD'"))
    billing_interval = Column(Text, nullable=True)    # month | year | once
    matches_included = Column(Integer, nullable=False, server_default=text("0"))
    techniques_included = Column(Integer, nullable=False, server_default=text("0"))
    external_wix_plan_id = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)


class Subscription(Base):
    """Current + historical subscriptions. Supersedes billing.subscription_state."""
    __tablename__ = "subscription"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.plan.id", ondelete="SET NULL"), nullable=True)
    external_plan_id = Column(Text, nullable=True)    # Wix plan UUID
    plan_code = Column(Text, nullable=True)
    plan_type = Column(Text, nullable=True)           # recurring | payg
    status = Column(Text, nullable=False, server_default=text("'active'"))  # active|cancelled|expired|past_due
    billing_provider = Column(Text, nullable=False, server_default=text("'wix_paypal'"))
    mrr_cents = Column(Integer, nullable=False, server_default=text("0"))
    matches_per_period = Column(Integer, nullable=True)

    current_period_start = _ts(nullable=True)
    current_period_end = _ts(nullable=True)
    started_at = _ts(nullable=True)
    cancelled_at = _ts(nullable=True)
    payment_cancelled_at = _ts(nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)


class SubscriptionEvent(Base):
    """Webhook audit log (idempotent by event_id). Supersedes billing.subscription_event_log."""
    __tablename__ = "subscription_event"
    __table_args__ = (UniqueConstraint("event_id", name="uq_subscription_event_id"), {"schema": SCHEMA})

    id = Column(BigInteger, primary_key=True)
    event_id = Column(Text, nullable=False)           # sha256 of canonical fields
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="CASCADE"), nullable=True)
    subscription_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.subscription.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(Text, nullable=False)
    provider = Column(Text, nullable=False, server_default=text("'wix'"))
    payload = Column(JSONB, nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)


class CreditLedger(Base):
    """Append-only credit ledger. Balance = SUM(matches_delta). Supersedes
    billing.entitlement_grant + entitlement_consumption."""
    __tablename__ = "credit_ledger"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="CASCADE"), nullable=False)
    entry_type = Column(Text, nullable=False)         # grant|consume|expire|adjustment|refund
    matches_delta = Column(Integer, nullable=False, server_default=text("0"))
    techniques_delta = Column(Integer, nullable=False, server_default=text("0"))
    source = Column(Text, nullable=False)             # subscription|payg_purchase|signup_bonus|manual|match_upload
    ref_type = Column(Text, nullable=True)            # match|order|plan|manual
    ref_id = Column(Text, nullable=True)
    plan_code = Column(Text, nullable=True)
    external_wix_id = Column(Text, nullable=True)     # grant idempotency
    valid_from = _ts(nullable=True)
    valid_to = _ts(nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)


# ---------------------------------------------------------------------------
# Matches / uploads (business record; links to bronze/silver/gold by task_id)
# ---------------------------------------------------------------------------

class Match(Base):
    __tablename__ = "match"
    __table_args__ = (UniqueConstraint("task_id", name="uq_match_task_id"), {"schema": SCHEMA})

    id = Column(BigInteger, primary_key=True)
    public_id = Column(UUID(as_uuid=True), nullable=False, server_default=_UUID_DEFAULT)
    task_id = Column(Text, nullable=False)            # bridge to bronze.submission_context
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="CASCADE"), nullable=False)
    uploaded_by_user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="SET NULL"), nullable=True)
    subject_person_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.person.id", ondelete="SET NULL"), nullable=True)

    sport_type = Column(Text, nullable=True)
    pipeline = Column(Text, nullable=True)            # sportai | t5 | technique
    status = Column(Text, nullable=False, server_default=text("'uploaded'"))  # uploaded|queued|processing|complete|failed|deleted

    uploaded_at = _ts(nullable=True)
    processing_started_at = _ts(nullable=True)
    processed_at = _ts(nullable=True)

    match_date = Column(Date, nullable=True)
    location = Column(Text, nullable=True)
    player_a_name = Column(Text, nullable=True)
    player_b_name = Column(Text, nullable=True)
    video_s3_key = Column(Text, nullable=True)
    trim_s3_key = Column(Text, nullable=True)
    kpi_summary = Column(JSONB, nullable=True)        # cached headline KPIs from gold.match_kpi

    error = Column(Text, nullable=True)
    retention_until = _ts(nullable=True)
    deleted_at = _ts(nullable=True)
    anonymized_at = _ts(nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)


# ---------------------------------------------------------------------------
# Usage events
# ---------------------------------------------------------------------------

class UsageEvent(Base):
    __tablename__ = "usage_event"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="SET NULL"), nullable=True)
    person_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.person.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(Text, nullable=False)         # match_upload|technique_upload|report_view|ai_coach_query|dashboard_view|login|...
    ref_type = Column(Text, nullable=True)
    ref_id = Column(Text, nullable=True)
    event_metadata = Column("metadata", JSONB, nullable=True)  # physical column name `metadata`
    occurred_at = _ts(nullable=False, server_default=_NOW)
    created_at = _ts(nullable=False, server_default=_NOW)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

class NpsResponse(Base):
    __tablename__ = "nps_response"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="SET NULL"), nullable=True)
    score = Column(Integer, nullable=False)           # 0-10
    bucket = Column(Text, nullable=True)              # detractor|passive|promoter (derived by DAL)
    comment = Column(Text, nullable=True)
    survey_id = Column(Text, nullable=True)
    submitted_at = _ts(nullable=False, server_default=_NOW)


class SurveyResponse(Base):
    __tablename__ = "survey_response"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="SET NULL"), nullable=True)
    survey_key = Column(Text, nullable=False)
    responses = Column(JSONB, nullable=True)
    submitted_at = _ts(nullable=False, server_default=_NOW)


class Ticket(Base):
    __tablename__ = "ticket"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    account_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.account.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="SET NULL"), nullable=True)
    subject = Column(Text, nullable=True)
    body = Column(Text, nullable=True)
    status = Column(Text, nullable=False, server_default=text("'open'"))  # open|pending|resolved|closed
    channel = Column(Text, nullable=True)             # support_bot|email|portal
    priority = Column(Text, nullable=True)
    assigned_to = Column(Text, nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)
    resolved_at = _ts(nullable=True)

    messages = relationship("TicketMessage", back_populates="ticket", cascade="all, delete-orphan")


class TicketMessage(Base):
    __tablename__ = "ticket_message"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    ticket_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.ticket.id", ondelete="CASCADE"), nullable=False)
    author = Column(Text, nullable=False)             # customer|agent|bot
    body = Column(Text, nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)

    ticket = relationship("Ticket", back_populates="messages")


# ---------------------------------------------------------------------------
# Consent, privacy & retention (compliance core)
# ---------------------------------------------------------------------------

class Consent(Base):
    """Versioned, per-type consent. For a minor, subject=the junior, granted_by=the parent's user."""
    __tablename__ = "consent"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    subject_person_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.person.id", ondelete="CASCADE"), nullable=False)
    granted_by_user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="SET NULL"), nullable=True)
    consent_type = Column(Text, nullable=False)
    # terms_of_service|privacy_policy|marketing_email|biometric_processing|minor_processing_parental
    policy_version = Column(Text, nullable=True)
    status = Column(Text, nullable=False, server_default=text("'granted'"))  # granted|withdrawn
    granted_at = _ts(nullable=True)
    withdrawn_at = _ts(nullable=True)
    source = Column(Text, nullable=True)              # signup|portal|import
    evidence = Column(JSONB, nullable=True)           # ip, user-agent, exact checkbox text
    created_at = _ts(nullable=False, server_default=_NOW)


class DataSubjectRequest(Base):
    """GDPR rights handling (access/erasure/rectification/portability)."""
    __tablename__ = "data_subject_request"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)
    subject_person_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.person.id", ondelete="SET NULL"), nullable=True)
    requested_by_user_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.app_user.id", ondelete="SET NULL"), nullable=True)
    request_type = Column(Text, nullable=False)       # access|erasure|rectification|portability
    status = Column(Text, nullable=False, server_default=text("'received'"))  # received|in_progress|completed|rejected
    requested_at = _ts(nullable=False, server_default=_NOW)
    completed_at = _ts(nullable=True)
    notes = Column(Text, nullable=True)


class RetentionRule(Base):
    """Configurable retention windows; a sweep job applies them (sets retention_until/anonymized_at)."""
    __tablename__ = "retention_rule"
    __table_args__ = (UniqueConstraint("data_class", "applies_after", name="uq_retention_class_after"),
                      {"schema": SCHEMA})

    id = Column(BigInteger, primary_key=True)
    data_class = Column(Text, nullable=False)         # match_video|biometrics|match_analysis|account_pii|marketing
    retention_days = Column(Integer, nullable=False)
    applies_after = Column(Text, nullable=False)      # account_closure|upload|consent_withdrawal
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    notes = Column(Text, nullable=True)
    created_at = _ts(nullable=False, server_default=_NOW)
    updated_at = _ts(nullable=False, server_default=_NOW)
