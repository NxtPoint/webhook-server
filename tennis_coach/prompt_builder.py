# tennis_coach/prompt_builder.py — Prompt templates for LLM Tennis Coach.
#
# Each builder returns (messages, system) ready for claude_client.call_claude().
# Messages are plain user-role content (no tool use, no function calling).
# System prompt is fixed — defined once below and reused across all templates.

import json
from typing import Tuple

MessageList = list[dict]

SYSTEM_PROMPT = """\
You are a professional tennis coach with 20 years of experience coaching players
from club level to ATP/WTA circuit. You analyse match data with the precision of
a tour-level analyst and the directness of a coach who respects their player's
time.

Your job is to help Player A (your client) IMPROVE. You focus exclusively on
what the player can control — their own technique, patterns, and decision-making.
You never discuss the opponent's weaknesses, strategies, or how to "beat" them.
Your coaching philosophy: consistent self-improvement is what wins matches.

Rules:
- You ONLY coach Player A. Never analyse or comment on the opponent's game.
  If asked about the opponent (e.g. "what are their weaknesses", "how do I beat
  them"), redirect: "My job is to make YOU better — let's focus on what you
  can control."
- Every coaching point must reference at least one specific statistic from the
  data provided, cited in brackets: e.g. "Your first serve percentage [54%] is
  below where you want it"
- Maximum 3 coaching points per response, each under 60 words
- Use direct language: "Your backhand is leaking errors" not "There may be room
  for improvement on your backhand side"
- If the data does not support a conclusion, say so explicitly: "The data
  doesn't give me enough serve placement detail to comment on T vs Wide
  selection"
- Never fabricate statistics. If a field is null or missing, ignore that
  dimension
- Close with one concrete drill or focus point for the next session\
"""

CARDS_SYSTEM_PROMPT = """\
You are a professional tennis coach focused on helping Player A improve. You
ONLY coach Player A — never analyse or comment on the opponent. Focus on what
the player can control to get better.

Analyse the match data and return a JSON array of exactly 3 insight cards.
Each card is an object with these fields:
  title    (string, max 6 words — punchy label)
  body     (string, 30-50 words — direct coaching insight for Player A only,
             citing at least one bracketed stat, e.g. [54%])
  category (string — one of: "serve", "rally", "tactics", "mental", "return")

Return ONLY valid JSON — no markdown fences, no prose outside the array.
Example format:
[{"title":"Fix your first serve","body":"Your first serve percentage [54%] is ...","category":"serve"},...]
\
"""


def _data_block(match_data: dict) -> str:
    """Compact JSON representation of match data for injection into user message."""
    return json.dumps(match_data, separators=(",", ":"), default=str)


def build_serve_analysis_prompt(match_data: dict) -> Tuple[MessageList, str]:
    """
    Template 1 — Serve Analysis.
    Focus: overall serve verdict → strongest direction → weakest direction → drill.
    Reads: summary + serve (KPIs + direction breakdown).
    """
    serve_data = {
        "match":   match_data.get("match", {}),
        "summary": {
            k: v for k, v in match_data.get("summary", {}).items()
            if k in ("total_points", "aces", "double_faults")
        },
        "serve": match_data.get("serve", {}),
    }
    user_msg = (
        "Analyse player A's serve performance based on the data below.\n\n"
        f"DATA:\n{_data_block(serve_data)}\n\n"
        "Give me: (1) overall serve verdict, (2) strongest direction to exploit, "
        "(3) weakest direction to fix. End with one specific drill."
    )
    return [{"role": "user", "content": user_msg}], SYSTEM_PROMPT


def build_weakness_prompt(match_data: dict) -> Tuple[MessageList, str]:
    """
    Template 2 — Biggest Weakness.
    Focus: highest error-rate pattern → secondary observation → corrective drill.
    Reads: summary + rally patterns + return data.
    """
    weakness_data = {
        "match":   match_data.get("match", {}),
        "summary": {
            k: v for k, v in match_data.get("summary", {}).items()
            if k in ("total_points", "unforced_errors", "winners")
        },
        "rally":   match_data.get("rally", {}),
        "return":  match_data.get("return", {}),
    }
    user_msg = (
        "What is player A's biggest weakness in this match?\n\n"
        f"DATA:\n{_data_block(weakness_data)}\n\n"
        "Identify the single biggest error source, name one contributing pattern, "
        "and close with one corrective drill for the next session."
    )
    return [{"role": "user", "content": user_msg}], SYSTEM_PROMPT


def build_tactics_prompt(match_data: dict) -> Tuple[MessageList, str]:
    """
    Template 3 — Tactical Improvement.
    Focus: player A's biggest area for improvement → pattern to develop → what to stop doing.
    Reads: all four sections but only coaches player A.
    """
    user_msg = (
        "Based on player A's performance in this match, what tactical adjustments "
        "should they focus on to improve?\n\n"
        f"DATA:\n{_data_block(match_data)}\n\n"
        "Identify (1) the area of player A's game with the most room for improvement, "
        "(2) a specific tactical pattern player A should develop in practice, "
        "(3) what player A should stop doing — the habit that is costing them points."
    )
    return [{"role": "user", "content": user_msg}], SYSTEM_PROMPT


def build_cards_prompt(match_data: dict) -> Tuple[MessageList, str]:
    """
    Template 4 — Pre-generated insight cards.
    Returns JSON array of 3 cards {title, body, category}.
    Uses a different system prompt that instructs JSON-only output.
    """
    user_msg = (
        "Generate 3 coaching insight cards for player A based on the match data below.\n\n"
        f"DATA:\n{_data_block(match_data)}\n\n"
        "Return a JSON array of exactly 3 card objects with fields: title, body, category."
    )
    return [{"role": "user", "content": user_msg}], CARDS_SYSTEM_PROMPT


def build_freeform_prompt(match_data: dict, question: str) -> Tuple[MessageList, str]:
    """
    Freeform question — pass match context + user question.
    Uses the standard coaching system prompt (not JSON mode).
    """
    user_msg = (
        f"{question.strip()}\n\n"
        f"DATA:\n{_data_block(match_data)}"
    )
    return [{"role": "user", "content": user_msg}], SYSTEM_PROMPT
