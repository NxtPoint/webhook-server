# core_db — canonical "single source of truth" data layer.
#
# Schema:        core_db/models.py     (SQLAlchemy ORM — the authoritative schema definition)
# Migration:     core_db/schema.py     (core_init(): idempotent bootstrap — create_all + supplementary DDL/views)
# Data access:   core_db/repositories/ (clean functions; callers never write raw SQL)
# Seed/test:     core_db/seed.py       (synthetic data; prod-guarded)
#
# Design decisions (approved 2026-06-16): new `core.*` schema · append-only credit ledger ·
# account+user+person identity split · bigint PKs + public_id uuid. See DB-SCHEMA-PROPOSAL.md.
#
# NOTE: this layer is ADDITIVE and currently DARK — it is not wired into any prod boot path,
# and no live data has been migrated. See DB-SCHEMA-PROPOSAL.md §7 for the migration plan.

from core_db.models import (  # noqa: F401
    Base,
    SCHEMA,
    Account,
    AppUser,
    Acquisition,
    Person,
    Relationship,
    Plan,
    Subscription,
    SubscriptionEvent,
    CreditLedger,
    Match,
    UsageEvent,
    Consent,
    DataSubjectRequest,
    RetentionRule,
    NpsResponse,
    SurveyResponse,
    Ticket,
    TicketMessage,
)
from core_db.schema import core_init  # noqa: F401

__all__ = ["Base", "SCHEMA", "core_init"]
