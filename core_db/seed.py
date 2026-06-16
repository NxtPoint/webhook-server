# core_db/seed.py — synthetic seed / test data for the core.* schema.
#
# SAFETY: refuses to run unless --force is given, AND refuses a non-local DATABASE_URL
# unless --allow-remote is also given (so it can't accidentally seed prod). All rows use
# the @seed.ten-fifty5.test domain so they're trivially identifiable and purgeable.
#
# Usage:
#   .venv/Scripts/python -m core_db.seed --force                 # seed local DB
#   .venv/Scripts/python -m core_db.seed --force --reset         # purge + reseed
#   .venv/Scripts/python -m core_db.seed --force --purge         # remove seed data only
#
# Builds one realistic account tree:
#   owner (parent) + 2 juniors (minors) + 1 coach, a recurring subscription, PAYG credits
#   (one consumed), 2 matches with KPI summaries, usage events, an NPS + a support ticket,
#   and consent records including minor parental + biometric processing.

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from core_db.db import get_engine, session_scope
from core_db.schema import core_init
from core_db.repositories import accounts, subscriptions, matches, feedback, consent

SEED_DOMAIN = "seed.ten-fifty5.test"
OWNER_EMAIL = f"owner@{SEED_DOMAIN}"
COACH_EMAIL = f"coach@{SEED_DOMAIN}"


def _now():
    return datetime.now(timezone.utc)


# ── Plan catalogue (representative — reconcile values with frontend/pricing.html) ──
SEED_PLANS = [
    dict(code="player starter", name="Player Starter", plan_type="recurring",
         price_cents=2500, billing_interval="month", matches_included=3),
    dict(code="player standard", name="Player Standard", plan_type="recurring",
         price_cents=4500, billing_interval="month", matches_included=6),
    dict(code="player advanced", name="Player Advanced", plan_type="recurring",
         price_cents=7000, billing_interval="month", matches_included=12),
    dict(code="topup 3", name="Top-up 3 matches", plan_type="payg",
         price_cents=3000, billing_interval="once", matches_included=3),
]

SEED_RETENTION = [
    dict(data_class="match_video", retention_days=365, applies_after="upload"),
    dict(data_class="biometrics", retention_days=365, applies_after="consent_withdrawal"),
    dict(data_class="account_pii", retention_days=30, applies_after="account_closure"),
    dict(data_class="marketing", retention_days=730, applies_after="consent_withdrawal"),
]


def _guard(engine, force, allow_remote):
    if not force:
        sys.exit("Refusing to seed without --force.")
    host = (engine.url.host or "").lower()
    is_local = host in ("", "localhost", "127.0.0.1")
    if not is_local and not allow_remote:
        sys.exit(
            f"Refusing to seed non-local DB host '{host}' without --allow-remote.\n"
            f"(DATABASE_URL points at a remote/prod DB — pass --allow-remote only if you are sure.)"
        )


def purge():
    """Remove all seed.* rows. Deletes child rows that FK to account with ON DELETE SET NULL
    explicitly, then deletes the seed accounts (cascades the rest)."""
    with session_scope() as s:
        acct_ids = [r[0] for r in s.execute(text(
            f"SELECT id FROM core.account WHERE email LIKE '%@{SEED_DOMAIN}'"
        ))]
        if not acct_ids:
            print("No seed accounts found.")
            return
        # child tables that SET NULL on account delete (would orphan otherwise)
        for tbl in ("usage_event", "nps_response", "survey_response"):
            s.execute(text(f"DELETE FROM core.{tbl} WHERE account_id = ANY(:ids)"),
                      {"ids": acct_ids})
        s.execute(text("DELETE FROM core.ticket_message WHERE ticket_id IN "
                       "(SELECT id FROM core.ticket WHERE account_id = ANY(:ids))"),
                  {"ids": acct_ids})
        s.execute(text("DELETE FROM core.ticket WHERE account_id = ANY(:ids)"), {"ids": acct_ids})
        # account delete cascades users/persons/subs/ledger/matches/consent/relationships
        s.execute(text("DELETE FROM core.account WHERE id = ANY(:ids)"), {"ids": acct_ids})
        print(f"Purged {len(acct_ids)} seed account(s) and dependents.")


