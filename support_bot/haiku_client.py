# support_bot/haiku_client.py — Anthropic SDK wrapper for the support bot.
#
# Model:        claude-haiku-4-5-20251001
# Temperature:  0.3 (consistent, slight phrasing variation)
# Max tokens:   400 (answers are short by spec — 50-150 words)
# Caching:      system prompt is marked cache_control=ephemeral so the FAQ
#               block (~6KB) is cached for 5 min between calls.
# Tool use:     forced via tool_choice=tool — guarantees structured output.
#
# Returns a structured dict to the caller. Never raises — config / API errors
# come back as {ok: False, error, detail}.

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_TEMPERATURE = 0.3
_MAX_TOKENS = 400

# Haiku 4.5 pricing (per million tokens):
#   input  uncached: $1.00     -> $0.0001 per 100 tokens
#   input  cached:   $0.10     -> $0.00001 per 100 tokens (90% off)
#   input  cache write: $1.25  -> $0.000125 per 100 tokens
#   output:          $5.00     -> $0.0005 per 100 tokens
# Returned as cents (×100) for the cost_cents column.
_INPUT_UNCACHED_PER_TOK = 1.00 / 1_000_000
_INPUT_CACHE_READ_PER_TOK = 0.10 / 1_000_000
_INPUT_CACHE_WRITE_PER_TOK = 1.25 / 1_000_000
_OUTPUT_PER_TOK = 5.00 / 1_000_000


def _get_client():
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY env var is not set")
        return anthropic.Anthropic(api_key=api_key)
    except ImportError as exc:
        raise RuntimeError("anthropic package not installed") from exc


def call_haiku(
    system_prompt: str,
    user_message: str,
    tool: dict,
) -> dict:
    """
    Call Haiku 4.5 with prompt caching + forced tool use.

    On success:
      {
        ok: True,
        tool_input: dict,         # the structured answer (ANSWER_TOOL schema)
        tokens_input: int,        # uncached input tokens (the user message + tool schema)
        tokens_output: int,
        tokens_cached: int,       # cache READ tokens (the cached FAQ system prompt)
        tokens_cache_write: int,  # cache WRITE tokens (first call of a 5-min window)
        cost_cents: float,
      }
    On failure:
      { ok: False, error, detail }
    """
    try:
        client = _get_client()

        # The system prompt is wrapped in a list with cache_control=ephemeral.
        # Anthropic caches the prefix for ~5 min; subsequent calls within the
        # window pay 10% input price for the cached portion.
        system_blocks = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            system=system_blocks,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user_message}],
        )

        # Find the tool_use block — guaranteed to exist due to forced tool_choice.
        tool_input: Optional[dict] = None
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                tool_input = block.input
                break

        if tool_input is None:
            log.error("[haiku_client] no tool_use block in response — content=%s",
                      response.content)
            return {"ok": False, "error": "no_tool_call",
                    "detail": "Model did not call the answer_user tool."}

        usage = response.usage
        ti = getattr(usage, "input_tokens", 0) or 0
        to = getattr(usage, "output_tokens", 0) or 0
        tcr = getattr(usage, "cache_read_input_tokens", 0) or 0
        tcw = getattr(usage, "cache_creation_input_tokens", 0) or 0

        cost = (
            ti * _INPUT_UNCACHED_PER_TOK
            + tcr * _INPUT_CACHE_READ_PER_TOK
            + tcw * _INPUT_CACHE_WRITE_PER_TOK
            + to * _OUTPUT_PER_TOK
        ) * 100.0  # to cents

        return {
            "ok":                True,
            "tool_input":        dict(tool_input),
            "tokens_input":      ti,
            "tokens_output":     to,
            "tokens_cached":     tcr,
            "tokens_cache_write": tcw,
            "cost_cents":        round(cost, 4),
        }

    except RuntimeError as exc:
        log.error("[haiku_client] config error: %s", exc)
        return {"ok": False, "error": "config_error", "detail": str(exc)}
    except Exception as exc:
        err_type = type(exc).__name__
        log.exception("[haiku_client] API call failed: %s", err_type)
        if "RateLimitError" in err_type:
            return {"ok": False, "error": "anthropic_rate_limit",
                    "detail": "Claude API rate limit hit — try again in a moment."}
        if "APIStatusError" in err_type or "APIError" in err_type:
            return {"ok": False, "error": "anthropic_api_error",
                    "detail": f"Claude API error: {str(exc)[:200]}"}
        return {"ok": False, "error": "claude_call_failed",
                "detail": f"{err_type}: {str(exc)[:200]}"}
