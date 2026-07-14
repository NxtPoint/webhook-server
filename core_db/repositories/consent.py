# core_db/repositories/consent.py — consent, data-subject requests, retention rules.
#
# This is the compliance core. For a minor, `subject_person_id` is the junior and
# `granted_by_user_id` is the consenting parent's login.

from datetime import datetime, timezone

from sqlalchemy import select

from core_db.models import Consent, DataSubjectRequest, RetentionRule

CONSENT_TYPES = (
    "terms_of_service",
    "privacy_policy",
    "marketing_email",
    "biometric_processing",
    "minor_processing_parental",
)


def _now():
    return datetime.now(timezone.utc)


def record_consent(session, *, subject_person_id, consent_type, granted_by_user_id=None,
                   status="granted", policy_version=None, source=None, evidence=None):
    if consent_type not in CONSENT_TYPES:
        raise ValueError(f"unknown consent_type: {consent_type}")
    row = Consent(
        subject_person_id=subject_person_id,
        granted_by_user_id=granted_by_user_id,
        consent_type=consent_type,
        policy_version=policy_version,
        status=status,
        source=source,
        evidence=evidence,
        granted_at=_now() if status == "granted" else None,
        withdrawn_at=_now() if status == "withdrawn" else None,
    )
    session.add(row)
    session.flush()
    return row


def withdraw_consent(session, *, subject_person_id, consent_type, granted_by_user_id=None,
                     source="portal"):
    return record_consent(
        session, subject_person_id=subject_person_id, consent_type=consent_type,
        granted_by_user_id=granted_by_user_id, status="withdrawn", source=source,
    )


def latest_consent(session, subject_person_id, consent_type):
    """Most recent consent record of a type for a subject (its status = current state)."""
    return session.execute(
        select(Consent).where(
            Consent.subject_person_id == subject_person_id,
            Consent.consent_type == consent_type,
        ).order_by(Consent.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def has_consent(session, subject_person_id, consent_type):
    row = latest_consent(session, subject_person_id, consent_type)
    return bool(row and row.status == "granted")


def open_dsar(session, *, request_type, subject_person_id=None, requested_by_user_id=None,
              notes=None):
    req = DataSubjectRequest(
        request_type=request_type, subject_person_id=subject_person_id,
        requested_by_user_id=requested_by_user_id, notes=notes,
    )
    session.add(req)
    session.flush()
    _alert_dsar_opened(session, req, subject_person_id)
    return req


def _alert_dsar_opened(session, req, subject_person_id):
    """Ops email on a new data-subject request — GDPR response deadlines apply, and this is
    the easiest alert to miss (it only shows in the cockpit otherwise). Best-effort; the
    email must never break the DSAR intake, so all of it is swallowed on error."""
    try:
        from coach_invite.video_complete_email import send_ops_email
        subject = "—"
        if subject_person_id is not None:
            try:
                from core_db.models import Person, Account
                p = session.get(Person, subject_person_id)
                if p is not None:
                    acct = session.get(Account, p.account_id) if getattr(p, "account_id", None) else None
                    subject = (getattr(acct, "email", None) or getattr(p, "full_name", None) or "—")
            except Exception:
                pass
        send_ops_email(
            f"🔐 DSAR opened: {req.request_type} ({subject})",
            "A data-subject request was raised — GDPR response deadlines apply.\n\n"
            f"Type:    {req.request_type}\n"
            f"Subject: {subject}\n"
            f"Request: #{getattr(req, 'id', '—')}\n"
            f"Notes:   {req.notes or '—'}\n\n"
            "Action it in Cockpit → DSAR.",
        )
    except Exception:
        pass


# Current consent policy version stamped on every consent record. FINAL (2026-06-18) — the scope-v2
# privacy package is adopted; remaining real-world admin (IO registration, Art.27 reps, entity
# details) is operational, not a blocker on the data policy. Bump the date on any future revision.
CURRENT_POLICY_VERSION = "1.0-2026-06-18"

# Retention policy — (data_class, applies_after, retention_days, note). Loaded into
# core.retention_rule via load_interim_retention_rules() (idempotent). Source: the scope-v2 privacy
# decisions in docs/business/privacy-and-consent.md.
INTERIM_RETENTION_RULES = [
    ("match_video",    "upload",             0,    "original upload deleted post-processing"),
    ("match_video",    "account_closure",    90,   "trimmed review clip"),
    ("biometrics",     "account_closure",    30,   "pose/biometric data"),
    ("biometrics",     "consent_withdrawal", 0,    "delete immediately on biometric consent withdrawal"),
    ("match_analysis", "account_closure",    90,   "derived analytics"),
    ("account_pii",    "account_closure",    90,   "account PII"),
    ("financial",      "account_closure",    2555, "anonymised financial (~7 years)"),
]


def load_interim_retention_rules(session, policy_version=CURRENT_POLICY_VERSION):
    """Idempotently upsert the interim retention policy into core.retention_rule. Returns the rows."""
    out = []
    for data_class, applies_after, days, note in INTERIM_RETENTION_RULES:
        upsert_retention_rule(session, data_class=data_class, applies_after=applies_after,
                              retention_days=days, is_active=True,
                              notes=f"{note} · {policy_version}")
        out.append({"data_class": data_class, "applies_after": applies_after, "retention_days": days})
    return out


def upsert_retention_rule(session, *, data_class, retention_days, applies_after,
                          is_active=True, notes=None):
    rule = session.execute(
        select(RetentionRule).where(
            RetentionRule.data_class == data_class,
            RetentionRule.applies_after == applies_after,
        )
    ).scalar_one_or_none()
    if rule is None:
        rule = RetentionRule(data_class=data_class, applies_after=applies_after)
        session.add(rule)
    rule.retention_days = retention_days
    rule.is_active = is_active
    rule.notes = notes
    rule.updated_at = _now()
    session.flush()
    return rule
