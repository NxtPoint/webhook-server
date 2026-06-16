# core_db/backfill.py — one-account migration from billing.*/bronze.* → core.*.
#
# Pre-launch there's no general backfill (Render is gospel; new signups self-populate core via the
# consent write-path). This migrates a SINGLE named account (e.g. tomo's) so it shows in the cockpit.
# Idempotent + defensive: re-runnable, skips what already exists, tolerates missing optional tables.
#
# Usage: .venv/Scripts/python -m core_db.backfill tomo.stojakovic@gmail.com

import sys

from sqlalchemy import text

from core_db.db import session_scope, norm_email
from core_db.models import CreditLedger, Person
from core_db.repositories import accounts, subscriptions, matches
from core_db.schema import core_init

_ROLE_MAP = {"player_parent": "player", "coach": "coach", "parent": "parent", "player": "player"}


def _table_exists(conn, schema, name):
    return conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=:s AND table_name=:n"),
        {"s": schema, "n": name}).scalar() is not None


def backfill_account(email):
    email = norm_email(email)
    core_init()
    summary = {"email": email, "account": False, "persons": 0, "grants": 0, "consumptions": 0,
               "subscription": False, "matches": 0}
    with session_scope() as s:
        b = s.execute(text(
            "SELECT id, primary_full_name, external_wix_id, currency_code "
            "FROM billing.account WHERE lower(email)=:e"), {"e": email}).mappings().first()
        if not b:
            print(f"No billing.account for {email} — nothing to backfill.")
            return summary
        bid = b["id"]

        # Account + owner user
        acct = accounts.create_account(s, email=email, display_name=b["primary_full_name"],
                                       currency_code=b["currency_code"] or "USD",
                                       external_wix_id=b["external_wix_id"])
        owner = accounts.create_user(s, account_id=acct.id, email=email, is_account_owner=True,
                                     auth_provider="wix", auth_provider_uid=b["external_wix_id"],
                                     email_verified=True)
        summary["account"] = True

        # Members → persons (skip ones already migrated by name)
        existing = {p.full_name for p in accounts.list_persons_for_account(s, acct.id, include_deleted=True)}
        mems = s.execute(text(
            "SELECT full_name, surname, role, is_primary, dob, utr, dominant_hand, country, area, "
            "skill_level, club_school, notes FROM billing.member WHERE account_id=:a ORDER BY is_primary DESC, id"),
            {"a": bid}).mappings().all()
        for m in mems:
            if m["full_name"] in existing:
                continue
            person = accounts.create_person(
                s, account_id=acct.id, full_name=m["full_name"], surname=m["surname"],
                role=_ROLE_MAP.get(m["role"], "player"), is_primary=bool(m["is_primary"]),
                user_id=(owner.id if m["is_primary"] else None), dob=m["dob"], utr=m["utr"],
                dominant_hand=m["dominant_hand"], country=m["country"], area=m["area"],
                skill_level=m["skill_level"], club_school=m["club_school"], notes=m["notes"])
            existing.add(m["full_name"])
            summary["persons"] += 1

        # Entitlement grants → ledger grants (idempotent via external_wix_id)
        for g in s.execute(text(
            "SELECT source, plan_code, matches_granted, external_wix_id "
            "FROM billing.entitlement_grant WHERE account_id=:a"), {"a": bid}).mappings():
            subscriptions.grant_credits(s, account_id=acct.id, matches=g["matches_granted"] or 0,
                                        source=g["source"] or "manual", plan_code=g["plan_code"],
                                        external_wix_id=g["external_wix_id"])
            summary["grants"] += 1

        # Consumption → ledger consume entries (idempotent on (ref_type, ref_id))
        for c in s.execute(text(
            "SELECT task_id, consumed_matches, source FROM billing.entitlement_consumption WHERE account_id=:a"),
            {"a": bid}).mappings():
            tid = str(c["task_id"])
            dup = s.execute(text(
                "SELECT 1 FROM core.credit_ledger WHERE entry_type='consume' AND ref_type='match' AND ref_id=:r"),
                {"r": tid}).scalar()
            if dup:
                continue
            s.add(CreditLedger(account_id=acct.id, entry_type="consume",
                               matches_delta=-(c["consumed_matches"] or 1), techniques_delta=0,
                               source=c["source"] or "match_upload", ref_type="match", ref_id=tid))
            summary["consumptions"] += 1
        s.flush()

        # Subscription (optional table)
        if _table_exists(s.connection(), "billing", "subscription_state"):
            ss = s.execute(text(
                "SELECT plan_code, plan_type, status, matches_granted FROM billing.subscription_state WHERE account_id=:a"),
                {"a": bid}).mappings().first()
            if ss:
                subscriptions.upsert_subscription(
                    s, account_id=acct.id, plan_code=ss["plan_code"], plan_type=ss["plan_type"],
                    status=(ss["status"] or "active").lower(), matches_per_period=ss["matches_granted"])
                summary["subscription"] = True

        # Matches from bronze (idempotent on task_id)
        for r in s.execute(text(
            "SELECT task_id, sport_type, match_date, location, player_a_name, player_b_name, "
            "ingest_started_at, ingest_finished_at, ingest_error, trim_output_s3_key "
            "FROM bronze.submission_context WHERE lower(email)=:e AND deleted_at IS NULL"),
            {"e": email}).mappings():
            status = ("failed" if r["ingest_error"] else "complete" if r["ingest_finished_at"]
                      else "processing" if r["ingest_started_at"] else "uploaded")
            matches.upsert_match(s, task_id=str(r["task_id"]), account_id=acct.id,
                                 uploaded_by_user_id=owner.id, sport_type=r["sport_type"],
                                 status=status, match_date=r["match_date"], location=r["location"],
                                 player_a_name=r["player_a_name"], player_b_name=r["player_b_name"],
                                 trim_s3_key=r["trim_output_s3_key"],
                                 processed_at=r["ingest_finished_at"])
            summary["matches"] += 1

    print("Backfill complete:", summary)
    return summary


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m core_db.backfill <email>")
    backfill_account(sys.argv[1])
