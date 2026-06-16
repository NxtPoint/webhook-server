# core_db/schema.py — idempotent bootstrap for the canonical `core.*` schema.
#
# core_init(engine) is THE migration. It is additive and safe to run repeatedly,
# including against prod (it never touches billing.*/bronze.*/etc). It:
#   1. CREATE SCHEMA IF NOT EXISTS core
#   2. Base.metadata.create_all(checkfirst=True)  — all core.* tables
#   3. supplementary DDL ORM create_all can't express:
#        - functional UNIQUE indexes on lower(email)  (case-insensitive, no citext dep)
#        - partial UNIQUE indexes (one active subscription per account; one account owner)
#        - credit-ledger idempotency indexes
#        - hot-path secondary indexes (usage_event, match)
#        - derived views: vw_account_credits, vw_subscription_current, vw_mrr
#
# No migration framework (repo convention) — schema is idempotent DDL, like
# db_init.bronze_init() / gold_init(). Requires Postgres 13+ (gen_random_uuid()).

from sqlalchemy import text

from core_db.models import Base, SCHEMA

# Supplementary DDL — each statement idempotent. Run after create_all.
_SUPPLEMENTAL = [
    # Case-insensitive unique email (replaces citext dependency)
    f'CREATE UNIQUE INDEX IF NOT EXISTS uq_account_email_lower ON {SCHEMA}.account (lower(email)) WHERE deleted_at IS NULL',
    f'CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_email_lower ON {SCHEMA}.app_user (lower(email)) WHERE deleted_at IS NULL',
    f'CREATE UNIQUE INDEX IF NOT EXISTS uq_account_public_id ON {SCHEMA}.account (public_id)',
    f'CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_public_id ON {SCHEMA}.app_user (public_id)',
    f'CREATE UNIQUE INDEX IF NOT EXISTS uq_person_public_id ON {SCHEMA}.person (public_id)',
    f'CREATE UNIQUE INDEX IF NOT EXISTS uq_match_public_id ON {SCHEMA}.match (public_id)',

    # One account owner; one active subscription per account
    f'CREATE UNIQUE INDEX IF NOT EXISTS uq_one_owner_per_account ON {SCHEMA}.app_user (account_id) WHERE is_account_owner',
    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_sub_per_account ON {SCHEMA}.subscription (account_id) WHERE status = 'active'",

    # Credit-ledger idempotency:
    #   - grants unique on (account, source, plan_code, external_wix_id)  [mirrors today]
    #   - a match is consumed exactly once
    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_grant_idem ON {SCHEMA}.credit_ledger (account_id, source, plan_code, external_wix_id) WHERE entry_type = 'grant' AND external_wix_id IS NOT NULL",
    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_consume_once ON {SCHEMA}.credit_ledger (ref_type, ref_id) WHERE entry_type = 'consume' AND ref_id IS NOT NULL",

    # Hot-path secondary indexes
    f'CREATE INDEX IF NOT EXISTS ix_person_account ON {SCHEMA}.person (account_id)',
    f'CREATE INDEX IF NOT EXISTS ix_app_user_account ON {SCHEMA}.app_user (account_id)',
    f'CREATE INDEX IF NOT EXISTS ix_relationship_to ON {SCHEMA}.relationship (to_person_id)',
    f'CREATE INDEX IF NOT EXISTS ix_relationship_from ON {SCHEMA}.relationship (from_person_id)',
    f'CREATE INDEX IF NOT EXISTS ix_subscription_account ON {SCHEMA}.subscription (account_id)',
    f'CREATE INDEX IF NOT EXISTS ix_credit_ledger_account ON {SCHEMA}.credit_ledger (account_id)',
    f'CREATE INDEX IF NOT EXISTS ix_match_account ON {SCHEMA}.match (account_id)',
    f'CREATE INDEX IF NOT EXISTS ix_match_status ON {SCHEMA}.match (status)',
    f'CREATE INDEX IF NOT EXISTS ix_usage_event_account_time ON {SCHEMA}.usage_event (account_id, occurred_at)',
    f'CREATE INDEX IF NOT EXISTS ix_usage_event_type_time ON {SCHEMA}.usage_event (event_type, occurred_at)',
    f'CREATE INDEX IF NOT EXISTS ix_consent_subject ON {SCHEMA}.consent (subject_person_id, consent_type)',
]

# Derived views (presentation/aggregation — keeps "SQL owns aggregation").
_VIEWS = [
    # Credit balance per account (matches/techniques remaining = sum of ledger deltas)
    f"""
    CREATE OR REPLACE VIEW {SCHEMA}.vw_account_credits AS
    SELECT
        a.id                                            AS account_id,
        a.public_id                                     AS account_public_id,
        COALESCE(SUM(cl.matches_delta), 0)              AS matches_remaining,
        COALESCE(SUM(cl.techniques_delta), 0)           AS techniques_remaining,
        COALESCE(SUM(cl.matches_delta) FILTER (WHERE cl.matches_delta > 0), 0)    AS matches_granted_total,
        COALESCE(-SUM(cl.matches_delta) FILTER (WHERE cl.matches_delta < 0), 0)   AS matches_consumed_total
    FROM {SCHEMA}.account a
    LEFT JOIN {SCHEMA}.credit_ledger cl ON cl.account_id = a.id
    WHERE a.deleted_at IS NULL
    GROUP BY a.id, a.public_id
    """,
    # Current subscription per account (active row if any)
    f"""
    CREATE OR REPLACE VIEW {SCHEMA}.vw_subscription_current AS
    SELECT DISTINCT ON (s.account_id)
        s.account_id, s.id AS subscription_id, s.plan_code, s.plan_type,
        s.status, s.mrr_cents, s.billing_provider,
        s.current_period_start, s.current_period_end, s.matches_per_period
    FROM {SCHEMA}.subscription s
    ORDER BY s.account_id,
             (s.status = 'active') DESC,
             s.current_period_end DESC NULLS LAST,
             s.id DESC
    """,
    # Total MRR across active recurring subscriptions
    f"""
    CREATE OR REPLACE VIEW {SCHEMA}.vw_mrr AS
    SELECT
        COUNT(*)                         AS active_subscriptions,
        COALESCE(SUM(mrr_cents), 0)      AS mrr_cents_total,
        COALESCE(SUM(mrr_cents), 0) / 100.0 AS mrr_total
    FROM {SCHEMA}.subscription
    WHERE status = 'active' AND plan_type = 'recurring'
    """,
]


def core_init(engine=None):
    """Create / update the core.* schema idempotently. Returns the engine used.

    If `engine` is None, uses db_init.engine (the shared app engine)."""
    if engine is None:
        from db_init import engine as shared_engine
        engine = shared_engine

    # Schema first (create_all does not create the schema itself for non-default schemas)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))

    # Tables (checkfirst=True → idempotent)
    Base.metadata.create_all(engine, checkfirst=True)

    # Supplementary indexes + views, each in the same transaction
    with engine.begin() as conn:
        for stmt in _SUPPLEMENTAL:
            conn.execute(text(stmt))
        for stmt in _VIEWS:
            conn.execute(text(stmt))

    return engine


if __name__ == "__main__":
    # Manual run: `.venv/Scripts/python -m core_db.schema`
    # Uses DATABASE_URL from the environment via db_init.engine. Additive + safe.
    eng = core_init()
    print(f"core.* schema initialised on {eng.url.render_as_string(hide_password=True)}")