def seed():
    with session_scope() as s:
        # Idempotency: if already seeded, bail (use --reset to rebuild)
        existing = accounts.get_account_by_email(s, OWNER_EMAIL)
        if existing:
            print("Seed account already exists — use --reset to rebuild. Skipping.")
            return

        # Plan catalogue + retention rules (idempotent upserts)
        for p in SEED_PLANS:
            subscriptions.upsert_plan(s, **p)
        for r in SEED_RETENTION:
            consent.upsert_retention_rule(s, **r)
        starter = subscriptions.upsert_plan(s, **SEED_PLANS[0])

        # Account + owner login (the parent)
        acct = accounts.create_account(s, email=OWNER_EMAIL, display_name="Seed Family",
                                       external_wix_id="wix-seed-0001")
        owner = accounts.create_user(s, account_id=acct.id, email=OWNER_EMAIL,
                                     auth_provider="wix", auth_provider_uid="wix-seed-0001",
                                     is_account_owner=True, marketing_opt_in=True,
                                     email_verified=True)
        accounts.record_acquisition(s, user_id=owner.id, source="google", medium="cpc",
                                    campaign="free-first-match", landing_page="/")

        parent = accounts.create_person(s, account_id=acct.id, full_name="Pat Seed",
                                        role="parent", user_id=owner.id, is_primary=True,
                                        country="GB")
        junior1 = accounts.create_person(s, account_id=acct.id, full_name="Jamie Seed",
                                         role="player", dob=date(2012, 5, 1), utr="4.2",
                                         dominant_hand="right", skill_level="intermediate",
                                         club_school="Seed Tennis Club")
        junior2 = accounts.create_person(s, account_id=acct.id, full_name="Robin Seed",
                                         role="player", dob=date(2014, 9, 15), utr="2.1",
                                         dominant_hand="left")

        # Coach (separate login, linked to the juniors)
        coach_acct = accounts.create_account(s, email=COACH_EMAIL, display_name="Coach Seed",
                                             external_wix_id="wix-seed-coach")
        coach_user = accounts.create_user(s, account_id=coach_acct.id, email=COACH_EMAIL,
                                          is_account_owner=True, email_verified=True)
        coach = accounts.create_person(s, account_id=coach_acct.id, full_name="Casey Coach",
                                       role="coach", user_id=coach_user.id, is_primary=True)

        # Relationships
        accounts.link_persons(s, from_person_id=parent.id, to_person_id=junior1.id,
                              type_="parent_junior")
        accounts.link_persons(s, from_person_id=parent.id, to_person_id=junior2.id,
                              type_="parent_junior")
        accounts.link_persons(s, from_person_id=coach.id, to_person_id=junior1.id,
                              type_="coach_player")
        accounts.link_persons(s, from_person_id=coach.id, to_person_id=junior2.id,
                              type_="coach_player")

        # Subscription + credits
        period_start = _now()
        subscriptions.upsert_subscription(
            s, account_id=acct.id, plan_id=starter.id, plan_code=starter.code,
            plan_type="recurring", price_cents=starter.price_cents, billing_interval="month",
            matches_per_period=starter.matches_included, status="active",
            current_period_start=period_start, current_period_end=period_start + timedelta(days=30),
        )
        subscriptions.grant_credits(s, account_id=acct.id, matches=3, source="subscription",
                                    plan_code=starter.code, external_wix_id="grant-seed-sub-1")
        subscriptions.grant_credits(s, account_id=acct.id, matches=3, source="payg_purchase",
                                    plan_code="topup 3", external_wix_id="grant-seed-payg-1")

        # Matches (one consumes a credit)
        m1 = matches.upsert_match(s, task_id="seed-task-0001", account_id=acct.id,
                                  sport_type="tennis_singles", pipeline="sportai",
                                  uploaded_by_user_id=owner.id, subject_person_id=junior1.id,
                                  status="complete", match_date=date.today() - timedelta(days=7),
                                  location="Seed Court 1", player_a_name="Jamie Seed",
                                  player_b_name="Opponent")
        matches.mark_processed(s, task_id="seed-task-0001",
                               kpi_summary={"points": 84, "aces": 5, "double_faults": 3,
                                            "winners": 22, "first_serve_pct": 0.61})
        subscriptions.consume_match(s, account_id=acct.id, task_id="seed-task-0001")

        matches.upsert_match(s, task_id="seed-task-0002", account_id=acct.id,
                             sport_type="tennis_singles_t5", pipeline="t5",
                             uploaded_by_user_id=owner.id, subject_person_id=junior2.id,
                             status="processing", match_date=date.today(),
                             player_a_name="Robin Seed", player_b_name="Opponent")

        # Usage events
        matches.record_usage(s, event_type="login", account_id=acct.id, user_id=owner.id)
        matches.record_usage(s, event_type="match_upload", account_id=acct.id, user_id=owner.id,
                             ref_type="match", ref_id="seed-task-0001")
        matches.record_usage(s, event_type="report_view", account_id=acct.id, user_id=owner.id,
                             ref_type="match", ref_id="seed-task-0001")
        matches.record_usage(s, event_type="ai_coach_query", account_id=acct.id, user_id=owner.id,
                             metadata={"q": "how is my second serve?"})

        # Feedback
        feedback.record_nps(s, score=9, account_id=acct.id, user_id=owner.id,
                            comment="Love the heatmaps.")
        feedback.open_ticket(s, subject="Video stuck processing", body="Match 2 has been processing for a while.",
                            account_id=acct.id, user_id=owner.id, channel="portal",
                            first_message="Any update on Robin's match?")

        # ── Consent (the compliance core) ──
        # Owner's own ToS / privacy / marketing
        for ct in ("terms_of_service", "privacy_policy", "marketing_email"):
            consent.record_consent(s, subject_person_id=parent.id, granted_by_user_id=owner.id,
                                   consent_type=ct, policy_version="2026-06-01", source="signup",
                                   evidence={"ip": "203.0.113.10", "checkbox": "I agree"})
        # Parental consent + biometric processing for BOTH minors (granted by the parent's login)
        for junior in (junior1, junior2):
            consent.record_consent(s, subject_person_id=junior.id, granted_by_user_id=owner.id,
                                   consent_type="minor_processing_parental",
                                   policy_version="2026-06-01", source="signup",
                                   evidence={"parent": "Pat Seed"})
            consent.record_consent(s, subject_person_id=junior.id, granted_by_user_id=owner.id,
                                   consent_type="biometric_processing",
                                   policy_version="2026-06-01", source="signup")

        print("Seeded: 2 accounts, 2 users, 4 persons, 4 relationships, 1 subscription, "
              "2 credit grants + 1 consume, 2 matches, 4 usage events, 1 NPS, 1 ticket, "
              "7 consent records (incl. minor parental + biometric).")


def main():
    ap = argparse.ArgumentParser(description="Seed synthetic core.* data (guarded).")
    ap.add_argument("--force", action="store_true", help="required to run")
    ap.add_argument("--allow-remote", action="store_true", help="permit a non-local DB host")
    ap.add_argument("--reset", action="store_true", help="purge seed data then reseed")
    ap.add_argument("--purge", action="store_true", help="remove seed data only")
    args = ap.parse_args()

    engine = get_engine()
    _guard(engine, args.force, args.allow_remote)

    core_init(engine)  # ensure schema exists (additive)

    if args.purge:
        purge()
        return
    if args.reset:
        purge()
    seed()


if __name__ == "__main__":
    main()
