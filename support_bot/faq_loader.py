# support_bot/faq_loader.py — Loads faq.md once at import, exposes content + hash.
#
# The FAQ is the entire knowledge base for the bot. It's loaded once at module
# import (cheap, ~6KB of markdown) and exposed as:
#   - FAQ_TEXT     : the raw markdown to stuff into the cached system prompt
#   - FAQ_HASH     : sha256 hex digest of the content (used to invalidate
#                    faq_cache rows when the FAQ is edited)
#   - FAQ_LOADED_AT: ISO timestamp of when the file was read
#
# Reload on Render is achieved by simply restarting the service after editing
# faq.md and pushing — no hot-reload needed.

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_FAQ_PATH = os.path.join(os.path.dirname(__file__), "faq.md")


def _load() -> tuple[str, str, str]:
    """Read faq.md from disk. Returns (text, sha256_hex, iso_timestamp)."""
    try:
        with open(_FAQ_PATH, "r", encoding="utf-8") as fh:
            text = fh.read()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        loaded_at = datetime.now(timezone.utc).isoformat()
        log.info("[support_bot.faq_loader] loaded %d chars, hash=%s",
                 len(text), digest[:12])
        return text, digest, loaded_at
    except FileNotFoundError:
        log.error("[support_bot.faq_loader] faq.md not found at %s", _FAQ_PATH)
        return "", "", datetime.now(timezone.utc).isoformat()
    except Exception:
        log.exception("[support_bot.faq_loader] failed to read faq.md")
        return "", "", datetime.now(timezone.utc).isoformat()


FAQ_TEXT, FAQ_HASH, FAQ_LOADED_AT = _load()
