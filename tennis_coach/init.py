# tennis_coach/init.py — Boot-time initialisation for the tennis coach package.
#
# Called from upload_app.py after gold_init_presentation() so that gold.vw_player
# already exists when coach_views.py tries to CREATE VIEW gold.coach_rally_patterns
# (which JOINs gold.vw_player).

import logging

log = logging.getLogger(__name__)


def init_tennis_coach():
    """
    Idempotent boot initialisation:
      1. Creates gold.coach_rally_patterns + gold.coach_pressure_points views.
      2. Creates tennis_coach schema + coach_cache table + unique index.

    Errors in either step are logged but do not raise — a failure here must
    never crash the main service on boot.
    """
    try:
        from tennis_coach.coach_views import init_coach_views
        result = init_coach_views()
        log.info("[tennis_coach] coach views init: %s", result)
    except Exception:
        log.exception("[tennis_coach] init_coach_views() failed")

    try:
        from tennis_coach.db import init_coach_cache
        init_coach_cache()
    except Exception:
        log.exception("[tennis_coach] init_coach_cache() failed")
