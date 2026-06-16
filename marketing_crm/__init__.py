# marketing_crm — the growth / CRM / admin stack, packed in one place.
#
# Mirrors how T5 lives in ml_pipeline/ (code) + ml_analysis.* (schema): this package is the
# CODE home for the growth machinery; the DATA it reads is the canonical core.* schema (core_db/).
#
# Subpackages (built incrementally):
#   contracts/   shared specs both Claude Code and Cowork treat as source of truth (events,
#                lifecycle stages, HubSpot field map, data dictionary, privacy inputs)
#   tracking/    event instrumentation (Amplitude + core.usage_event)        [Prompt 3]
#   crm_sync/    DB → HubSpot sync                                            [Prompt 4]
#   backoffice/  internal admin cockpit (business health, customers, ops)    [Prompt 5]
#   feedback/    in-app feedback widget + NPS survey backend                 [Prompt 6]
#   referral/    referral system                                             [later]
#
# core_db/ (source of truth) stays foundational — product, billing, AND marketing read it.
# This package is a CONSUMER of core, never the other way round.
