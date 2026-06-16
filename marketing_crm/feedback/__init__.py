# marketing_crm.feedback — in-app feedback + NPS (Prompt 6).
#
# blueprint.py: customer-facing /api/client/feedback/* (NPS, widget feedback, cancellation survey,
# NPS eligibility). Writes core.nps_response / core.survey_response and emits tracking events.
# DARK by default: register(app) is a no-op unless FEEDBACK_ENABLED=1.

from marketing_crm.feedback.blueprint import feedback_bp, register  # noqa: F401

__all__ = ["feedback_bp", "register"]
