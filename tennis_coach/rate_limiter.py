# tennis_coach/rate_limiter.py — Rate limiting for LLM coach calls.
#
# Limits (per UTC calendar day):
#   - Per (email, task_id): max 5 freeform calls
#   - Per email (all matches): max 20 total calls
#
# Cards ('prompt_key = cards') are excluded from all counts.

import logging
from datetime import datetime, timezone
from typing import Tuple

from tennis_coach.db import count_daily_calls

log = logging.getLogger(__name__)

_PER_MATCH_LIMIT = 5
_PER_USER_LIMIT  = 20


def _tomorrow_midnight_utc() -> str:
    """ISO string for the start of tomorrow (UTC)."""
    now = datetime.now(timezone.utc)
    # next midnight UTC
    from datetime import timedelta
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


def check_rate_limit(email: str, task_id: str) -> Tuple[bool, str, str]:
    """
    Check whether the user may make another LLM call.

    Returns (allowed, reason, resets_at) where:
      - allowed:   True if the call is permitted
      - reason:    empty string if allowed, else a machine-readable error key
      - resets_at: ISO timestamp for when the limit resets (midnight UTC)
    """
    resets_at = _tomorrow_midnight_utc()

    try:
        per_match = count_daily_calls(email, task_id)
        if per_match >= _PER_MATCH_LIMIT:
            return False, "per_match_daily_limit_reached", resets_at

        per_user = count_daily_calls(email)
        if per_user >= _PER_USER_LIMIT:
            return False, "daily_limit_reached", resets_at
    except Exception:
        log.exception("[rate_limiter] check failed for email=%s task_id=%s — allowing", email, task_id)
        # Fail open: if the rate limit check itself errors, let the call through
        # rather than blocking the user.
        return True, "", resets_at

    return True, "", resets_at
