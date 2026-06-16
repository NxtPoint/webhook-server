# marketing_crm.backoffice — internal admin cockpit (Prompt 5).
#
# views.py      : core.* analytic views (business health, customers, at-risk, processing ops)
# blueprint.py  : /api/client/backoffice/cockpit/* read-only endpoints (admin-only, env-gated)
#
# Reads core.* (source of truth) + bronze.submission_context (live processing state).
# Aggregation lives in SQL views (repo rule #2); endpoints are thin passthroughs.

from marketing_crm.backoffice.views import init_cockpit_views  # noqa: F401
from marketing_crm.backoffice.blueprint import cockpit_bp, register  # noqa: F401

__all__ = ["init_cockpit_views", "cockpit_bp", "register"]
