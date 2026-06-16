# marketing_crm.consent — consent capture + withdrawal (privacy spec Part B).
#
# Customer-facing /api/client/consent/* over core.consent. Records ToS/privacy/marketing/biometric/
# parental consent with policy_version + timestamp + evidence; withdrawal routes biometric through
# core.data_subject_request. Recording consent also ensures the core identity exists (the forward
# write-path into core.*). DARK by default: register(app) no-ops unless CONSENT_ENABLED=1.

from marketing_crm.consent.blueprint import consent_bp, register  # noqa: F401

__all__ = ["consent_bp", "register"]
