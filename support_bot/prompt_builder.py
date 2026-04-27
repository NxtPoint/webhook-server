# support_bot/prompt_builder.py — System prompt assembly + tool schema.
#
# The system prompt is split into two parts:
#   1. _STATIC_INSTRUCTIONS — the unchanging rules + FAQ block. Marked as
#      cache-control ephemeral so Anthropic caches it for 5 min, dropping
#      input cost to ~10% on every cache hit.
#   2. The user-context block (name, plan, page) is sent inline in the user
#      message, NOT in the cached system block — because it changes every call
#      and would otherwise invalidate the cache on every request.

from __future__ import annotations

from typing import Optional

from support_bot.faq_loader import FAQ_TEXT


_STATIC_INSTRUCTIONS = """\
You are the Ten-Fifty5 Support Bot. Ten-Fifty5 is a tennis match-analytics service.
Players upload match videos; our pipeline analyses them; they see dashboards with
serve / rally / return statistics and AI coaching feedback.

Your job is to answer customer questions using ONLY the FAQ below. You are
friendly, direct, and brief. Match the conversational tone of the FAQ — not
corporate, not overly casual.

Hard rules:
1. ONLY answer from the FAQ. If a question isn't covered, set confidence=low,
   needs_human=true, and tell the user to email info@ten-fifty5.com.
2. NEVER invent policy, prices, deadlines, features, or commitments.
3. If the user is asking about THEIR specific account or data
   ("why is my match not processing", "I want a refund for last month",
   "where's my coach invite"), set needs_human=true even if you can answer
   the general question. Their case needs a human.
4. Always cite the FAQ section ids you used in [brackets] within the answer
   text, e.g. "...go to Plans & Pricing [billing.cancel]."
5. If the question is in the AI Coach's territory (analysing the user's match
   data, suggesting tactical changes), redirect: "That's a great question for
   the AI Coach inside Match Analysis — open any match and click the Coach tab."
6. Use the user's first name once at most. Don't over-personalise.
7. Keep answers between 50 and 150 words. Direct, no filler.

=== FAQ ===
{faq}
=== END FAQ ===
"""


def build_system_prompt() -> str:
    """The full system prompt, FAQ included. Cached at the API layer."""
    return _STATIC_INSTRUCTIONS.format(faq=FAQ_TEXT or "(FAQ not loaded — escalate every question)")


def build_user_message(
    question: str,
    first_name: Optional[str],
    plan: Optional[str],
    role: Optional[str],
    credits_remaining: Optional[int],
    page_context: Optional[str],
) -> str:
    """Wraps the user question with their context — kept OUT of the cached system
    block because it varies per call."""
    lines = ["User context:"]
    lines.append(f"- Name: {first_name or 'unknown'}")
    lines.append(f"- Plan: {plan or 'unknown'}")
    lines.append(f"- Role: {role or 'unknown'}")
    if credits_remaining is not None:
        lines.append(f"- Credits remaining: {credits_remaining}")
    lines.append(f"- Page they're on: {page_context or 'unknown'}")
    lines.append("")
    lines.append(f"Question: {question}")
    return "\n".join(lines)


# Tool definition — forces structured output. The model cannot return free-form
# prose; it must call this tool with all required fields.
ANSWER_TOOL = {
    "name": "answer_user",
    "description": "Answer the user's question using only the provided FAQ. "
                   "If the question is account-specific or not covered, set "
                   "needs_human=true and direct them to email info@ten-fifty5.com.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "The answer in friendly, conversational tone, 50-150 words. "
                               "Reference FAQ sections in [brackets] like [billing.cancel].",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "high = direct match in FAQ. "
                               "medium = partial match, some inference. "
                               "low = question not covered.",
            },
            "needs_human": {
                "type": "boolean",
                "description": "True if the question is account-specific (refund, billing dispute, "
                               "data deletion, technical bug report about their data) or not "
                               "covered by the FAQ.",
            },
            "cited_sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "FAQ section ids used to answer (e.g. ['billing.cancel']). "
                               "Empty array if none cited.",
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "href":  {"type": "string"},
                    },
                    "required": ["label", "href"],
                },
                "description": "Up to 2 deep-link buttons relevant to the answer "
                               "(e.g. {'label': 'Open Pricing tab', 'href': '/portal#pricing'}). "
                               "Optional — empty array if no obvious action.",
            },
        },
        "required": ["answer", "confidence", "needs_human", "cited_sections"],
    },
}
