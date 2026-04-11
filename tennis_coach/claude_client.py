# tennis_coach/claude_client.py — Thin Anthropic SDK wrapper for tennis coach.
#
# Model: claude-sonnet-4-6
# Temperature: 0.3 (low variation, deterministic coaching)
# Max tokens: 600 (system prompt caps at 3 points × 60 words; hard ceiling here)
#
# Error handling: structured error dict returned so callers can surface
# human-readable messages without crashing the HTTP endpoint.

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 600


def _get_client():
    """Lazy-initialise the Anthropic client (avoids import-time failures)."""
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY env var is not set")
        return anthropic.Anthropic(api_key=api_key)
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package is not installed. Add 'anthropic' to requirements.txt."
        ) from exc


def call_claude(
    messages: list[dict],
    system: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> dict:
    """
    Call Claude and return a structured result dict.

    On success:  { ok: True,  text, input_tokens, output_tokens }
    On failure:  { ok: False, error, detail }

    The caller is responsible for deciding whether to propagate the error or
    surface it to the user — this function never raises.
    """
    try:
        client = _get_client()
        response = client.messages.create(
            model=_MODEL,
            max_tokens=max_tokens,
            temperature=_TEMPERATURE,
            system=system,
            messages=messages,
        )
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        return {
            "ok":            True,
            "text":          text.strip(),
            "input_tokens":  response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    except RuntimeError as exc:
        # Config / import errors (no key, no package)
        log.error("[claude_client] config error: %s", exc)
        return {"ok": False, "error": "config_error", "detail": str(exc)}

    except Exception as exc:
        # Anthropic API errors (rate limit, server error, network timeout, etc.)
        err_type = type(exc).__name__
        log.exception("[claude_client] API call failed: %s", err_type)

        # Detect common Anthropic SDK error types by name (avoids hard import dep)
        if "RateLimitError" in err_type:
            return {
                "ok":     False,
                "error":  "anthropic_rate_limit",
                "detail": "Claude API rate limit hit — please try again in a moment.",
            }
        if "APIStatusError" in err_type or "APIError" in err_type:
            return {
                "ok":     False,
                "error":  "anthropic_api_error",
                "detail": f"Claude API error: {str(exc)[:200]}",
            }
        return {
            "ok":     False,
            "error":  "claude_call_failed",
            "detail": f"{err_type}: {str(exc)[:200]}",
        }
