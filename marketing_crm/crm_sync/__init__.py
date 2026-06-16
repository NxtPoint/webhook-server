# marketing_crm.crm_sync — push core.* → HubSpot + Klaviyo (Prompt 4).
#
# core.* is the source of truth; HubSpot/Klaviyo are one-way downstream mirrors. This module is
# the data feed Cowork's flows depend on: profile traits (HubSpot contact + Klaviyo profile) and
# forwarded events (Klaviyo metrics → flow triggers).
#
# DARK by default: everything no-ops unless CRM_SYNC_ENABLED=1 and the relevant API key is set.
# Privacy boundary (hard, per contracts/hubspot_field_map.md): never sync minor PII or biometric
# data; marketing email only when marketing_opt_in is true.

from marketing_crm.crm_sync.sync import (  # noqa: F401
    enabled, build_traits, sync_profile, forward_event, sync_all,
)

__all__ = ["enabled", "build_traits", "sync_profile", "forward_event", "sync_all"]
