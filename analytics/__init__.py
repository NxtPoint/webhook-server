# analytics/ — portable website-traffic aggregation over core.usage_event.
#
# SHARED ENGINE (replicable across sites — kept in lock-step with the nextpoint repo). Every
# function is a guarded, read-only SELECT against the page-view beacon data in core.usage_event
# (event_type='page_view' for the views; 'page_leave' for time-on-site). A missing/empty table
# yields a safe empty default, never a 500.
#
# Site-agnostic: functions take a SQLAlchemy session/connection + a day window (+ optional
# club_id for multi-tenant sites; ten-fifty5 passes None). The metadata JSONB contract is set by
# marketing_crm/tracking/beacon.py and is identical everywhere, so this module is drop-in.
#
# ten-fifty5 surfaces overview() in the /backoffice cockpit (Website Traffic panel). nextpoint
# surfaces the same traffic functions in its analytics.routes Business Overview.

from analytics.traffic import overview  # noqa: F401

__all__ = ["overview"]
