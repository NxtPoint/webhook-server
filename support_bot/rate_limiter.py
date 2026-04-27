# support_bot/rate_limiter.py — Per-email daily question cap.
#
# Limits (UTC calendar day):
#   - Soft: 30 questions / 24h before warning (not enforced; logged for review)
#   - Hard: 100 questions / 24h before 429 returned
#
# Cache hits (faq_cache deduped responses) DO count toward the limit, since
# a turn is still logged either way. This is intentional — the limit guards
# against runaway abuse, not against thoughtful re-asking.

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Tuple

from support_bot.db import count_daily

log = logging.getLogger(__name__)

SOFT_LIMIT = 30
HARD_LIMIT = 100


def _tomorrow_midnight_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


def check_rate_limit(email: str) -> Tuple[bool, str, str, int]:
    """
    Returns (allowed, reason, resets_at, used_today).
      - allowed:    False if hard limit hit
      - reason:     'daily_limit_reached' if blocked, '' otherwise
      - resets_at:  ISO timestamp of tomorrow midnight UTC
      - used_today: how many questions this email has asked today
    """
    resets_at = _tomorrow_midnight_utc_iso()
    try:
        used = count_daily(email)
    except Exception:
        log.exception("[support_bot.rate_limiter] count failed for %s — failing open", email)
        return True, "", resets_at, 0

    if used >= HARD_LIMIT:
        return False, "daily_limit_reached", resets_at, used
    return True, "", resets_at, used
