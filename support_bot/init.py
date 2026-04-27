# support_bot/init.py — Boot-time initialisation for the support bot.
#
# Called from upload_app.py wrapped in a try/except so a failure here
# cannot kill the main service.

import logging

log = logging.getLogger(__name__)


def init_support_bot():
    """Idempotent init: schema creation + FAQ load (the latter happens at import)."""
    try:
        from support_bot.db import init_support_schema
        init_support_schema()
    except Exception:
        log.exception("[support_bot] init_support_schema failed")

    try:
        from support_bot.faq_loader import FAQ_HASH, FAQ_TEXT
        if not FAQ_TEXT:
            log.warning("[support_bot] FAQ is empty — bot will escalate every question")
        else:
            log.info("[support_bot] FAQ loaded (%d chars, hash=%s)",
                     len(FAQ_TEXT), FAQ_HASH[:12])
    except Exception:
        log.exception("[support_bot] faq load check failed")
