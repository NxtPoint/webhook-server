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
    return req


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
