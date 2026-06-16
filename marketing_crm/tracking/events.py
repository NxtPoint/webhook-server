# marketing_crm/tracking/events.py — canonical event names (mirror contracts/events.md).
# Use these constants, never string literals, so the taxonomy stays single-sourced.

ACCOUNT_CREATED = "account_created"
LOGIN = "login"
MATCH_UPLOADED = "match_uploaded"
MATCH_PROCESSED = "match_processed"
MATCH_FAILED = "match_failed"
REPORT_VIEWED = "report_viewed"
AI_COACH_QUERY = "ai_coach_query"
TECHNIQUE_UPLOADED = "technique_uploaded"
CREDIT_PURCHASED = "credit_purchased"
SUBSCRIPTION_STARTED = "subscription_started"
SUBSCRIPTION_CANCELLED = "subscription_cancelled"
COACH_INVITED = "coach_invited"
COACH_ACCEPTED = "coach_accepted"
NPS_SUBMITTED = "nps_submitted"
FEEDBACK_SUBMITTED = "feedback_submitted"
CANCELLATION_REASON_SUBMITTED = "cancellation_reason_submitted"

EVENTS = {
    ACCOUNT_CREATED, LOGIN, MATCH_UPLOADED, MATCH_PROCESSED, MATCH_FAILED, REPORT_VIEWED,
    AI_COACH_QUERY, TECHNIQUE_UPLOADED, CREDIT_PURCHASED, SUBSCRIPTION_STARTED,
    SUBSCRIPTION_CANCELLED, COACH_INVITED, COACH_ACCEPTED, NPS_SUBMITTED, FEEDBACK_SUBMITTED,
    CANCELLATION_REASON_SUBMITTED,
}
